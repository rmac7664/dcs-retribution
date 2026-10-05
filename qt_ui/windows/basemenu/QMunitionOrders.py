"""Window for ordering munitions by hand (Logistics tab → Order munitions)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from game import Game
from game.theater import ControlPoint
from game.warehouse.munitions import ammo_only
from game.warehouse.state import short_name
from game.warehouse.supply import (
    MunitionPrices,
    SupplyPlanner,
    Suggestion,
    manual_purchasing,
    ordered_at,
    set_order,
)
from qt_ui.windows.GameUpdateSignal import GameUpdateSignal

COLUMNS = ["Munition", "At depot", "Suggested", "Unit price", "Order"]


class QMunitionOrders(QDialog):
    """Lets the player pick what each depot buys this turn.

    Orders are paid when placed and arrive at the depot at the end of the turn, then
    ship forward on supply runs like anything else the depot holds.
    """

    def __init__(
        self, game: Game, cp: ControlPoint, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.game = game
        self.coalition = game.blue
        self.setWindowTitle("Order munitions")
        self.setMinimumSize(720, 560)
        self.spins: dict[str, QSpinBox] = {}
        self.planner = SupplyPlanner(game, self.coalition)

        layout = QVBoxLayout()
        intro = QLabel(
            "Pick what this depot buys. Orders are paid now and arrive at the depot "
            "at the end of the turn, then go forward on supply runs to the bases it "
            "serves."
            + (
                ""
                if manual_purchasing(game, self.coalition)
                else " The computer still buys what's short automatically at turn "
                "end; your orders come on top of that."
            )
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        top = QHBoxLayout()
        top.addWidget(QLabel("Depot:"))
        self.depot_box = QComboBox()
        for depot in self.planner.depots:
            self.depot_box.addItem(depot.name, depot)
        top.addWidget(self.depot_box, 1)
        self.show_all = QCheckBox("Show every munition your aircraft use")
        top.addWidget(self.show_all)
        layout.addLayout(top)

        self.serves = QLabel()
        self.serves.setWordWrap(True)
        layout.addWidget(self.serves)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        self.total = QLabel()
        layout.addWidget(self.total)

        buttons = QHBoxLayout()
        self.suggest_button = QPushButton("Use suggestions")
        self.suggest_button.setToolTip(
            "Order what the computer would have bought for this depot."
        )
        self.clear_button = QPushButton("Clear")
        self.place_button = QPushButton("Place orders")
        self.place_button.setProperty("style", "btn-success")
        close_button = QPushButton("Close")
        buttons.addWidget(self.suggest_button)
        buttons.addWidget(self.clear_button)
        buttons.addStretch()
        buttons.addWidget(self.place_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.setLayout(layout)

        start = self._starting_depot(cp)
        if start is not None:
            self.depot_box.setCurrentIndex(self.planner.depots.index(start))
        self.depot_box.currentIndexChanged.connect(lambda _: self._fill())
        self.show_all.toggled.connect(lambda _: self._fill())
        self.suggest_button.clicked.connect(self._use_suggestions)
        self.clear_button.clicked.connect(self._clear)
        self.place_button.clicked.connect(self._place)
        close_button.clicked.connect(self.accept)
        self._fill()

    # Data -----------------------------------------------------------------------

    def _starting_depot(self, cp: ControlPoint) -> Optional[ControlPoint]:
        if cp in self.planner.depots:
            return cp
        return self.planner.served_by.get(cp.id)

    @property
    def depot(self) -> Optional[ControlPoint]:
        return self.depot_box.currentData()

    def _served(self, depot: ControlPoint) -> list[ControlPoint]:
        return [
            cp
            for cp in self.planner.bases
            if cp is not depot and self.planner.served_by.get(cp.id) is depot
        ]

    def _suggested(self, depot: ControlPoint) -> dict[str, Suggestion]:
        return {s.resource: s for s in self.planner.suggestions() if s.depot is depot}

    def _price(self, resource: str) -> float:
        if not self.game.settings.logistics_munitions_cost:
            return 0.0
        return MunitionPrices.price(resource, self.game.settings)

    # Table ----------------------------------------------------------------------

    def _fill(self) -> None:
        depot = self.depot
        self.spins.clear()
        self.table.setRowCount(0)
        if depot is None:
            self.serves.setText("Your side has no depots to order munitions at.")
            self._update_total()
            return

        served = self._served(depot)
        self.serves.setText(
            "Supplies: " + ", ".join(cp.name for cp in served)
            if served
            else "Supplies only itself."
        )
        suggested = self._suggested(depot)
        ordered = ordered_at(self.game.warehouse_logistics, depot)
        stock = self.game.warehouse_logistics.ensure_stock(self.game, depot).munitions

        names = set(suggested) | set(ordered)
        for cp in [depot, *served]:
            names |= set(self.planner.authorized.get(cp.id, {}))
        if self.show_all.isChecked():
            for authorized in self.planner.authorized.values():
                names |= set(authorized)
            names |= set(ammo_only(stock))
        rows = sorted(
            names,
            key=lambda n: (
                -(suggested[n].urgency if n in suggested else -1),
                short_name(n),
            ),
        )

        self.table.setRowCount(len(rows))
        for row, name in enumerate(rows):
            label = QTableWidgetItem(short_name(name))
            label.setToolTip(name)
            self.table.setItem(row, 0, label)
            self.table.setItem(row, 1, self._number(stock.get(name, 0)))
            suggestion = suggested.get(name)
            self.table.setItem(
                row, 2, self._number(suggestion.quantity if suggestion else 0)
            )
            price = self._price(name)
            self.table.setItem(
                row,
                3,
                self._text(f"${price:,.3f}M" if price else "free"),
            )
            spin = QSpinBox()
            spin.setRange(0, 99_999)
            spin.setValue(ordered.get(name, 0))
            spin.valueChanged.connect(lambda _: self._update_total())
            self.table.setCellWidget(row, 4, spin)
            self.spins[name] = spin
        self._update_total()

    @staticmethod
    def _number(value: int) -> QTableWidgetItem:
        item = QTableWidgetItem(f"{value:,}" if value else "–")
        item.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        return item

    @staticmethod
    def _text(value: str) -> QTableWidgetItem:
        item = QTableWidgetItem(value)
        item.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        return item

    def _change(self) -> float:
        """Money this depot's edits would cost (negative for a refund)."""
        depot = self.depot
        if depot is None:
            return 0.0
        orders = self.game.warehouse_logistics.orders.get(depot.id, {})
        change = 0.0
        for name, spin in self.spins.items():
            order = orders.get(name)
            had = order.count if order else 0
            want = spin.value()
            if want > had:
                change += (want - had) * self._price(name)
            elif want < had and order is not None:
                change -= order.paid * (had - want) / had
        return change

    def _update_total(self) -> None:
        change = self._change()
        budget = self.coalition.budget
        verb = "cost" if change >= 0 else "refund"
        text = f"Budget: ${budget:,.1f}M · These changes {verb} ${abs(change):,.2f}M"
        if change > budget:
            text += " — <span style='color:#e06c6c'>not enough money</span>"
        self.total.setText(text)
        self.place_button.setEnabled(change <= budget and self._has_edits())

    def _has_edits(self) -> bool:
        depot = self.depot
        if depot is None:
            return False
        ordered = ordered_at(self.game.warehouse_logistics, depot)
        return any(spin.value() != ordered.get(n, 0) for n, spin in self.spins.items())

    # Actions --------------------------------------------------------------------

    def _use_suggestions(self) -> None:
        depot = self.depot
        if depot is None:
            return
        ordered = ordered_at(self.game.warehouse_logistics, depot)
        for name, suggestion in self._suggested(depot).items():
            spin = self.spins.get(name)
            if spin is not None:
                spin.setValue(ordered.get(name, 0) + suggestion.quantity)

    def _clear(self) -> None:
        for spin in self.spins.values():
            spin.setValue(0)

    def _place(self) -> None:
        depot = self.depot
        if depot is None:
            return
        # Refunds first, so money freed by cutting one order can pay for another.
        edits = sorted(
            self.spins.items(),
            key=lambda item: item[1].value()
            - ordered_at(self.game.warehouse_logistics, depot).get(item[0], 0),
        )
        try:
            for name, spin in edits:
                set_order(self.game, depot, name, spin.value())
        except ValueError as error:
            QMessageBox.warning(self, "Order munitions", str(error))
        GameUpdateSignal.get_instance().updateBudget(self.game)
        # Suggestions shrink as orders are placed.
        self.planner = SupplyPlanner(self.game, self.coalition)
        self._fill()
