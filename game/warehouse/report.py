"""The logistics part of the turn report, and the overview's summary of last turn.

Deliveries and losses are recorded as supply runs finish (supply.deliver), then at
the end of the turn they are reported with what each side bought, and saved as "last
turn" for the Logistics overview.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from game import Game
    from game.settings import Settings
    from .supply import PurchaseReport

#: A base below this share of its munitions or fuel is reported as running low.
LOW_PERCENT = 25


@dataclass
class TurnLog:
    """What one side's supply runs did this turn."""

    #: (destination, tonnes delivered)
    delivered: list[tuple[str, float]] = field(default_factory=list)
    #: (destination, tonnes lost on the way)
    lost: list[tuple[str, float]] = field(default_factory=list)

    @property
    def delivered_tons(self) -> float:
        return sum(t for _, t in self.delivered)

    @property
    def lost_tons(self) -> float:
        return sum(t for _, t in self.lost)


def _logs(game: Game, attr: str = "turn_log") -> dict[str, TurnLog]:
    state = game.warehouse_logistics
    logs = getattr(state, attr, None)
    if logs is None:
        logs = {}
        setattr(state, attr, logs)
    return logs


def record_delivery(
    game: Game, side: str, destination: str, tons: float, lost_tons: float
) -> None:
    log = _logs(game).setdefault(side, TurnLog())
    if tons > 0.05:
        log.delivered.append((destination, tons))
    if lost_tons > 0.05:
        log.lost.append((destination, lost_tons))


def end_turn(game: Game) -> None:
    """Keeps this turn's log as "last turn" and starts a new one."""
    state = game.warehouse_logistics
    state.last_turn_log = dict(_logs(game))
    state.turn_log = {}
    state.last_ships_sunk = dict(getattr(state, "ships_sunk", {}) or {})
    state.ships_sunk = {}


def _purchase_line(report: PurchaseReport, settings: Settings) -> Optional[str]:
    if not report.bought:
        return None
    bought = sum(report.bought.values())
    cost = f" for ${report.spent:,.1f}M" if settings.logistics_munitions_cost else ""
    top = ", ".join(
        f"{k.split('.', 2)[-1]} ×{v}" for k, v in report.bought.most_common(4)
    )
    return f"Bought {bought} munitions{cost} (most: {top})."


def _places(entries: list[tuple[str, float]]) -> str:
    totals: dict[str, float] = {}
    for name, tons in entries:
        totals[name] = totals.get(name, 0.0) + tons
    return ", ".join(
        f"{name} {tons:.0f} t"
        for name, tons in sorted(totals.items(), key=lambda kv: -kv[1])
    )


def low_bases(game: Game, player: Any) -> list[str]:
    """Bases of `player` below LOW_PERCENT of munitions or fuel, worst first."""
    from .overview import overview

    rows = [
        r
        for r in overview(game, player).rows
        if r.level < LOW_PERCENT and r.role != "Rear area"
    ]
    return [
        f"{r.name} (munitions {r.munitions_percent}%, fuel {r.fuel_percent}%)"
        for r in rows
    ]


def turn_messages(game: Game) -> list[tuple[str, str]]:
    """The logistics entries for the player's turn report, as (title, text)."""
    from .supply import report_lines

    state = game.warehouse_logistics
    settings = game.settings
    blue = game.blue.player
    messages = []

    report = state.last_purchase.get(blue.name)
    log = _logs(game).get(blue.name, TurnLog())
    supply: list[str] = []
    if report is not None:
        line = _purchase_line(report, settings)
        if line:
            supply.append(line)
        supply.extend(
            l for l in report_lines(report, settings) if not l.startswith("Bought ")
        )
    if log.delivered:
        supply.append(
            f"Delivered {log.delivered_tons:,.0f} t: {_places(log.delivered)}."
        )
    if supply:
        messages.append(("Logistics: supply", "\n".join(supply)))

    losses = []
    if log.lost:
        losses.append(f"Lost on the way: {_places(log.lost)}.")
    sunk = (getattr(state, "ships_sunk", {}) or {}).get(blue.name, [])
    if sunk:
        losses.append(
            "Replenishment ships sunk (fuel and munitions lost): "
            + ", ".join(sorted(sunk))
            + "."
        )
    if losses:
        messages.append(("Logistics: losses", "\n".join(losses)))

    low = low_bases(game, blue)
    if low:
        messages.append(
            (
                "Logistics: bases running low",
                "\n".join(low)
                + "\nMark them as priority in the Logistics overview to resupply "
                "them first.",
            )
        )

    enemy = game.red.player
    enemy_log = _logs(game).get(enemy.name, TurnLog())
    enemy_lines = []
    enemy_sunk = (getattr(state, "ships_sunk", {}) or {}).get(enemy.name, [])
    if enemy_sunk:
        enemy_lines.append(
            f"We sank {len(enemy_sunk)} enemy replenishment ship(s) bound for "
            + ", ".join(sorted(enemy_sunk))
            + "."
        )
    if enemy_log.lost:
        enemy_lines.append(
            f"Enemy supply runs lost {enemy_log.lost_tons:,.0f} t of cargo on the "
            "way."
        )
    if enemy_lines:
        messages.append(("Logistics: enemy supply", "\n".join(enemy_lines)))
    return messages


def summary_lines(game: Game, player: Any) -> list[str]:
    """Last turn in a few lines, for the Logistics overview."""
    state = game.warehouse_logistics
    settings = game.settings
    lines = []
    report = state.last_purchase.get(player.name)
    if report is not None:
        line = _purchase_line(report, settings)
        lines.append(line or "Bought nothing last turn.")
        if report.unaffordable:
            lines.append(
                "Couldn't afford: "
                + ", ".join(
                    f"{k.split('.', 2)[-1]} ×{v}"
                    for k, v in report.unaffordable.most_common(4)
                )
                + "."
            )
    log = (getattr(state, "last_turn_log", None) or {}).get(player.name)
    if log is not None:
        if log.delivered:
            lines.append(f"Delivered last turn: {log.delivered_tons:,.0f} t.")
        if log.lost:
            lines.append(f"Lost on the way last turn: {log.lost_tons:,.0f} t.")
    sunk = (getattr(state, "last_ships_sunk", {}) or {}).get(player.name, [])
    if sunk:
        lines.append(f"Replenishment ships sunk last mission: {len(sunk)}.")
    return lines
