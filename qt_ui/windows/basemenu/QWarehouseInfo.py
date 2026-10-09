from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from game import Game
from game.theater import Airfield, ControlPoint
from game.warehouse.munitions import ammo_only
from game.warehouse.state import KG_PER_TON, short_name
from game.warehouse.supply import (
    MunitionPrices,
    SupplyPlanner,
    main_base,
    main_base_is_chosen,
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
                lambda: self._toggle_main_base(cp, game)
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
        if runs:
            runs_group = QGroupBox("Supply runs")
            runs_layout = QVBoxLayout()
            for transfer in runs:
                line = QLabel(f"{transfer} — {transfer.description}")
                line.setWordWrap(True)
                runs_layout.addWidget(line)
            runs_group.setLayout(runs_layout)
            content.addWidget(runs_group)

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

    def _show_main_base(self, cp: ControlPoint, game: Game) -> None:
        current = main_base(game, cp.captured)
        if current is cp:
            how = (
                "you picked it"
                if main_base_is_chosen(game, cp)
                else "chosen automatically: your airfield farthest from the enemy"
            )
            self.main_base_label.setText(
                f"<b>Main supply base</b> ({how}). Supplies originate here and "
                "replenishment ships sail from this direction."
            )
            self.main_base_button.setText("Use automatic choice")
            self.main_base_button.setVisible(main_base_is_chosen(game, cp))
            return
        name = current.name if current is not None else "none"
        self.main_base_label.setText(f"Main supply base: {name}.")
        self.main_base_button.setText("Make this the main supply base")
        self.main_base_button.setVisible(
            isinstance(cp, Airfield) and cp.runway_is_operational()
        )

    def _toggle_main_base(self, cp: ControlPoint, game: Game) -> None:
        from game.server import EventStream

        before = main_base(game, cp.captured)
        if before is cp and main_base_is_chosen(game, cp):
            set_main_base(game, cp.captured, None)
        else:
            set_main_base(game, cp.captured, cp)
        after = main_base(game, cp.captured)
        self._show_main_base(cp, game)
        with EventStream.event_context() as events:
            for changed in {before, after, cp}:
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
