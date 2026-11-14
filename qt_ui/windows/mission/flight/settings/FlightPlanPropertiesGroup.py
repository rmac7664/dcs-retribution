"""Timing and route: where it leaves from, when, and where it comes back to.

Five facts in flight order, as key/value rows rather than a stack of labelled fields.
The times are mono and right-aligned so they line up down a column, which is the whole
reason to read them together. "Ahead of package" was a checkbox next to a spinner --
two controls for one setting, and the checkbox had to be read to know what the spinner
meant; Behind/Ahead plus mm:ss says it once.
"""

import logging
from datetime import timedelta

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from game import Game
from game.ato import FlightType, attackprofile
from game.ato.attackprofile import AttackProfile
from game.ato.flight import Flight
from game.ato.flightplans.planningerror import PlanningError
from qt_ui.models import PackageModel
from qt_ui.widgets.combos.QArrivalAirfieldSelector import QArrivalAirfieldSelector
from qt_ui.widgets.cards import make_transparent
from qt_ui.widgets.spinsliders import MinuteSecondSpinner
from qt_ui.widgets.controls import (
    wrapped_tooltip,
    Segmented,
    key_value,
    styled_input,
    value_label,
)
from qt_ui.windows.mission.flight.waypoints.QFlightWaypointList import (
    QFlightWaypointList,
)

SEPARATOR = "#1D2731"
NOTE = "#7C8B99"


