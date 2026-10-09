"""Every base's supply situation on one page."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from game import Game
from game.warehouse.overview import BaseRow, overview
from game.warehouse.priority import set_priority

COLUMNS = (
    "Base",
    "Role",
    "Munitions",
    "Fuel",
    "Short of",
    "Inbound",
    "At risk",
    "Priority",
)


def level_color(percent: int) -> QColor:
    if percent >= 66:
        return QColor("#3fb950")
    if percent >= 33:
        return QColor("#d29922")
    return QColor("#f85149")


class QLogisticsOverview(QDialog):
    """All of the player's bases: stock, shortages, inbound supply and risks."""

    def __init__(self, game: Game, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.game = game
        self.setWindowTitle("Logistics overview")
        self.setMinimumSize(1100, 520)

        layout = QVBoxLayout()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        hint = QLabel(
            "Worst-supplied bases first. Priority bases have their shortfalls bought "
            "first and are first in line for their depot's spare stock. Sort out a "
            "base's supply runs (or send them by air) in its Logistics tab."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(refresh)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.setLayout(layout)
        self.refresh()

    def refresh(self) -> None:
        data = overview(self.game, self.game.blue.player)
        summary = data.summary or ["Nothing to report from last turn yet."]
        self.summary.setText("<b>Last turn:</b> " + " ".join(summary))
        self.table.setRowCount(len(data.rows))
        for row, base in enumerate(data.rows):
            self._fill(row, base)

    def _fill(self, row: int, base: BaseRow) -> None:
        def cell(column: int, text: str, tooltip: str = "") -> QTableWidgetItem:
            item = QTableWidgetItem(text)
            if tooltip:
                item.setToolTip(tooltip)
            self.table.setItem(row, column, item)
            return item

        cell(0, base.name)
        cell(1, base.role)
        munitions = cell(2, f"{base.munitions_percent}%")
        munitions.setForeground(level_color(base.munitions_percent))
        fuel = cell(3, f"{base.fuel_percent}% ({base.fuel_tons:,.0f} t)")
        fuel.setForeground(level_color(base.fuel_percent))
        short = base.short_of
        cell(
            4,
            ", ".join(short[:4])
            + (f" +{len(short) - 4} more" if len(short) > 4 else ""),
            "\n".join(short),
        )
        cell(5, base.inbound or "—")
        risk = cell(6, ", ".join(base.risks) or "—")
        if base.risks:
            risk.setForeground(QColor("#d29922"))

        box = QCheckBox()
        box.setChecked(base.priority)
        box.setEnabled(base.role != "Rear area")
        cp = base.cp
        box.toggled.connect(lambda on, cp=cp: set_priority(self.game, cp, on))
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.addWidget(box)
        holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        self.table.setCellWidget(row, 7, holder)
