"""Dialog for choosing a strike package's additional targets."""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from game import weaponeering
from game.ato import multitarget
from game.ato.package import Package
from game.game import Game
from game.theater.missiontarget import MissionTarget
from game.utils import meters
from qt_ui.widgets.cards import caption
from qt_ui.widgets.controls import button, styled_input, value_label

TARGET_ROLE = Qt.ItemDataRole.UserRole


def summary(package: Package) -> str:
    """One line naming the additional targets, for the package dialog."""
    names = [target.name for target in package.additional_targets]
    if not names:
        return "None. The package attacks its main target only."
    return "Then: " + ", then ".join(names)


def describe(target: MissionTarget) -> str:
    """ "BUTTERFLY: Ammo depot at Aleppo", or just the name when that is all there is."""
    try:
        kind = str(target)
    except KeyError:
        kind = ""
    base = getattr(getattr(target, "control_point", None), "name", "")
    if not kind or kind == target.name:
        return target.name
    return f"{target.name}: {kind} at {base}" if base else f"{target.name}: {kind}"


class AdditionalTargetsDialog(QDialog):
    """Picks, orders and removes the targets a strike flies on to.

    Nothing is changed until the dialog is accepted; read ``targets`` then.
    """

    def __init__(
        self, game: Game, package: Package, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.game = game
        self.package = package
        self.targets: list[MissionTarget] = list(package.additional_targets)
        self.setWindowTitle(f"Additional targets: {package.target.name}")
        self.resize(820, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        hint = value_label(
            "Strike flights attack the main target first, then each target below "
            "in order, and then turn for home. Each flight keeps part of its "
            "weapons for the later targets. Escorts stay with the strike flights."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        columns = QHBoxLayout()
        columns.setSpacing(12)
        layout.addLayout(columns, 1)

        chosen_column = QVBoxLayout()
        chosen_column.addWidget(caption("Attack order"))
        self.chosen = QListWidget()
        self.chosen.currentRowChanged.connect(self._update_buttons)
        chosen_column.addWidget(self.chosen, 1)
        chosen_buttons = QHBoxLayout()
        self.up_button = button("Move up", handler=lambda: self._move(-1))
        self.down_button = button("Move down", handler=lambda: self._move(1))
        self.remove_button = button("Remove", handler=self._remove)
        for control in (self.up_button, self.down_button, self.remove_button):
            control.setAutoDefault(False)
            chosen_buttons.addWidget(control)
        chosen_buttons.addStretch()
        chosen_column.addLayout(chosen_buttons)
        columns.addLayout(chosen_column, 1)

        available_column = QVBoxLayout()
        available_column.addWidget(
            caption("Available targets, nearest to the last one first")
        )
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by name, type or base")
        self.search.textChanged.connect(self._fill_available)
        available_column.addWidget(styled_input(self.search))
        self.available = QListWidget()
        self.available.itemDoubleClicked.connect(lambda _item: self._add())
        self.available.currentRowChanged.connect(self._update_buttons)
        available_column.addWidget(self.available, 1)
        available_buttons = QHBoxLayout()
        self.add_button = button("Add to attack order", "primary", self._add)
        self.add_button.setAutoDefault(False)
        available_buttons.addWidget(self.add_button)
        available_buttons.addStretch()
        available_column.addLayout(available_buttons)
        columns.addLayout(available_column, 1)

        self.weapons_label = value_label("")
        self.weapons_label.setWordWrap(True)
        self.weapons_label.setToolTip(
            "A planning estimate from resources/weaponeering.yaml, counting "
            "everything still alive at the main target and the targets added."
        )
        layout.addWidget(self.weapons_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._fill_chosen()
        self._fill_available()

    def _candidates(self) -> list[MissionTarget]:
        """Targets not yet chosen, nearest to the last chosen target first."""
        taken = [self.package.target, *self.targets]
        origin = taken[-1].position
        found: list[MissionTarget] = [
            target
            for target in multitarget.candidates(self.package, self.game)
            if not any(target is other for other in taken)
        ]
        found.sort(key=lambda target: target.position.distance_to_point(origin))
        return found

    def _fill_chosen(self, select: int = -1) -> None:
        self.chosen.clear()
        first = QListWidgetItem(f"1. {describe(self.package.target)} (main target)")
        first.setFlags(Qt.ItemFlag.NoItemFlags)
        self.chosen.addItem(first)
        for number, target in enumerate(self.targets, start=2):
            item = QListWidgetItem(f"{number}. {describe(target)}")
            item.setData(TARGET_ROLE, target)
            self.chosen.addItem(item)
        if 0 <= select < len(self.targets):
            self.chosen.setCurrentRow(select + 1)
        self._update_buttons()
        self._update_weapons()

    def _update_weapons(self) -> None:
        try:
            text = weaponeering.summary_for_flights(
                multitarget.strike_flights(self.package),
                [self.package.target, *self.targets],
                self.game,
            )
        except Exception:
            logging.exception("Could not estimate the weapons required")
            text = ""
        self.weapons_label.setText(text)

    def _fill_available(self) -> None:
        wanted = self.search.text().strip().lower()
        origin = [self.package.target, *self.targets][-1].position
        self.available.clear()
        for target in self._candidates():
            if wanted and wanted not in describe(target).lower():
                continue
            distance = meters(target.position.distance_to_point(origin))
            item = QListWidgetItem(
                f"{describe(target)}  ({distance.nautical_miles:.0f} NM)"
            )
            item.setData(TARGET_ROLE, target)
            self.available.addItem(item)
        self._update_buttons()

    def _selected_index(self) -> int:
        """Index into ``targets`` of the selected row, or -1."""
        row = self.chosen.currentRow()
        return row - 1 if row >= 1 else -1

    def _update_buttons(self) -> None:
        index = self._selected_index()
        self.remove_button.setEnabled(index >= 0)
        self.up_button.setEnabled(index > 0)
        self.down_button.setEnabled(0 <= index < len(self.targets) - 1)
        self.add_button.setEnabled(self.available.currentRow() >= 0)

    def _add(self) -> None:
        row = self.available.currentRow()
        if row < 0:
            return
        self.targets.append(self.available.item(row).data(TARGET_ROLE))
        self._fill_chosen(len(self.targets) - 1)
        self._fill_available()

    def _remove(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        del self.targets[index]
        self._fill_chosen(min(index, len(self.targets) - 1))
        self._fill_available()

    def _move(self, step: int) -> None:
        index = self._selected_index()
        other = index + step
        if index < 0 or not 0 <= other < len(self.targets):
            return
        self.targets[index], self.targets[other] = (
            self.targets[other],
            self.targets[index],
        )
        self._fill_chosen(other)
        self._fill_available()
