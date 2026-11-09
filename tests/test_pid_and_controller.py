"""Unit tests for the anti-overshoot PID loop, Set Field Mode, Sweep Mode, and Safety Limits."""

from __future__ import annotations

import pytest

from coil_driver.config import AppConfig, SafetyConfig
from coil_driver.controller import OperationMode, SweepParameters, SystemController


def test_set_field_mode_gentle_encroachment_without_overshoot(tmp_path) -> None:
    """Verify Set Field Mode charges the 330F capacitor to target without overshooting and holds <1%."""
    cfg = AppConfig()
    cfg.logging.enabled = False
    cfg.logging.log_file_path = str(tmp_path / "log.csv")

    controller = SystemController(cfg)
    controller.connect_instruments()

    target_field_nt = 10000.0  # 1.0 A at 0.0001 A/nT
    controller.start_set_field_mode(target_field_nt)

    max_observed_field_nt = 0.0
    settled_reached = False

    # Simulate 2200 seconds in 2.0 s control steps (covers 330 F * 10 V / 3 A physical charge time)
    for _ in range(1100):
        sample = controller.update(2.0)
        max_observed_field_nt = max(max_observed_field_nt, sample.measured_field_nt)
        if sample.within_tolerance:
            settled_reached = True

    assert settled_reached is True
    # Must NEVER overshoot the target field beyond the <1% holding band (in fact <0.1% overshoot)
    assert max_observed_field_nt <= target_field_nt * 1.002
    assert controller.latest_sample is not None
    assert controller.latest_sample.holding_error_pct < 1.0
    assert controller.latest_sample.within_tolerance is True


def test_set_field_mode_handles_model_resistance_mismatch(tmp_path) -> None:
    """Verify the integral term eliminates steady-state error within <1% even if R_load differs by 5%."""
    cfg = AppConfig()
    cfg.logging.enabled = False
    cfg.logging.log_file_path = str(tmp_path / "log.csv")

    controller = SystemController(cfg)
    # Perturb the actual virtual circuit load resistance by +5% relative to PID's nominal model
    assert controller.virtual_circuit is not None
    controller.virtual_circuit.config.load_resistance_ohm = 10.5

    controller.connect_instruments()
    target_field_nt = 10000.0
    controller.start_set_field_mode(target_field_nt)

    for _ in range(1200):
        sample = controller.update(2.0)

    assert sample.within_tolerance is True
    assert sample.holding_error_pct < 1.0
    assert sample.measured_field_nt <= target_field_nt * 1.01


def test_set_field_mode_allows_passive_bleed_down_to_lower_target(tmp_path) -> None:
    """Verify Set Field Mode allows a lower target by dropping PSU to 0V for passive RC bleed-down."""
    cfg = AppConfig()
    cfg.logging.enabled = False
    cfg.logging.log_file_path = str(tmp_path / "log.csv")

    controller = SystemController(cfg)
    assert controller.virtual_circuit is not None
    # Pre-charge capacitor to 10.0 V (1.0 A = 10,000 nT)
    controller.virtual_circuit.reset(initial_voltage_v=10.0)
    controller.connect_instruments()

    # Set a lower target of 8,000 nT (0.8 A -> 8.0 V)
    controller.start_set_field_mode(8000.0)
    sample = controller.update(1.0)
    # Because measured field (10,000 nT) > target (8,000 nT), PID immediately commands 0.0 V
    assert sample.psu_voltage_set_v == pytest.approx(0.0)

    # Simulate passive bleed-down and capture at 8,000 nT
    for _ in range(800):
        sample = controller.update(1.0)
        if sample.within_tolerance:
            break

    assert sample.within_tolerance is True
    assert sample.holding_error_pct < 1.0


