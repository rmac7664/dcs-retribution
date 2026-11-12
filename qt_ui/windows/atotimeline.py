"""The ATO timeline: every package on one time axis.

Rows are packages, grouped by mission type. Each flight is a bar: a thin line for
start-up and taxi, a bar while airborne, a brighter band while on station, and a
diamond at its time over target. The DCS mission window is shaded and the player's
main push marked.

Drag one of your packages left or right to move its time over target (whole
minutes). Double-click a package to open it. Ctrl+wheel zooms around the cursor.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from game.ato.timeline import (
    FlightBar,
    PackageRow,
    build_rows,
    category_order,
    dcs_window,
    main_push,
    snap,
)
from game.server import EventStream
from game.sim import GameUpdateEvents
from qt_ui.models import GameModel
from qt_ui.widgets.controls import style_button
from qt_ui.windows.GameUpdateSignal import GameUpdateSignal

LABEL_WIDTH = 230
AXIS_HEIGHT = 34
HEADER_HEIGHT = 20
FLIGHT_HEIGHT = 16
ROW_PADDING = 6

BG = QColor("#1F2630")
GRID = QColor("#2E3946")
TEXT = QColor("#D3DFE8")
DIM_TEXT = QColor("#8C9BAA")
NOW_LINE = QColor("#F2C230")
WINDOW_FILL = QColor(120, 160, 150, 60)
WINDOW_EDGE = QColor("#6FA39A")
PUSH = QColor("#F0A030")
PLAYER = QColor("#5FD068")

CATEGORY_COLORS = {
    "Defensive counter-air": QColor("#6470C8"),
    "Offensive counter-air": QColor("#7C5CC4"),
    "Support (AEW&C, tankers)": QColor("#4FA8A0"),
    "SEAD / DEAD": QColor("#E08A3C"),
    "Strike": QColor("#B8405A"),
    "Interdiction": QColor("#C8583C"),
    "Close air support": QColor("#A0A040"),
    "Other": QColor("#7A8794"),
}


def _color(row: PackageRow) -> QColor:
    color = QColor(CATEGORY_COLORS.get(row.category, CATEGORY_COLORS["Other"]))
    if not row.blue:
        # The enemy's packages are drawn faded, behind yours.
        color = color.darker(160)
        color.setAlpha(170)
    return color


class _Layout:
    """Where each category header and package row sits vertically."""

    def __init__(self, rows: list[PackageRow]) -> None:
        self.items: list[tuple[str, object, int, int]] = []
        y = AXIS_HEIGHT
        current: Optional[str] = None
        order = category_order()
        for row in sorted(rows, key=lambda r: order.index(r.category)):
            if row.category != current:
                current = row.category
                self.items.append(("header", current, y, HEADER_HEIGHT))
                y += HEADER_HEIGHT
            height = max(1, len(row.flights)) * FLIGHT_HEIGHT + 2 * ROW_PADDING
            self.items.append(("row", row, y, height))
            y += height
        self.height = y + 10

    def row_at(self, y: int) -> Optional[tuple[PackageRow, int, int]]:
        for kind, item, top, height in self.items:
            if kind == "row" and top <= y < top + height:
                assert isinstance(item, PackageRow)
                return item, top, height
        return None


class TimelineCanvas(QWidget):
    """Draws the timeline and handles zoom, drag and double-click."""

    tot_moved = Signal(object, object)  # (Package, new TOT)
    package_opened = Signal(object)  # Package

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.rows: list[PackageRow] = []
        self.layout_: _Layout = _Layout([])
        self.origin = datetime.now()
        self.end = self.origin + timedelta(hours=6)
        self.mission_window: Optional[tuple[datetime, datetime]] = None
        self.now: Optional[datetime] = None
        self.push: Optional[datetime] = None
        self.px_per_minute = 2.6
        self._drag_row: Optional[PackageRow] = None
        self._drag_start_x = 0
        self._drag_delta = timedelta()

    # -- data --------------------------------------------------------------

    def set_rows(
        self,
        rows: list[PackageRow],
        now: datetime,
        window: tuple[datetime, datetime],
        push: Optional[datetime],
    ) -> None:
        self.rows = rows
        self.layout_ = _Layout(rows)
        self.now = now
        self.mission_window = window
        self.push = push
        starts = [f.start for r in rows for f in r.flights if f.start is not None]
        ends = [f.end for r in rows for f in r.flights if f.end is not None]
        self.origin = min([now, window[0], *starts])
        self.origin = self.origin.replace(minute=0, second=0, microsecond=0)
        self.end = max([window[1], *ends, self.origin + timedelta(hours=2)])
        self._resize()

    def _resize(self) -> None:
        minutes = (self.end - self.origin).total_seconds() / 60 + 30
        self.setMinimumSize(
            int(LABEL_WIDTH + minutes * self.px_per_minute), self.layout_.height
        )
        self.update()

    def zoom(self, factor: float, anchor_x: Optional[float] = None) -> None:
        anchor_time = self.time_at(anchor_x) if anchor_x is not None else None
        self.px_per_minute = min(30.0, max(0.4, self.px_per_minute * factor))
        self._resize()
        if anchor_time is not None and anchor_x is not None:
            scroll = self._scroll_area()
            if scroll is not None:
                bar = scroll.horizontalScrollBar()
                offset = anchor_x - bar.value()
                bar.setValue(int(self.x_for(anchor_time) - offset))

    def fit(self, width: int) -> None:
        minutes = max(60.0, (self.end - self.origin).total_seconds() / 60 + 30)
        self.px_per_minute = max(0.4, (width - LABEL_WIDTH - 20) / minutes)
        self._resize()

    def _scroll_area(self) -> Optional[QScrollArea]:
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        return parent

    # -- geometry ----------------------------------------------------------

    def x_for(self, time: datetime) -> float:
        minutes = (time - self.origin).total_seconds() / 60
        return LABEL_WIDTH + minutes * self.px_per_minute

    def time_at(self, x: float) -> datetime:
        return self.origin + timedelta(minutes=(x - LABEL_WIDTH) / self.px_per_minute)

    def _bars(self, row: PackageRow) -> list[FlightBar]:
        if row is self._drag_row and self._drag_delta:
            return [f.shifted(self._drag_delta) for f in row.flights]
        return row.flights

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BG)
        visible = event.rect()
        self._paint_grid(painter, visible)
        self._paint_window(painter)
        for kind, item, top, height in self.layout_.items:
            if top > visible.bottom() or top + height < visible.top():
                continue
            if kind == "header":
                self._paint_header(painter, str(item), top)
            else:
                assert isinstance(item, PackageRow)
                self._paint_row(painter, item, top, height)
        self._paint_markers(painter)
        self._paint_axis(painter, visible)
        painter.end()

    def _paint_grid(self, painter: QPainter, visible: QRect) -> None:
        painter.setPen(QPen(GRID, 1))
        step = self._grid_step()
        t = self.origin
        while t <= self.end + timedelta(minutes=30):
            x = self.x_for(t)
            if visible.left() - 2 <= x <= visible.right() + 2:
                painter.drawLine(QPointF(x, AXIS_HEIGHT), QPointF(x, self.height()))
            t += step

    def _grid_step(self) -> timedelta:
        for minutes in (5, 10, 15, 30, 60, 120):
            if minutes * self.px_per_minute >= 36:
                return timedelta(minutes=minutes)
        return timedelta(hours=4)

    def _paint_axis(self, painter: QPainter, visible: QRect) -> None:
        painter.fillRect(QRect(0, 0, self.width(), AXIS_HEIGHT), BG.darker(115))
        font = QFont(self.font())
        font.setPixelSize(11)
        painter.setFont(font)
        step = max(self._grid_step(), timedelta(minutes=15))
        if step.total_seconds() / 60 * self.px_per_minute < 70:
            step = timedelta(hours=1)
        t = self.origin
        start = self.mission_window[0] if self.mission_window else self.origin
        while t <= self.end + timedelta(minutes=30):
            x = self.x_for(t)
            if visible.left() - 80 <= x <= visible.right() + 2:
                hours = (t - start).total_seconds() / 3600
                painter.setPen(TEXT)
                painter.drawText(QPointF(x + 3, AXIS_HEIGHT - 8), t.strftime("%H:%M"))
                painter.setPen(DIM_TEXT)
                painter.drawText(QPointF(x + 40, AXIS_HEIGHT - 8), f"{hours:+.1f}h")
            t += step
        # Fixed label column on top of everything that scrolls under it.
        painter.fillRect(QRect(0, 0, LABEL_WIDTH, AXIS_HEIGHT), BG.darker(115))

    def _paint_window(self, painter: QPainter) -> None:
        if self.mission_window is None:
            return
        left, right = (self.x_for(t) for t in self.mission_window)
        painter.fillRect(
            QRectF(left, AXIS_HEIGHT, right - left, self.height()), WINDOW_FILL
        )
        painter.setPen(QPen(WINDOW_EDGE, 1))
        painter.drawLine(QPointF(left, AXIS_HEIGHT), QPointF(left, self.height()))
        painter.drawLine(QPointF(right, AXIS_HEIGHT), QPointF(right, self.height()))

    def _paint_markers(self, painter: QPainter) -> None:
        font = QFont(self.font())
        font.setPixelSize(11)
        painter.setFont(font)
        if self.mission_window is not None:
            painter.setPen(WINDOW_EDGE)
            painter.drawText(
                QPointF(self.x_for(self.mission_window[0]) + 4, AXIS_HEIGHT + 12),
                "DCS window",
            )
        if self.push is not None:
            x = self.x_for(self.push)
            painter.setPen(QPen(PUSH, 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(x, AXIS_HEIGHT), QPointF(x, self.height()))
            painter.setPen(PUSH)
            painter.drawText(QPointF(x + 4, AXIS_HEIGHT + 24), "Main push")
        if self.now is not None:
            x = self.x_for(self.now)
            painter.setPen(QPen(NOW_LINE, 2))
            painter.drawLine(QPointF(x, AXIS_HEIGHT), QPointF(x, self.height()))

    def _paint_header(self, painter: QPainter, name: str, top: int) -> None:
        color = QColor(CATEGORY_COLORS.get(name, CATEGORY_COLORS["Other"]))
        color.setAlpha(60)
        painter.fillRect(QRect(0, top, self.width(), HEADER_HEIGHT), color)
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(TEXT)
        painter.drawText(
            QRect(8, top, LABEL_WIDTH, HEADER_HEIGHT),
            Qt.AlignmentFlag.AlignVCenter,
            name.upper(),
        )

    def _paint_row(
        self, painter: QPainter, row: PackageRow, top: int, height: int
    ) -> None:
        color = _color(row)
        painter.setPen(QPen(GRID, 1))
        painter.drawLine(0, top + height, self.width(), top + height)
        for i, bar in enumerate(self._bars(row)):
            y = top + ROW_PADDING + i * FLIGHT_HEIGHT + FLIGHT_HEIGHT // 2
            self._paint_bar(painter, bar, y, color, row)
        # Label column, drawn last so bars scroll under it.
        painter.fillRect(QRect(0, top, LABEL_WIDTH, height), BG)
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setBold(row.player)
        painter.setFont(font)
        painter.setPen(TEXT if row.blue else DIM_TEXT)
        label = row.label if row.blue else f"OPFOR: {row.label}"
        painter.drawText(
            QRect(14, top, LABEL_WIDTH - 18, height),
            Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
            label,
        )

    def _paint_bar(
        self, painter: QPainter, bar: FlightBar, y: int, color: QColor, row: PackageRow
    ) -> None:
        # Start-up and taxi: a thin line up to take-off.
        if bar.startup is not None and bar.takeoff is not None:
            painter.setPen(QPen(color, 2))
            painter.drawLine(
                QPointF(self.x_for(bar.startup), y), QPointF(self.x_for(bar.takeoff), y)
            )
        # Airborne.
        if bar.takeoff is not None and bar.landing is not None:
            rect = QRectF(
                self.x_for(bar.takeoff),
                y - 5,
                max(2.0, self.x_for(bar.landing) - self.x_for(bar.takeoff)),
                10,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawRoundedRect(rect, 3, 3)
            if bar.player:
                painter.setPen(QPen(PLAYER, 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 3, 3)
        # On station: a brighter band.
        if bar.on_station is not None and bar.off_station is not None:
            band = QColor(color).lighter(150)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(band, Qt.BrushStyle.Dense4Pattern))
            painter.drawRect(
                QRectF(
                    self.x_for(bar.on_station),
                    y - 3,
                    max(2.0, self.x_for(bar.off_station) - self.x_for(bar.on_station)),
                    6,
                )
            )
        # Time over target.
        if bar.tot is not None:
            x = self.x_for(bar.tot)
            diamond = QPolygonF(
                [
                    QPointF(x, y - 7),
                    QPointF(x + 7, y),
                    QPointF(x, y + 7),
                    QPointF(x - 7, y),
                ]
            )
            painter.setPen(QPen(QColor("#1A1F26"), 1))
            painter.setBrush(QColor("white") if row.blue else QColor("#A0A8B0"))
            painter.drawPolygon(diamond)

    # -- interaction -------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
            self.zoom(factor, event.position().x())
            event.accept()
            return
        super().wheelEvent(event)

    def _row_under(self, pos: QPoint) -> Optional[PackageRow]:
        found = self.layout_.row_at(pos.y())
        if found is None:
            return None
        return found[0]

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        row = self._row_under(pos)
        if row is None or not row.editable or pos.x() < LABEL_WIDTH:
            return
        if row.package.auto_asap or row.package.auto_coordinated:
            QToolTip.showText(
                event.globalPosition().toPoint(),
                "This package's time is automatic (ASAP or coordinated). "
                "Dragging it switches that off.",
                self,
            )
        self._drag_row = row
        self._drag_start_x = pos.x()
        self._drag_delta = timedelta()
        self.setCursor(Qt.CursorShape.SizeHorCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if self._drag_row is not None:
            minutes = (pos.x() - self._drag_start_x) / self.px_per_minute
            self._drag_delta = snap(timedelta(minutes=minutes))
            new_tot = self._drag_row.package.time_over_target + self._drag_delta
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"TOT {new_tot:%H:%M}  ({self._signed(self._drag_delta)})",
                self,
            )
            self.update()
            return
        row = self._row_under(pos)
        if row is not None:
            QToolTip.showText(
                event.globalPosition().toPoint(), self._describe(row), self
            )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        row, delta = self._drag_row, self._drag_delta
        self._drag_row = None
        self._drag_delta = timedelta()
        self.unsetCursor()
        if row is not None and delta:
            self.tot_moved.emit(row.package, row.package.time_over_target + delta)
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        row = self._row_under(event.position().toPoint())
        if row is not None and row.editable:
            self.package_opened.emit(row.package)

    @staticmethod
    def _signed(delta: timedelta) -> str:
        minutes = int(delta.total_seconds() // 60)
        return f"{minutes:+d} min"

    @staticmethod
    def _describe(row: PackageRow) -> str:
        lines = [row.label if row.blue else f"OPFOR: {row.label}"]
        tot = row.package.time_over_target
        if tot > datetime.min:
            lines.append(f"Package TOT {tot:%H:%M}")
        for bar in row.flights:
            times = []
            if bar.startup is not None:
                times.append(f"start-up {bar.startup:%H:%M}")
            if bar.takeoff is not None:
                times.append(f"take-off {bar.takeoff:%H:%M}")
            if bar.tot is not None:
                times.append(f"TOT {bar.tot:%H:%M}")
            if bar.landing is not None:
                times.append(f"landing {bar.landing:%H:%M}")
            player = " (players)" if bar.player else ""
            lines.append(f"• {bar.name}{player}: " + ", ".join(times))
        if row.blue:
            lines.append("Drag to move the TOT, double-click to open.")
        return "\n".join(lines)


class AtoTimelineWindow(QDialog):
    def __init__(self, game_model: GameModel, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.game_model = game_model
        self.setWindowTitle("ATO timeline")
        self.setMinimumSize(900, 480)
        self.resize(1250, 760)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)

        self.canvas = TimelineCanvas()
        top = QHBoxLayout()
        self.opfor = QCheckBox("Show OPFOR's ATO")
        self.opfor.toggled.connect(self.refresh)
        top.addWidget(self.opfor)
        top.addStretch()
        legend = QLabel(
            "— start-up   ▬ airborne   ◆ TOT   ░ on station   "
            "green outline: player flight   shaded: DCS window   ·   "
            "Ctrl+wheel to zoom, drag to move a package, double-click to edit"
        )
        legend.setStyleSheet("color: #8C9BAA;")
        top.addWidget(legend)
        for text, slot in (
            ("−", lambda: self.canvas.zoom(1 / 1.3, self._centre_x())),
            ("+", lambda: self.canvas.zoom(1.3, self._centre_x())),
            ("Fit", self.fit),
        ):
            button = style_button(QPushButton(text))
            button.setFixedWidth(44)
            button.clicked.connect(slot)
            top.addWidget(button)

        self.canvas.tot_moved.connect(self.move_tot)
        self.canvas.package_opened.connect(self.open_package)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidget(self.canvas)
        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setStyleSheet("QScrollArea { border: 1px solid #33414F; }")

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.scroll_area, 1)

        signal = GameUpdateSignal.get_instance()
        signal.gameupdated.connect(lambda _game: self.refresh())
        signal.ato_changed.connect(self.refresh)
        self.game_model.ato_model.layoutChanged.connect(self.refresh)
        self.game_model.ato_model.rowsInserted.connect(self.refresh)
        self.game_model.ato_model.rowsRemoved.connect(self.refresh)
        self.refresh()
        self.fit()

    def _centre_x(self) -> float:
        bar = self.scroll_area.horizontalScrollBar()
        return bar.value() + self.scroll_area.viewport().width() / 2

    def fit(self) -> None:
        self.canvas.fit(self.scroll_area.viewport().width())

    def refresh(self) -> None:
        game = self.game_model.game
        if game is None:
            self.canvas.set_rows([], datetime.now(), (datetime.now(),) * 2, None)
            return
        try:
            rows = build_rows(game, self.opfor.isChecked())
            now = self.game_model.sim_controller.current_time_in_sim
        except Exception:
            logging.exception("Could not build the ATO timeline")
            return
        self.canvas.set_rows(rows, now, dcs_window(game, rows), main_push(rows))

    def move_tot(self, package: object, tot: datetime) -> None:
        model = self.game_model.ato_model.find_matching_package_model(package)  # type: ignore[arg-type]
        if model is None:
            return
        # A time set by hand replaces ASAP and coordinated timing, as in the
        # package dialog.
        package_ = model.package
        package_.auto_coordinated = False
        model.set_asap(False)
        model.set_tot(tot)
        model.push_tot_if_unreachable()
        EventStream.put_nowait(GameUpdateEvents().update_flights_in_package(package_))
        self.refresh()

    def open_package(self, package: object) -> None:
        from qt_ui.dialogs import Dialog

        model = self.game_model.ato_model.find_matching_package_model(package)  # type: ignore[arg-type]
        if model is not None:
            Dialog.open_edit_package_dialog(model)