class FlightPlanPropertiesGroup(QWidget):
    def __init__(
        self,
        game: Game,
        package_model: PackageModel,
        flight: Flight,
        flight_wpt_list: QFlightWaypointList,
    ) -> None:
        super().__init__()
        self.game = game
        self.package_model = package_model
        self.flight = flight
        self.flight_wpt_list = flight_wpt_list

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(0)

        layout.addWidget(
            key_value("Departure", value_label(flight.departure.name, align_right=True))
        )
        self.departure_time = value_label("", monospace=True, align_right=True)
        layout.addWidget(key_value("Takes off", self.departure_time))
        self.package_model.tot_changed.connect(self.update_departure_time)
        self.update_departure_time()

        layout.addWidget(key_value("TOT offset", self._offset_control(), height=40))

        layout.addWidget(
            key_value("Arrival", value_label(flight.arrival.name, align_right=True))
        )

        self.divert = QArrivalAirfieldSelector(
            [
                cp
                for cp in game.theater.controlpoints
                if cp.captured == flight.coalition.player
            ],
            flight.unit_type,
            "None",
        )
        self.divert.currentIndexChanged.connect(self.set_divert)
        if flight.divert is not None:
            self.divert.setCurrentText(flight.divert.name)
        divert_row = QHBoxLayout()
        divert_row.setContentsMargins(0, 0, 0, 0)
        divert_row.addStretch()
        divert_row.addWidget(styled_input(self.divert, width=200))
        divert_holder = QWidget()
        make_transparent(divert_holder)
        divert_holder.setLayout(divert_row)
        layout.addWidget(key_value("Divert", divert_holder, height=40))

        for option in self._task_options():
            layout.addWidget(self._separator())
            layout.addWidget(option)

        self.setLayout(layout)

    # --- the pieces ---------------------------------------------------------

    def _offset_control(self) -> QWidget:
        """Behind or ahead of the package, and by how much -- one setting, one row."""
        delay = int(self.flight.flight_plan.tot_offset.total_seconds())
        ahead = delay < 0
        delay = abs(delay)

        self.direction = Segmented([("Behind", False), ("Ahead", True)], current=ahead)
        self.direction.selection_changed.connect(self._on_direction_changed)

        self.tot_offset_spinner = MinuteSecondSpinner(delay, maximum=59 * 60 + 59)
        self.tot_offset_spinner.valueChanged.connect(self.set_tot_offset)
        self.tot_offset_spinner.setToolTip(
            "How far this flight is from the package TOT. The arrows step a minute at"
            " a time; seconds can be typed."
        )

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addStretch()
        row.addWidget(self.direction)
        # Wide enough for mm:ss AND the spin arrows: at 76 the seconds were under them.
        row.addWidget(styled_input(self.tot_offset_spinner, width=96))
        holder = QWidget()
        make_transparent(holder)
        holder.setLayout(row)
        return holder

    def _task_options(self) -> list[QWidget]:
        """The checkboxes only some tasks have. Below a rule, because they are not
        timing -- but this is where they have always lived and moving them is a
        separate argument."""
        options: list[QWidget] = []

        if self.flight.flight_type == FlightType.SEAD:
            self.release_at_ingress_checkbox = QCheckBox(
                "Release decoys at ingress point ignoring the weapon range"
            )
            self.release_at_ingress_checkbox.setChecked(self.flight.release_at_ingress)
            self.release_at_ingress_checkbox.setToolTip(
                wrapped_tooltip(
                    "For a decoy (e.g. TALD) SEAD run. Normally the AI closes to the "
                    "decoy's launch range before releasing, which means flying deep into "
                    "the SAM envelope and getting shot before it fires. With this on, the "
                    "flight releases its decoys from stand-off instead -- at a hidden bait "
                    "point just inside the threat ring -- so it fires from outside the "
                    "SAM's reach and the decoys glide the rest of the way in to draw "
                    "fire.\n\n"
                    "Only affects decoys; guided and anti-radiation weapons (HARM, JDAM) "
                    "still close to the target as usual.\n\n"
                    "Tip: place the flight's ingress waypoint OUTSIDE the SAM ring for the "
                    "stand-off effect to matter."
                )
            )
            self.release_at_ingress_checkbox.toggled.connect(
                self.set_release_at_ingress
            )
            options.append(self._wrap_option(self.release_at_ingress_checkbox))

        if attackprofile.supported(self.flight):
            options.append(self._attack_profile_option())

        if self.flight.flight_type == FlightType.AIR_ASSAULT and self.flight.is_helo:
            self.remain_at_destination_checkbox = QCheckBox(
                "Remain at the assault destination (do not return)"
            )
            self.remain_at_destination_checkbox.setChecked(
                self.flight.remain_at_destination
            )
            self.remain_at_destination_checkbox.setToolTip(
                wrapped_tooltip(
                    "The helicopters land at the objective and do NOT fly home. At the "
                    "end of the turn:\n"
                    " - if you CAPTURE the objective's base, the helicopters redeploy "
                    "there (a free ferry to the new base);\n"
                    " - if you do NOT capture it, the helicopters are LOST.\n\n"
                    "Lets a one-way assault use the helicopter's full ferry range instead "
                    "of its round-trip radius, and forward-stages the aircraft on the "
                    "captured base. Helicopters only."
                )
            )
            self.remain_at_destination_checkbox.toggled.connect(
                self.set_remain_at_destination
            )
            options.append(self._wrap_option(self.remain_at_destination_checkbox))

        return options

    def _attack_profile_option(self) -> QWidget:
        self.attack_profile = QComboBox()
        for profile in AttackProfile:
            self.attack_profile.addItem(profile.value, profile)
        self.attack_profile.setCurrentIndex(
            self.attack_profile.findData(attackprofile.profile_of(self.flight))
        )
        self.attack_profile.setToolTip(
            wrapped_tooltip(
                "How this flight runs in and attacks.\n\n"
                "Normal: at the aircraft's usual attack altitude.\n"
                "Low-level sled: the run-in and egress are flown low, and the bombs "
                "are released in level flight. With slick (unretarded) unguided bombs "
                "the AI climbs to 1,500 ft above the target to release, clear of the "
                "fragments.\n"
                "Pop-up dive: low run-in, then a climb to about 3,000 ft above the "
                "target and a dive attack.\n"
                "Loft: low run-in to a pull-up point, for the pilot to toss the bombs. "
                "DCS AI cannot loft, so an AI flight flies the sled instead.\n\n"
                "The low legs are flown 500 ft above the ground, or 1,000 ft where "
                "the ground is hilly or its height could not be checked. An AI flight "
                "whose run-in crosses mountains keeps the normal profile.\n\n"
                "DEAD flights carrying anti-radiation missiles keep the normal "
                "profile. Anti-ship flights only fly the route low."
            )
        )
        self.attack_profile.currentIndexChanged.connect(self.set_attack_profile)
        self.attack_profile_note = value_label("")
        self.attack_profile_note.setStyleSheet(f"color: {NOTE}; font-size: 11px;")
        self.attack_profile_note.setWordWrap(True)
        self._update_attack_profile_note()
        combo_row = QHBoxLayout()
        combo_row.setContentsMargins(0, 0, 0, 0)
        combo_row.addStretch()
        combo_row.addWidget(styled_input(self.attack_profile, width=200))
        combo_holder = QWidget()
        make_transparent(combo_holder)
        combo_holder.setLayout(combo_row)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 6)
        column.setSpacing(0)
        column.addWidget(key_value("Attack profile", combo_holder, height=40))
        note_row = QHBoxLayout()
        note_row.setContentsMargins(14, 0, 14, 0)
        note_row.addWidget(self.attack_profile_note)
        column.addLayout(note_row)
        holder = QWidget()
        make_transparent(holder)
        holder.setLayout(column)
        return holder

    def _update_attack_profile_note(self) -> None:
        layout = self.flight.flight_plan.layout
        chosen = attackprofile.profile_of(self.flight)
        flown = getattr(layout, "attack_profile", None)
        height = getattr(layout, "low_level_height_ft", None)
        if not chosen.low_level:
            text = ""
        elif flown is None and attackprofile.carries_arms(self.flight):
            text = (
                "This flight carries anti-radiation missiles, which need altitude, "
                "so it keeps the normal profile."
            )
        elif flown is None:
            text = (
                "The run-in crosses mountains, so this AI flight keeps the normal "
                "profile."
            )
        else:
            text = f"Low legs flown {height:,} ft above the ground."
            if attackprofile.route_only(self.flight):
                text += " Only the route is low: the attack on the ships is unchanged."
            if flown != chosen.value:
                text += (
                    f" AI flights cannot loft, so this one flies the {flown.lower()}."
                )
        self.attack_profile_note.setText(text)
        self.attack_profile_note.setVisible(bool(text))

    def set_attack_profile(self, index: int) -> None:
        profile = self.attack_profile.itemData(index)
        old = self.flight.attack_profile
        if not isinstance(profile, AttackProfile) or profile is old:
            return
        self.flight.attack_profile = profile
        try:
            self.flight.recreate_flight_plan()
        except PlanningError as ex:
            self.flight.attack_profile = old
            logging.exception("Could not change the attack profile")
            QMessageBox.critical(
                self,
                "Could not update flight plan",
                str(ex),
                QMessageBox.StandardButton.Ok,
            )
            return
        self.package_model.push_tot_if_unreachable()
        self.package_model.update_tot()
        self.update_departure_time()
        self._update_attack_profile_note()

    @staticmethod
    def _wrap_option(checkbox: QCheckBox) -> QWidget:
        checkbox.setStyleSheet(
            "font-size: 12px; background: transparent; border: none;"
        )
        row = QHBoxLayout()
        row.setContentsMargins(14, 6, 14, 6)
        row.addWidget(checkbox)
        holder = QWidget()
        make_transparent(holder)
        holder.setLayout(row)
        return holder

    @staticmethod
    def _separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {SEPARATOR}; border: none;")
        return line

    # --- behaviour, unchanged -----------------------------------------------

    def update_departure_time(self) -> None:
        if not self.flight.package.flights:
            # This is theoretically impossible, but for some reason the dialog that owns
            # this object QEditFlightDialog does not dispose properly on close, so this
            # handler may be called for a flight whose package has been canceled, which
            # is an invalid state for calling anything in TotEstimator.
            return
        self.departure_time.setText(
            f"{self.flight.flight_plan.startup_time():%H:%M:%S}"
        )
        self.flight_wpt_list.update_list()

    def set_divert(self, index: int) -> None:
        old_divert = self.flight.divert
        divert = self.divert.itemData(index)
        if divert == old_divert:
            return

        self.flight.divert = divert
        try:
            self.flight.recreate_flight_plan()
        except PlanningError as ex:
            self.flight.divert = old_divert
            logging.exception("Could not change divert airfield")
            QMessageBox.critical(
                self,
                "Could not update flight plan",
                str(ex),
                QMessageBox.StandardButton.Ok,
            )

    def set_tot_offset(self, seconds: int) -> None:
        delay = timedelta(seconds=seconds)
        if self.direction.value:
            delay = -delay
        self._apply_tot_offset(delay)

    def _on_direction_changed(self, ahead: bool) -> None:
        # The sign comes from the control's state, so the two cannot disagree.
        delay = abs(self.flight.flight_plan.tot_offset)
        self._apply_tot_offset(-delay if ahead else delay)

    def _apply_tot_offset(self, delay: timedelta) -> None:
        self.flight.flight_plan.tot_offset = delay
        # A flight put ahead of its package may no longer reach its own TOT; slide the
        # package rather than leave it with an unreachable plan.
        self.package_model.push_tot_if_unreachable()
        self.package_model.update_tot()
        self.update_departure_time()

    def set_release_at_ingress(self, checked: bool) -> None:
        self.flight.release_at_ingress = checked

    def set_remain_at_destination(self, checked: bool) -> None:
        self.flight.remain_at_destination = checked
        # Toggling the flag alone does not regenerate the route: rebuild the flight
        # plan so the return leg is dropped (or restored) and refresh the list.
        try:
            self.flight.recreate_flight_plan()
        except PlanningError:
            self.flight.remain_at_destination = not checked
            logging.exception("Could not recreate flight plan after toggling remain")
            return
        self.flight_wpt_list.update_list()