def test_sweep_mode_strictly_rejects_high_to_low_and_invalid_steps(tmp_path) -> None:
    """Verify Sweep Mode strictly rejects high-to-low sweeps, zero/negative steps, or charged start."""
    cfg = AppConfig()
    cfg.logging.enabled = False
    cfg.logging.log_file_path = str(tmp_path / "log.csv")

    controller = SystemController(cfg)
    controller.connect_instruments()

    # 1. High-to-low sweep (start > end) must be rejected
    with pytest.raises(ValueError, match="low-to-high"):
        controller.start_sweep_mode(
            SweepParameters(
                start_field_nt=15000.0,
                end_field_nt=5000.0,
                step_field_nt=2500.0,
                dwell_time_s=5.0,
            )
        )

    # 2. Equal start and end (start == end) must be rejected
    with pytest.raises(ValueError, match="low-to-high"):
        controller.start_sweep_mode(
            SweepParameters(
                start_field_nt=10000.0,
                end_field_nt=10000.0,
                step_field_nt=1000.0,
                dwell_time_s=5.0,
            )
        )

    # 3. Negative or zero step_field_nt must be rejected
    with pytest.raises(ValueError, match="step_field_nt"):
        controller.start_sweep_mode(
            SweepParameters(
                start_field_nt=5000.0,
                end_field_nt=15000.0,
                step_field_nt=-1000.0,
                dwell_time_s=5.0,
            )
        )

    # 4. Starting a sweep when capacitor is already charged above start_field_nt must be rejected
    assert controller.virtual_circuit is not None
    controller.virtual_circuit.reset(initial_voltage_v=15.0)  # 1.5 A = 15,000 nT
    with pytest.raises(ValueError, match="already higher than start_field_nt"):
        controller.start_sweep_mode(
            SweepParameters(
                start_field_nt=5000.0,
                end_field_nt=20000.0,
                step_field_nt=5000.0,
                dwell_time_s=5.0,
            )
        )


def test_sweep_mode_low_to_high_progression_and_final_hold(tmp_path) -> None:
    """Verify Sweep Mode steps from low to high after <1% settling + dwell, then holds final value."""
    cfg = AppConfig()
    cfg.logging.enabled = False
    cfg.logging.log_file_path = str(tmp_path / "log.csv")

    controller = SystemController(cfg)
    controller.connect_instruments()

    params = SweepParameters(
        start_field_nt=5000.0,
        end_field_nt=15000.0,
        step_field_nt=5000.0,
        dwell_time_s=10.0,
    )
    controller.start_sweep_mode(params)
    assert controller.sweep_steps_nt == [5000.0, 10000.0, 15000.0]
    assert controller.mode == OperationMode.SWEEP

    visited_targets: list[float] = []
    for _ in range(2000):
        sample = controller.update(2.0)
        if not visited_targets or visited_targets[-1] != sample.target_field_nt:
            visited_targets.append(sample.target_field_nt)
        if controller.mode == OperationMode.SET_FIELD and sample.within_tolerance:
            break

    assert visited_targets == [5000.0, 10000.0, 15000.0]
    assert controller.mode == OperationMode.SET_FIELD
    assert controller.target_field_nt == pytest.approx(15000.0)
    assert controller.latest_sample is not None
    assert controller.latest_sample.within_tolerance is True
    assert controller.latest_sample.holding_error_pct < 1.0


def test_safety_hard_limits_validation_and_emergency_cutoff(tmp_path) -> None:
    """Verify safety hard limits reject unsafe setpoints and trip FAULT cutoff on over-current."""
    cfg = AppConfig(safety=SafetyConfig(max_voltage_v=25.0, max_current_a=2.0))
    cfg.logging.enabled = False
    cfg.logging.log_file_path = str(tmp_path / "log.csv")

    controller = SystemController(cfg)
    controller.connect_instruments()

    # 1. Setpoint requiring 2.5 A (> 2.0 A max_current_a) must be rejected
    with pytest.raises(ValueError, match="max_current_a"):
        controller.start_set_field_mode(25000.0)

    # 2. Setpoint requiring 1.9 A coil + 0.19 A bleeder = 2.09 A (> 2.0 A max_current_a) rejected
    with pytest.raises(ValueError, match="max_current_a"):
        controller.start_set_field_mode(19000.0)

    # 3. Runtime over-current trips emergency cutoff (0 V, OP 0, FAULT mode)
    controller.start_set_field_mode(10000.0)
    assert controller.virtual_circuit is not None
    # Simulate an external fault spike on the capacitor exceeding max_current_a (2.2 A > 2.0 A)
    controller.virtual_circuit.capacitor_voltage_v = 22.0
    sample = controller.update(0.1)

    assert controller.mode == OperationMode.FAULT
    assert sample.mode == OperationMode.FAULT.value
    assert "Over-current fault" in sample.fault_message
    assert controller.psu.get_set_voltage() == pytest.approx(0.0)
    assert controller.psu.is_output_enabled() is False
