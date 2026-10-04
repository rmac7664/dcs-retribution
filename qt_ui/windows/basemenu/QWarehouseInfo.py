from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QLabel,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from game import Game
from game.theater import ControlPoint
from game.warehouse.munitions import ammo_only
from game.warehouse.state import KG_PER_TON, short_name


class QWarehouseInfo(QFrame):
    """Fuel and munition stock at a base when base logistics is enabled."""

    def __init__(self, cp: ControlPoint, game: Game) -> None:
        super().__init__()
        state = game.warehouse_logistics
        stock = state.ensure_stock(game, cp)
        capacity = state.fuel_capacity_kg(cp, game.settings)
        authorized = state.authorized_munitions(game, cp)

        content = QVBoxLayout()

        fuel_group = QGroupBox("Aviation fuel")
        fuel_layout = QVBoxLayout()
        fuel_bar = QProgressBar()
        fuel_bar.setRange(0, max(1, int(capacity / KG_PER_TON)))
        fuel_bar.setValue(int(stock.jet_fuel_kg / KG_PER_TON))
        fuel_bar.setFormat(
            f"{stock.jet_fuel_kg / KG_PER_TON:,.0f} t of {capacity / KG_PER_TON:,.0f} t"
        )
        fuel_layout.addWidget(fuel_bar)
        fuel_group.setLayout(fuel_layout)
        content.addWidget(fuel_group)

        munitions_group = QGroupBox("Munitions (on hand / authorized)")
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
        munitions_group.setLayout(grid)
        content.addWidget(munitions_group)

        last = state.last_result
        if last is not None and cp.name in last.munitions_used:
            used = ", ".join(
                f"{short_name(k)} ×{v}"
                for k, v in sorted(last.munitions_used[cp.name].items())
            )
            content.addWidget(QLabel(f"Expended last mission: {used}"))

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
