"""Core state machine and control loop for Set Field Mode and Low-to-High Sweep Mode."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .config import AppConfig
from .instruments import (
    Keysight34461A,
    MultimeterInterface,
    PowerSupplyInterface,
    TTiMX100TP,
    VirtualKeysight34461A,
    VirtualTTiMX100TP,
)
from .logger import CSVDataLogger, TelemetrySample
from .physics import VirtualCircuitModel
from .pid import AntiOvershootPID


class OperationMode(str, Enum):
    """Operational states of the solenoid magnetic field controller."""

    IDLE = "IDLE"
    SET_FIELD = "SET_FIELD"
    SWEEP = "SWEEP"
    FAULT = "FAULT"


@dataclass
class SweepParameters:
    """Configuration for a discrete low-to-high magnetic field sweep."""

    start_field_nt: float
    end_field_nt: float
    step_field_nt: float
    dwell_time_s: float

    def generate_steps(self) -> list[float]:
        """Validate sweep parameters and generate the ascending list of field setpoints (nT)."""
        if self.start_field_nt < 0:
            raise ValueError(f"start_field_nt must be >= 0, got {self.start_field_nt}.")
        if self.end_field_nt <= self.start_field_nt:
            raise ValueError(
                "Sweep Mode strictly enforces a low-to-high sweep direction: "
                f"end_field_nt ({self.end_field_nt} nT) must be greater than "
                f"start_field_nt ({self.start_field_nt} nT)."
            )
        if self.step_field_nt <= 0:
            raise ValueError(
                f"Sweep step_field_nt must be strictly positive (> 0), got {self.step_field_nt}."
            )
        if self.dwell_time_s < 0:
            raise ValueError(f"dwell_time_s must be >= 0, got {self.dwell_time_s}.")

        steps: list[float] = []
        current = float(self.start_field_nt)
        while current < self.end_field_nt - 1e-9:
            steps.append(round(current, 6))
            current += self.step_field_nt
        steps.append(round(float(self.end_field_nt), 6))
        return steps


class SystemController:
    """Headless domain controller coordinating instruments, PID, modes, safety, and logging."""

    def __init__(
        self,
        config: AppConfig,
        psu: PowerSupplyInterface | None = None,
        dmm: MultimeterInterface | None = None,
        virtual_circuit: VirtualCircuitModel | None = None,
        max_history: int = 3600,
    ) -> None:
        config.validate()
        self.config = config
        self.max_history = max_history

        if psu is not None and dmm is not None:
            self.psu = psu
            self.dmm = dmm
            self.virtual_circuit = virtual_circuit
        elif config.instruments.virtual_mode:
            self.virtual_circuit = virtual_circuit or VirtualCircuitModel(config.circuit_model)
            self.psu = VirtualTTiMX100TP(
                self.virtual_circuit,
                channel=config.instruments.psu_channel,
                safety=config.safety,
            )
            self.dmm = VirtualKeysight34461A(self.virtual_circuit)
        else:
            self.virtual_circuit = None
            self.psu = TTiMX100TP(
                ip=config.instruments.psu_ip,
                port=config.instruments.psu_port,
                channel=config.instruments.psu_channel,
                safety=config.safety,
                timeout_s=config.instruments.socket_timeout_s,
            )
            self.dmm = Keysight34461A(
                ip=config.instruments.dmm_ip,
                port=config.instruments.dmm_port,
                timeout_s=config.instruments.socket_timeout_s,
            )

        self.pid = AntiOvershootPID(
            pid_config=config.pid,
            safety_config=config.safety,
            circuit_config=config.circuit_model,
        )
        self.logger = CSVDataLogger(
            file_path=config.logging.log_file_path,
            log_interval_s=config.logging.log_interval_s,
            enabled=config.logging.enabled,
        )

        self.connected: bool = False
        self.mode: OperationMode = OperationMode.IDLE
        self.target_field_nt: float = 0.0
        self.elapsed_s: float = 0.0
        self.fault_message: str = ""

        # Sweep state
        self.sweep_steps_nt: list[float] = []
        self.sweep_step_index: int = 0
        self.sweep_dwell_time_s: float = 0.0
        self.sweep_dwell_elapsed_s: float = 0.0

        # Telemetry history buffer
        self.history: list[TelemetrySample] = []
        self.latest_sample: TelemetrySample | None = None

    def connect_instruments(self) -> None:
        """Connect to the power supply and multimeter."""
        self.psu.connect()
        self.dmm.connect()
        self.connected = True

    def disconnect_instruments(self) -> None:
        """Safely zero output and disconnect from instruments."""
        if self.connected:
            try:
                self.psu.set_voltage(0.0)
                self.psu.set_output(False)
            except Exception:
                pass
        self.psu.disconnect()
        self.dmm.disconnect()
        self.connected = False
        self.mode = OperationMode.IDLE

    def validate_field_within_safety_limits(self, field_nt: float) -> None:
        """Verify that a target magnetic field (nT) does not violate safety hard limits."""
        if field_nt < 0:
            raise ValueError(f"Magnetic field setpoint cannot be negative ({field_nt} nT).")

        required_current_a = self.config.field_nt_to_current_a(field_nt)
        if required_current_a > self.config.safety.max_current_a + 1e-9:
            raise ValueError(
                f"Target field {field_nt:.2f} nT requires {required_current_a:.4f} A, "
                f"which exceeds safety hard limit max_current_a = {self.config.safety.max_current_a:.4f} A."
            )

        required_psu_voltage_v = (
            self.config.circuit_model.steady_state_psu_voltage_for_coil_current(required_current_a)
        )
        if required_psu_voltage_v > self.config.safety.max_voltage_v + 1e-9:
            raise ValueError(
                f"Target field {field_nt:.2f} nT requires steady-state PSU voltage "
                f"{required_psu_voltage_v:.2f} V, which exceeds safety hard limit "
                f"max_voltage_v = {self.config.safety.max_voltage_v:.2f} V."
            )

        # Also check total PSU current (coil + bleeder) against max_current_a
        v_cap = required_current_a * self.config.circuit_model.load_resistance_ohm
        i_bleed = v_cap / self.config.circuit_model.bleeder_resistance_ohm
        total_psu_current_a = required_current_a + i_bleed
        if total_psu_current_a > self.config.safety.max_current_a + 1e-9:
            raise ValueError(
                f"Target field {field_nt:.2f} nT requires total PSU current (coil + bleeder) "
                f"of {total_psu_current_a:.4f} A, which exceeds safety hard limit "
                f"max_current_a = {self.config.safety.max_current_a:.4f} A."
            )

    def start_set_field_mode(self, target_field_nt: float) -> None:
        """Enter Set Field Mode to hold a constant magnetic field with <1% accuracy."""
        if not self.connected:
            raise RuntimeError("Instruments must be connected before starting Set Field Mode.")

        self.validate_field_within_safety_limits(target_field_nt)
        self.fault_message = ""
        self.target_field_nt = float(target_field_nt)
        self.mode = OperationMode.SET_FIELD
        self.sweep_steps_nt = []
        self.sweep_step_index = 0
        self.sweep_dwell_elapsed_s = 0.0

        self.psu.set_current_limit(self.config.safety.max_current_a)
        self.psu.set_output(True)

    def start_sweep_mode(self, params: SweepParameters) -> None:
        """Enter Sweep Mode to step magnetic field strictly from low to high."""
        if not self.connected:
            raise RuntimeError("Instruments must be connected before starting Sweep Mode.")

        steps = params.generate_steps()
        for step_field in (steps[0], steps[-1]):
            self.validate_field_within_safety_limits(step_field)

        # Verify current measured field is not above start_field_nt (cannot actively pull energy out)
        current_coil_a = self.dmm.measure_current_dc()
        current_field_nt = self.config.current_a_to_field_nt(current_coil_a)
        allowed_start_ceiling = (
            params.start_field_nt * (1.0 + self.config.pid.holding_tolerance_ratio) + 1e-3
        )
        if current_field_nt > allowed_start_ceiling:
            raise ValueError(
                f"Cannot start low-to-high sweep at {params.start_field_nt:.2f} nT because the "
                f"current field ({current_field_nt:.2f} nT) is already higher than start_field_nt."
            )

        self.fault_message = ""
        self.sweep_steps_nt = steps
        self.sweep_step_index = 0
        self.sweep_dwell_time_s = float(params.dwell_time_s)
        self.sweep_dwell_elapsed_s = 0.0
        self.target_field_nt = self.sweep_steps_nt[0]
        self.mode = OperationMode.SWEEP

        self.psu.set_current_limit(self.config.safety.max_current_a)
        self.psu.set_output(True)

    def stop(self) -> None:
        """Stop active control, set PSU voltage to 0 V, and disable PSU output."""
        self.mode = OperationMode.IDLE
        self.target_field_nt = 0.0
        self.sweep_steps_nt = []
        self.sweep_step_index = 0
        self.sweep_dwell_elapsed_s = 0.0
        self.pid.reset()
        if self.connected:
            self.psu.set_voltage(0.0)
            self.psu.set_output(False)

    def _trip_safety_fault(self, reason: str) -> None:
        """Immediately shut down the power supply output and latch FAULT mode."""
        self.mode = OperationMode.FAULT
        self.fault_message = reason
        self.target_field_nt = 0.0
        self.pid.reset()
        if self.connected:
            try:
                self.psu.set_voltage(0.0)
                self.psu.set_output(False)
            except Exception:
                pass

    def compute_holding_error(self, target_field_nt: float, measured_field_nt: float) -> tuple[float, bool]:
        """Compute percentage error and whether the field is within <1% holding tolerance."""
        tol_ratio = self.config.pid.holding_tolerance_ratio
        if target_field_nt <= 1e-9:
            abs_curr = self.config.field_nt_to_current_a( abs(measured_field_nt) )
            within = abs_curr < 1e-4
            return (0.0 if within else 100.0), within

        rel_error = abs(measured_field_nt - target_field_nt) / abs(target_field_nt)
        error_pct = rel_error * 100.0
        within = rel_error < tol_ratio and not math.isnan(rel_error)
        return error_pct, within

    def update(self, dt_s: float) -> TelemetrySample:
        """Execute one control, physics (if virtual), safety, and logging cycle."""
        if dt_s <= 0:
            raise ValueError(f"dt_s must be > 0, got {dt_s}.")
        if not self.connected:
            raise RuntimeError("Cannot update controller while instruments are disconnected.")

        # Advance virtual physics if running in Virtual Testing Mode
        if self.virtual_circuit is not None:
            self.virtual_circuit.step(dt_s)

        self.elapsed_s += dt_s

        # 1. Read measurements from instruments
        coil_current_a = self.dmm.measure_current_dc()
        psu_v_meas = self.psu.get_measured_voltage()
        psu_i_meas = self.psu.get_measured_current()
        measured_field_nt = self.config.current_a_to_field_nt(coil_current_a)

        # 2. Enforce runtime safety hard limits (emergency cutoff if exceeded)
        if coil_current_a > self.config.safety.max_current_a * 1.001:
            self._trip_safety_fault(
                f"Over-current fault: coil current {coil_current_a:.4f} A exceeded "
                f"max_current_a ({self.config.safety.max_current_a:.4f} A)."
            )
        elif psu_i_meas > self.config.safety.max_current_a * 1.001:
            self._trip_safety_fault(
                f"Over-current fault: PSU output current {psu_i_meas:.4f} A exceeded "
                f"max_current_a ({self.config.safety.max_current_a:.4f} A)."
            )
        elif psu_v_meas > self.config.safety.max_voltage_v * 1.001:
            self._trip_safety_fault(
                f"Over-voltage fault: PSU terminal voltage {psu_v_meas:.2f} V exceeded "
                f"max_voltage_v ({self.config.safety.max_voltage_v:.2f} V)."
            )

        # 3. Evaluate holding accuracy for the current target
        error_pct, within_tolerance = self.compute_holding_error(
            self.target_field_nt, measured_field_nt
        )

        # 4. Mode state machine & PID control output
        commanded_voltage_v = 0.0
        if self.mode == OperationMode.SWEEP:
            if within_tolerance:
                self.sweep_dwell_elapsed_s += dt_s
                if self.sweep_dwell_elapsed_s >= self.sweep_dwell_time_s - 1e-9:
                    if self.sweep_step_index + 1 < len(self.sweep_steps_nt):
                        self.sweep_step_index += 1
                        self.target_field_nt = self.sweep_steps_nt[self.sweep_step_index]
                        self.sweep_dwell_elapsed_s = 0.0
                        error_pct, within_tolerance = self.compute_holding_error(
                            self.target_field_nt, measured_field_nt
                        )
                    else:
                        # Final step completed -> hold final field in SET_FIELD mode
                        self.mode = OperationMode.SET_FIELD

        if self.mode in (OperationMode.SET_FIELD, OperationMode.SWEEP):
            target_current_a = self.config.field_nt_to_current_a(self.target_field_nt)
            commanded_voltage_v = self.pid.compute(
                target_current_a=target_current_a,
                measured_current_a=coil_current_a,
                dt_s=dt_s,
            )
            self.psu.set_voltage(commanded_voltage_v)
        else:
            target_current_a = 0.0
            commanded_voltage_v = 0.0

        # 5. Build telemetry sample and log if interval elapsed
        sample = TelemetrySample(
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            elapsed_s=self.elapsed_s,
            mode=self.mode.value,
            target_field_nt=self.target_field_nt,
            measured_field_nt=measured_field_nt,
            holding_error_pct=error_pct,
            within_tolerance=within_tolerance,
            target_current_a=target_current_a,
            coil_current_a=coil_current_a,
            psu_voltage_set_v=commanded_voltage_v,
            psu_voltage_meas_v=psu_v_meas,
            psu_current_meas_a=psu_i_meas,
            sweep_step_index=(self.sweep_step_index + 1) if self.sweep_steps_nt else 0,
            sweep_total_steps=len(self.sweep_steps_nt),
            sweep_dwell_elapsed_s=self.sweep_dwell_elapsed_s,
            fault_message=self.fault_message,
        )

        self.latest_sample = sample
        self.history.append(sample)
        if len(self.history) > self.max_history:
            self.history.pop(0)

        self.logger.maybe_log(sample)
        return sample
