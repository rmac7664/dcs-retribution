from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from game import Game
from game.theater import ControlPoint
from game.warehouse.munitions import ammo_only
from game.warehouse.state import KG_PER_TON, SupplyShip, short_name
from game.warehouse.supply import (
    MunitionPrices,
    SupplyPlanner,
    main_base,
    main_base_options,
    main_base_warnings,
    set_main_base,
    manual_purchasing,
    ordered_at,
    supply_of,
)


class QWarehouseInfo(QFrame):
    """Fuel and munition stock at a base when base logistics is enabled."""

    def __init__(self, cp: ControlPoint, game: Game) -> None:
        super().__init__()
        state = game.warehouse_logistics
        settings = game.settings
        stock = state.ensure_stock(game, cp)
        capacity = state.fuel_capacity_kg(cp, settings)
        authorized = state.authorized_munitions(game, cp)

        content = QVBoxLayout()

        if settings.logistics_supply_lines:
            if SupplyPlanner.is_depot(cp):
                role = (
                    "<b>Depot.</b> New munitions are bought here and shipped to the "
                    "bases it serves."
                )
            else:
                role = (
                    "Resupplied by convoy, ship or airlift from the nearest depot. "
                    "Destroying its supply runs starves it."
                )
            role_label = QLabel(role)
            role_label.setWordWrap(True)
            content.addWidget(role_label)

        if cp.captured.is_blue:
            main_row = QHBoxLayout()
            self.main_base_label = QLabel()
            self.main_base_label.setWordWrap(True)
            main_row.addWidget(self.main_base_label, 1)
            self.main_base_button = QPushButton()
            self.main_base_button.clicked.connect(
                lambda: self._make_main_base(cp, game)
            )
            main_row.addWidget(self.main_base_button)
            content.addLayout(main_row)
            self._show_main_base(cp, game)

        if cp.captured.is_blue:
            order_row = QHBoxLayout()
            self.order_label = QLabel(self._order_note(cp, game))
            self.order_label.setWordWrap(True)
            order_row.addWidget(self.order_label, 1)
            order_button = QPushButton("Order munitions…")
            order_button.clicked.connect(lambda: self._open_orders(cp, game))
            order_row.addWidget(order_button)
            content.addLayout(order_row)

        fuel_group = QGroupBox("Aviation fuel")
        fuel_layout = QVBoxLayout()
        fuel_bar = QProgressBar()
        fuel_bar.setRange(0, max(1, int(capacity / KG_PER_TON)))
        fuel_bar.setValue(int(min(capacity, stock.jet_fuel_kg) / KG_PER_TON))
        fuel_bar.setFormat(
            f"{stock.jet_fuel_kg / KG_PER_TON:,.0f} t of {capacity / KG_PER_TON:,.0f} t"
        )
        fuel_layout.addWidget(fuel_bar)
        fuel_group.setLayout(fuel_layout)
        content.addWidget(fuel_group)

        runs = [
            t
            for t in cp.coalition.transfers.pending_transfers
            if supply_of(t) is not None and cp in (t.origin, t.destination)
        ]
        ships = list(state.ships_bound_for(cp))
        if runs or ships:
            runs_group = QGroupBox("Supply runs")
            runs_layout = QVBoxLayout()
            for transfer in runs:
                runs_layout.addLayout(
                    self._shipment_row(
                        game, transfer, f"{transfer} — {transfer.description}"
                    )
                )
            for ship in ships:
                cargo = sum(ship.munitions.values())
                runs_layout.addLayout(
                    self._shipment_row(
                        game,
                        ship,
                        f"Replenishment ship at sea: {cargo} munitions, "
                        f"{ship.fuel_kg / KG_PER_TON:,.0f} t fuel",
                    )
                )
            runs_group.setLayout(runs_layout)
            content.addWidget(runs_group)

        self._add_depot_buildings(content, cp, game)

        show_price = settings.logistics_munitions_cost
        title = "Munitions (on hand / authorized" + (
            ", unit price)" if show_price else ")"
        )
        munitions_group = QGroupBox(title)
        grid = QGridLayout()
        names = sorted(
            set(ammo_only(stock.munitions)) | set(authorized),
            key=lambda n: (-stock.munitions.get(n, 0), short_name(n)),
        )
        if not names:
            grid.addWidget(QLabel("No limited munitions stocked here."), 0, 0)
        for row, name in enumerate(names):
            have = stock.munitions.get(name, 0)
            want = authorized.get(name, 0)
            label = QLabel(f"<b>{short_name(name)}</b>")
            label.setToolTip(name)
            grid.addWidget(label, row, 0)
            amount = QLabel(f"{have} / {want}" if want else str(have))
            if want and have < want / 4:
                amount.setStyleSheet("color: #e06c6c;")
            grid.addWidget(amount, row, 1)
            if show_price:
                price = MunitionPrices.price(name, settings)
                grid.addWidget(QLabel(f"${price:,.3f}M"), row, 2)
        munitions_group.setLayout(grid)
        content.addWidget(munitions_group)

        last = state.last_result
        if last is not None and cp.name in last.munitions_used:
            used = ", ".join(
                f"{short_name(k)} ×{v}"
                for k, v in sorted(last.munitions_used[cp.name].items())
            )
            used_label = QLabel(f"Expended last mission: {used}")
            used_label.setWordWrap(True)
            content.addWidget(used_label)

        content.addStretch()
        inner = QWidget()
        inner.setLayout(content)
        scroll = QScrollArea()
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        layout = QVBoxLayout()
        layout.addWidget(scroll)
        self.setLayout(layout)

    def _add_depot_buildings(
        self, content: QVBoxLayout, cp: ControlPoint, game: Game
    ) -> None:
        """Ammo/fuel depots, factories and warehouses, with rebuilding."""
        from game.warehouse.rebuild import (
            REBUILD_COST,
            REBUILD_TURNS,
            can_rebuild,
            depot_buildings,
            turns_left,
        )

        buildings = depot_buildings(cp)
        if not buildings:
            return
        group = QGroupBox("Supply depot buildings")
        layout = QVBoxLayout()
        for tgo in buildings:
            row = QHBoxLayout()
            left = turns_left(game, tgo)
            if not tgo.is_dead:
                status = "standing"
            elif left is not None:
                status = f"rebuilding, {left} turn(s) left"
            else:
                status = "destroyed"
            label = QLabel(f"{tgo.name} ({tgo.category}): {status}")
            row.addWidget(label, 1)
            if cp.captured.is_blue and can_rebuild(game, tgo):
                button = QPushButton(
                    f"Rebuild (${REBUILD_COST}M, {REBUILD_TURNS} turns)"
                )
                button.clicked.connect(
                    lambda _=False, tgo=tgo, label=label, button=button: (
                        self._rebuild(game, tgo, label, button)
                    )
                )
                row.addWidget(button)
            layout.addLayout(row)
        group.setLayout(layout)
        content.addWidget(group)

    def _rebuild(
        self, game: Game, tgo: Any, label: QLabel, button: QPushButton
    ) -> None:
        from game.warehouse.rebuild import REBUILD_TURNS, begin_rebuild
        from qt_ui.windows.GameUpdateSignal import GameUpdateSignal

        try:
            begin_rebuild(game, tgo)
        except ValueError as ex:
            QMessageBox.warning(self, "Can't rebuild", str(ex))
            return
        label.setText(
            f"{tgo.name} ({tgo.category}): rebuilding, {REBUILD_TURNS} turn(s) left"
        )
        button.setEnabled(False)
        signal = GameUpdateSignal.get_instance()
        if signal is not None:
            signal.updateBudget(game)

    def _shipment_row(self, game: Game, shipment: Any, text: str) -> QHBoxLayout:
        """A supply run or ship, with "Send by air…" or "Undo air delivery"."""
        from game.warehouse.airswitch import can_undo, switchable

        row = QHBoxLayout()
        line = QLabel(text)
        line.setWordWrap(True)
        row.addWidget(line, 1)
        if isinstance(shipment, SupplyShip):
            if shipment.side != "BLUE":
                return row
        elif not shipment.player.is_blue:
            return row
        if switchable(game, shipment):
            button = QPushButton("Send by air…")
            button.clicked.connect(
                lambda: self._send_by_air(game, shipment, line, button)
            )
            row.addWidget(button)
        elif not isinstance(shipment, SupplyShip) and can_undo(game, shipment):
            button = QPushButton("Undo air delivery")
            button.clicked.connect(lambda: self._undo_air(game, shipment, line, button))
            row.addWidget(button)
        return row

    def _send_by_air(
        self, game: Game, shipment: Any, line: QLabel, button: QPushButton
    ) -> None:
        from qt_ui.windows.QSendByAirDialog import QSendByAirDialog

        if QSendByAirDialog(game, shipment, self).exec():
            line.setText(line.text() + " — munitions sent by air")
            button.setEnabled(False)

    def _undo_air(
        self, game: Game, transfer: Any, line: QLabel, button: QPushButton
    ) -> None:
        from qt_ui.windows.QSendByAirDialog import undo_air_delivery

        if undo_air_delivery(game, transfer, self):
            line.setText(line.text() + " — back on the ship or convoy")
            button.setEnabled(False)

    @staticmethod
    def can_change_main_base(game: Game) -> bool:
        """Picked before the campaign begins; after that only with the cheat on."""
        return game.turn == 0 or game.settings.enable_main_base_cheat

    def _show_main_base(self, cp: ControlPoint, game: Game) -> None:
        current = main_base(game, cp.captured)
        if current is cp:
            self.main_base_label.setText(
                "<b>★ Main supply base.</b> Supplies originate here and "
                "replenishment ships sail from this direction."
            )
            self.main_base_button.setVisible(False)
            return
        name = current.name if current is not None else "none"
        self.main_base_label.setText(f"Main supply base: {name}.")
        self.main_base_button.setText("Make this the main supply base")
        self.main_base_button.setVisible(
            self.can_change_main_base(game)
            and cp in main_base_options(game, cp.captured)
        )

    def _make_main_base(self, cp: ControlPoint, game: Game) -> None:
        from game.server import EventStream

        warnings = main_base_warnings(game, cp)
        if warnings:
            answer = QMessageBox.question(
                self,
                "Risky main supply base",
                f"{cp.name} is a risky main supply base: "
                + "; ".join(warnings)
                + ".<br><br>Make it the main supply base anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        before = main_base(game, cp.captured)
        set_main_base(game, cp.captured, cp)
        self._show_main_base(cp, game)
        with EventStream.event_context() as events:
            for changed in {before, cp}:
                if changed is not None:
                    events.update_control_point(changed)

    def _open_orders(self, cp: ControlPoint, game: Game) -> None:
        from qt_ui.windows.basemenu.QMunitionOrders import QMunitionOrders

        QMunitionOrders(game, cp, self).exec()
        self.order_label.setText(self._order_note(cp, game))

    @staticmethod
    def _order_note(cp: ControlPoint, game: Game) -> str:
        on_order = ordered_at(game.warehouse_logistics, cp)
        if on_order:
            summary = ", ".join(
                f"{short_name(k)} ×{v}" for k, v in sorted(on_order.items())
            )
            return f"On order (arrives at turn end): {summary}"
        if manual_purchasing(game, cp.coalition):
            return "You pick munition purchases. Nothing on order here yet."
        return "Munitions are bought automatically; you can order extra."
