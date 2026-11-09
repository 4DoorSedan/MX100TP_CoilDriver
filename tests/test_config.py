"""Unit tests for configuration loading, saving, validation, and A/nT conversion math."""

from __future__ import annotations

from pathlib import Path

import pytest

from coil_driver.config import (
    AppConfig,
    CircuitModelConfig,
    ConversionConfig,
    InstrumentConfig,
    LoggingConfig,
    PIDConfig,
    SafetyConfig,
    load_config,
    save_config,
)


def test_load_default_project_config() -> None:
    """Verify the root config.ini loads cleanly and matches expected defaults."""
    root_cfg = Path(__file__).resolve().parent.parent / "config.ini"
    cfg = load_config(root_cfg)
    assert cfg.instruments.virtual_mode is True
    assert cfg.instruments.psu_port == 9221
    assert cfg.instruments.dmm_port == 5025
    assert cfg.safety.max_voltage_v == pytest.approx(35.0)
    assert cfg.safety.max_current_a == pytest.approx(3.0)
    assert cfg.conversion.a_per_nt == pytest.approx(0.0001)
    assert cfg.circuit_model.capacitance_f == pytest.approx(330.0)


def test_save_and_reload_config_roundtrip(tmp_path: Path) -> None:
    """Verify saving and reloading an AppConfig preserves all fields."""
    custom = AppConfig(
        instruments=InstrumentConfig(
            virtual_mode=False,
            psu_ip="10.0.0.10",
            psu_port=9221,
            psu_channel=2,
            dmm_ip="10.0.0.11",
            dmm_port=5025,
            socket_timeout_s=3.5,
        ),
        safety=SafetyConfig(max_voltage_v=28.0, max_current_a=2.5),
        conversion=ConversionConfig(a_per_nt=0.0002),
        pid=PIDConfig(
            kp=15.0,
            ki=0.25,
            kd=20.0,
            holding_tolerance_ratio=0.008,
            control_interval_s=0.05,
            max_overdrive_v=2.0,
        ),
        circuit_model=CircuitModelConfig(
            capacitance_f=330.0,
            load_resistance_ohm=12.0,
            bleeder_resistance_ohm=120.0,
            source_resistance_ohm=0.4,
        ),
        logging=LoggingConfig(
            enabled=True,
            log_interval_s=2.5,
            log_file_path="logs/custom_test.csv",
        ),
    )

    cfg_file = tmp_path / "custom.ini"
    save_config(custom, cfg_file)
    reloaded = load_config(cfg_file)

    assert reloaded.instruments.virtual_mode is False
    assert reloaded.instruments.psu_ip == "10.0.0.10"
    assert reloaded.instruments.psu_channel == 2
    assert reloaded.safety.max_voltage_v == pytest.approx(28.0)
    assert reloaded.safety.max_current_a == pytest.approx(2.5)
    assert reloaded.conversion.a_per_nt == pytest.approx(0.0002)
    assert reloaded.pid.kp == pytest.approx(15.0)
    assert reloaded.pid.holding_tolerance_ratio == pytest.approx(0.008)
    assert reloaded.circuit_model.load_resistance_ohm == pytest.approx(12.0)
    assert reloaded.logging.log_interval_s == pytest.approx(2.5)


def test_field_current_conversion_math() -> None:
    """Verify A/nT conversion rate in both directions."""
    cfg = AppConfig(conversion=ConversionConfig(a_per_nt=0.0001))
    assert cfg.field_nt_to_current_a(10000.0) == pytest.approx(1.0)
    assert cfg.current_a_to_field_nt(1.0) == pytest.approx(10000.0)
    assert cfg.field_nt_to_current_a(2500.0) == pytest.approx(0.25)
    assert cfg.current_a_to_field_nt(0.25) == pytest.approx(2500.0)


def test_invalid_config_raises_value_error(tmp_path: Path) -> None:
    """Verify invalid safety limits, channels, or conversion factors raise ValueError."""
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nonexistent.ini")

    with pytest.raises(ValueError, match="psu_channel"):
        InstrumentConfig(psu_channel=4).validate()

    with pytest.raises(ValueError, match="max_voltage_v"):
        SafetyConfig(max_voltage_v=0.0).validate()

    with pytest.raises(ValueError, match="max_current_a"):
        SafetyConfig(max_current_a=-1.0).validate()

    with pytest.raises(ValueError, match="a_per_nt"):
        ConversionConfig(a_per_nt=0.0).validate()

    with pytest.raises(ValueError, match="capacitance_f"):
        CircuitModelConfig(capacitance_f=-330.0).validate()
