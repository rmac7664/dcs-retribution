"""Asks the player to pick their main supply base before the campaign begins."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from game import Game
from game.theater import ControlPoint
from game.warehouse.supply import (
    METERS_PER_NM,
    distance_to_enemy,
    enemy_positions,
    main_base,
    main_base_options,
    main_base_warnings,
    set_main_base,
)


def _enemy_distance_nm(game: Game, cp: ControlPoint) -> float:
    enemy = enemy_positions(game, cp.captured)
    return distance_to_enemy(cp, enemy) / METERS_PER_NM


class QMainBaseDialog(QDialog):
    """Lists the bases that may be the main supply base, with warnings."""

    def __init__(self, game: Game, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.game = game
        self.player = game.blue.player
        self.options = main_base_options(game, self.player)
        self.setWindowTitle("Choose your main supply base")
        self.setMinimumWidth(520)

        layout = QVBoxLayout()
        intro = QLabel(
            "Your main supply base is where new supplies arrive and where "
            "replenishment ships sail from. It can't be changed once the campaign "
            "begins (unless the cheat is enabled), and if it is lost a new one is "
            "picked for you.<br><br>Only bases you have held since the start and that "
            "are in the safer half of your territory are listed. The first is the "
            "base farthest from the enemy."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.list = QListWidget()
        current = main_base(game, self.player)
        for cp in self.options:
            text = f"{cp.name} — {_enemy_distance_nm(game, cp):.0f} nm from the enemy"
            warnings = main_base_warnings(game, cp)
            if warnings:
                text += "  ⚠ " + "; ".join(warnings)
            item = QListWidgetItem(text)
            self.list.addItem(item)
            if cp is current:
                self.list.setCurrentItem(item)
        if self.list.currentRow() < 0 and self.options:
            self.list.setCurrentRow(0)
        layout.addWidget(self.list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Begin Campaign")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def accept(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.options):
            set_main_base(self.game, self.player, self.options[row])
        super().accept()

    @staticmethod
    def needed(game: Game) -> bool:
        return (
            game.turn == 0
            and game.settings.logistics_enabled
            and len(main_base_options(game, game.blue.player)) > 0
        )
