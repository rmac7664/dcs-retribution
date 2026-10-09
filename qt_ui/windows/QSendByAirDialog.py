"""Switching a ship or convoy's munitions to air delivery (game/warehouse/airswitch.py)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from game import Game
from game.transfers import TransferOrder
from game.warehouse.airswitch import (
    AirOption,
    Shipment,
    air_options,
    can_undo,
    describe_shipment,
    send_by_air,
    undo_send_by_air,
)
from qt_ui.windows.GameUpdateSignal import GameUpdateSignal


class QSendByAirDialog(QDialog):
    """Picks a transport squadron to fly a ship or convoy's munitions."""

    def __init__(
        self, game: Game, shipment: Shipment, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.game = game
        self.shipment = shipment
        self.options = air_options(game, shipment)
        self.setWindowTitle("Send by air")
        self.setMinimumWidth(640)

        layout = QVBoxLayout()
        intro = QLabel(
            f"<b>{describe_shipment(game, shipment)}</b><br><br>"
            "Fly the munitions in with transport aircraft this turn instead. Fuel "
            "stays with the ship or convoy. If the aircraft can't take everything, the "
            "most urgent items fly first and the rest stays on board. The flights join "
            "this turn's ATO; cargo is lost if they are shot down. You can undo this "
            "until the mission starts."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.list = QListWidget()
        for option in self.options:
            item = QListWidgetItem(option.describe())
            if option.problem is not None:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.list.addItem(item)
        usable = [i for i, o in enumerate(self.options) if o.problem is None]
        if usable:
            self.list.setCurrentRow(usable[0])
        else:
            none = QLabel(
                "No transport squadron can fly this cargo this turn."
                if self.options
                else "This side has no transport squadrons."
            )
            none.setStyleSheet("color: #e06c6c;")
            layout.addWidget(none)
        layout.addWidget(self.list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Send by air")
        self.ok.setEnabled(bool(usable))
        self.list.currentRowChanged.connect(self._on_row)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def _selected(self) -> Optional[AirOption]:
        row = self.list.currentRow()
        if 0 <= row < len(self.options):
            return self.options[row]
        return None

    def _on_row(self, _row: int) -> None:
        option = self._selected()
        self.ok.setEnabled(option is not None and option.problem is None)

    def accept(self) -> None:
        option = self._selected()
        if option is None or option.problem is not None:
            return
        try:
            send_by_air(self.game, self.shipment, option)
        except ValueError as ex:
            QMessageBox.warning(self, "Can't send by air", str(ex))
            return
        _refresh(self.game)
        super().accept()


def undo_air_delivery(game: Game, transfer: TransferOrder, parent: QWidget) -> bool:
    """Asks, then puts an air delivery's cargo back on its ship or convoy."""
    if not can_undo(game, transfer):
        return False
    answer = QMessageBox.question(
        parent,
        "Undo air delivery",
        "Cancel the transport flights and put this cargo back on the ship or convoy?",
    )
    if answer != QMessageBox.StandardButton.Yes:
        return False
    try:
        undo_send_by_air(game, transfer)
    except ValueError as ex:
        QMessageBox.warning(parent, "Can't undo", str(ex))
        return False
    _refresh(game)
    return True


def _refresh(game: Game) -> None:
    """Redraws the ATO and map after flights were added or removed."""
    signal = GameUpdateSignal.get_instance()
    if signal is not None:
        signal.updateGame(game)
