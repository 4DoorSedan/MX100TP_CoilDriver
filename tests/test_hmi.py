"""Unit tests for the headless HMI touch-stepping and layout calculation helpers."""

from __future__ import annotations

import pytest

from coil_driver.config import AppConfig
from coil_driver.gui import CoilDriverGUI
from coil_driver.hmi import (
    compute_single_plot_height,
    compute_stacked_plot_height,
    format_toggle_label,
    step_numeric_value,
)


def test_step_numeric_value_increments_and_decrements_cleanly() -> None:
    """Verify touch [+]/[-] step math and decimal rounding."""
    assert step_numeric_value(10000.0, 1000.0, min_value=0.0, decimals=2) == pytest.approx(11000.0)
    assert step_numeric_value(10000.0, -100.0, min_value=0.0, decimals=2) == pytest.approx(9900.0)
    assert step_numeric_value(0.35, 0.05, min_value=0.0, decimals=3) == pytest.approx(0.40)
    assert step_numeric_value(0.35, -0.05, min_value=0.0, decimals=3) == pytest.approx(0.30)


def test_step_numeric_value_clamps_to_min_and_max() -> None:
    """Verify touch step buttons never push values below min_value or above max_value."""
    assert step_numeric_value(500.0, -1000.0, min_value=0.0, decimals=2) == pytest.approx(0.0)
    assert step_numeric_value(1.0, -0.5, min_value=1.0, decimals=1) == pytest.approx(1.0)
    assert step_numeric_value(48.0, 5.0, min_value=1.0, max_value=50.0, decimals=1) == pytest.approx(
        50.0
    )


def test_format_toggle_label() -> None:
    """Verify ON/OFF toggle button label formatting."""
    assert format_toggle_label("Virtual 330F Mode", True) == "Virtual 330F Mode: ON"
    assert format_toggle_label("Virtual 330F Mode", False) == "Virtual 330F Mode: OFF"
    assert format_toggle_label("CSV Logging", True) == "CSV Logging: ON"
    assert format_toggle_label("Auto-Fit Axes", False) == "Auto-Fit Axes: OFF"


def test_compute_stacked_and_single_plot_heights() -> None:
    """Verify responsive plot heights for 1280x800 and 1366x768 HMI screens."""
    # 800 px viewport height: (800 - 210) // 3 = 196 px per stacked plot
    h_800 = compute_stacked_plot_height(800)
    assert h_800 == 196
    assert h_800 * 3 + 210 <= 800

    # 768 px viewport height: (768 - 210) // 3 = 186 px per stacked plot
    h_768 = compute_stacked_plot_height(768)
    assert h_768 == 186
    assert h_768 * 3 + 210 <= 768

    # Minimum floor clamping for tiny heights
    assert compute_stacked_plot_height(300, min_plot_height=150) == 150
    assert compute_single_plot_height(300, min_plot_height=400) == 400
    assert compute_single_plot_height(800) == 610

    with pytest.raises(ValueError, match="num_plots"):
        compute_stacked_plot_height(800, num_plots=0)


def test_coil_driver_gui_headless_initialization_and_toggles(tmp_path) -> None:
    """Verify CoilDriverGUI initializes headlessly, builds widget layout, and handles touch callbacks."""
    import dearpygui.dearpygui as dpg

    cfg = AppConfig()
    cfg.logging.enabled = False
    cfg.logging.log_file_path = str(tmp_path / "log.csv")

    gui = CoilDriverGUI(config=cfg, config_path=tmp_path / "config.ini")
    assert gui.controller.connected is True
    assert gui.autofit_plots is True

    # Test callbacks before DPG context is created (safe no-ops on UI items)
    gui._on_toggle_autofit_btn()
    assert gui.autofit_plots is False
    gui._on_toggle_autofit_btn()
    assert gui.autofit_plots is True

    # Now create an in-memory DPG context (no display/viewport required) and verify full layout & callbacks
    dpg.create_context()
    gui._dpg_context_ready = True
    try:
        gui._build_window_layout()

        # Verify key touch inputs, toggle buttons, and both stacked + single plot tabs exist
        for tag in (
            "primary_window",
            "control_tab_bar",
            "plot_tab_bar",
            "input_set_field_nt",
            "input_sweep_start_nt",
            "input_sweep_end_nt",
            "input_sweep_step_nt",
            "input_sweep_dwell_s",
            "btn_toggle_virtual_mode",
            "btn_toggle_logging",
            "btn_toggle_autofit",
            "plot_field_stacked",
            "plot_volt_stacked",
            "plot_curr_stacked",
            "plot_field_single",
            "plot_volt_single",
            "plot_curr_single",
        ):
            assert dpg.does_item_exist(tag), f"Expected HMI widget tag '{tag}' to exist"

        # Verify touch step adjustment and preset helpers update the DPG input widgets
        gui._set_input_field("input_set_field_nt", 5000.0)
        assert dpg.get_value("input_set_field_nt") == pytest.approx(5000.0)
        gui._step_input_field("input_set_field_nt", 1000.0, min_value=0.0, decimals=2)
        assert dpg.get_value("input_set_field_nt") == pytest.approx(6000.0)

        # Verify starting Set Field mode, ticking plot updates, and stopping output
        gui._on_start_set_field()
        gui._tick()
        assert "Set Field Mode active" in gui._status_banner_msg

        gui._on_reset_virtual_capacitor()
        assert "discharged to 0.0 V" in gui._status_banner_msg

        gui._on_stop_output()
        assert "Output stopped" in gui._status_banner_msg
    finally:
        gui._dpg_context_ready = False
        gui.controller.disconnect_instruments()
        dpg.destroy_context()


def _simulate_dpg_button_press(tag: str) -> None:
    """Invoke a Dear PyGui button's registered callback using DPG's exact C++ co_argcount dispatch rules.

    In Dear PyGui's C++ mvRunCallback:
      - co_argcount == 0 -> callback()
      - co_argcount == 1 -> callback(sender)
      - co_argcount == 2 -> callback(sender, app_data)
      - co_argcount == 3 -> callback(sender, app_data, user_data)
      - co_argcount > 3  -> ignored by Dear PyGui (never called!)
    """
    import dearpygui.dearpygui as dpg

    assert dpg.does_item_exist(tag), f"Button '{tag}' does not exist"
    cb = dpg.get_item_callback(tag)
    assert cb is not None, f"Button '{tag}' has no callback registered"
    user_data = dpg.get_item_user_data(tag)

    argcount = cb.__code__.co_argcount
    assert 0 <= argcount <= 3, (
        f"Button '{tag}' callback has co_argcount={argcount}; "
        "Dear PyGui ignores callbacks with >3 positional arguments!"
    )
    if argcount == 0:
        cb()
    elif argcount == 1:
        cb(tag)
    elif argcount == 2:
        cb(tag, None)
    else:
        cb(tag, None, user_data)


def test_all_hmi_step_and_preset_buttons_via_dpg_callback_dispatch(tmp_path) -> None:
    """Press every Set Field, Preset, Sweep, Sim Speed, PID, and Logging button via DPG dispatch."""
    import dearpygui.dearpygui as dpg

    cfg = AppConfig()
    cfg.logging.enabled = False
    cfg.logging.log_file_path = str(tmp_path / "log.csv")

    gui = CoilDriverGUI(config=cfg, config_path=tmp_path / "config.ini")
    dpg.create_context()
    gui._dpg_context_ready = True
    try:
        gui._build_window_layout()

        # 1. Set Field Preset buttons (0, 20000, 45000, 70000 nT)
        for btn_tag, expected_val in [
            ("btn_set_preset_0", 0.0),
            ("btn_set_preset_20000", 20000.0),
            ("btn_set_preset_45000", 45000.0),
            ("btn_set_preset_70000", 70000.0),
        ]:
            _simulate_dpg_button_press(btn_tag)
            assert dpg.get_value("input_set_field_nt") == pytest.approx(expected_val)

        # 2. Set Field Step buttons (-1000, -100, +100, +1000 nT) starting from 20,000 nT
        _simulate_dpg_button_press("btn_set_preset_20000")
        _simulate_dpg_button_press("btn_set_step_m1000")
        assert dpg.get_value("input_set_field_nt") == pytest.approx(19000.0)
        _simulate_dpg_button_press("btn_set_step_m100")
        assert dpg.get_value("input_set_field_nt") == pytest.approx(18900.0)
        _simulate_dpg_button_press("btn_set_step_p100")
        assert dpg.get_value("input_set_field_nt") == pytest.approx(19000.0)
        _simulate_dpg_button_press("btn_set_step_p1000")
        assert dpg.get_value("input_set_field_nt") == pytest.approx(20000.0)

        # 3. Sweep Mode -500 / +500 / -1s / +1s buttons
        assert dpg.get_value("input_sweep_start_nt") == pytest.approx(5000.0)
        _simulate_dpg_button_press("btn_sweep_start_plus")
        assert dpg.get_value("input_sweep_start_nt") == pytest.approx(5500.0)
        _simulate_dpg_button_press("btn_sweep_start_minus")
        assert dpg.get_value("input_sweep_start_nt") == pytest.approx(5000.0)

        assert dpg.get_value("input_sweep_end_nt") == pytest.approx(20000.0)
        _simulate_dpg_button_press("btn_sweep_end_plus")
        assert dpg.get_value("input_sweep_end_nt") == pytest.approx(20500.0)
        _simulate_dpg_button_press("btn_sweep_end_minus")
        assert dpg.get_value("input_sweep_end_nt") == pytest.approx(20000.0)

        assert dpg.get_value("input_sweep_step_nt") == pytest.approx(5000.0)
        _simulate_dpg_button_press("btn_sweep_step_plus")
        assert dpg.get_value("input_sweep_step_nt") == pytest.approx(5500.0)
        _simulate_dpg_button_press("btn_sweep_step_minus")
        assert dpg.get_value("input_sweep_step_nt") == pytest.approx(5000.0)

        assert dpg.get_value("input_sweep_dwell_s") == pytest.approx(10.0)
        _simulate_dpg_button_press("btn_sweep_dwell_plus")
        assert dpg.get_value("input_sweep_dwell_s") == pytest.approx(11.0)
        _simulate_dpg_button_press("btn_sweep_dwell_minus")
        assert dpg.get_value("input_sweep_dwell_s") == pytest.approx(10.0)

        # 4. Sim Speed -5x / +5x buttons
        assert dpg.get_value("input_sim_speed") == pytest.approx(5.0)
        _simulate_dpg_button_press("btn_sim_speed_plus")
        assert dpg.get_value("input_sim_speed") == pytest.approx(10.0)
        _simulate_dpg_button_press("btn_sim_speed_minus")
        assert dpg.get_value("input_sim_speed") == pytest.approx(5.0)

        # 5. PID Kp, Ki, Kd step buttons (also verifies after_change updates config.pid)
        kp_init = cfg.pid.kp
        _simulate_dpg_button_press("btn_pid_kp_plus")
        assert dpg.get_value("input_pid_kp") == pytest.approx(kp_init + 1.0)
        assert gui.config.pid.kp == pytest.approx(kp_init + 1.0)
        _simulate_dpg_button_press("btn_pid_kp_minus")
        assert dpg.get_value("input_pid_kp") == pytest.approx(kp_init)

        ki_init = cfg.pid.ki
        _simulate_dpg_button_press("btn_pid_ki_plus")
        assert dpg.get_value("input_pid_ki") == pytest.approx(ki_init + 0.05)
        assert gui.config.pid.ki == pytest.approx(ki_init + 0.05)
        _simulate_dpg_button_press("btn_pid_ki_minus")
        assert dpg.get_value("input_pid_ki") == pytest.approx(ki_init)

        kd_init = cfg.pid.kd
        _simulate_dpg_button_press("btn_pid_kd_plus")
        assert dpg.get_value("input_pid_kd") == pytest.approx(kd_init + 1.0)
        assert gui.config.pid.kd == pytest.approx(kd_init + 1.0)
        _simulate_dpg_button_press("btn_pid_kd_minus")
        assert dpg.get_value("input_pid_kd") == pytest.approx(kd_init)

        # 6. Log interval -0.5s / +0.5s buttons
        assert dpg.get_value("input_log_interval_s") == pytest.approx(1.0)
        _simulate_dpg_button_press("btn_log_interval_plus")
        assert dpg.get_value("input_log_interval_s") == pytest.approx(1.5)
        assert gui.config.logging.log_interval_s == pytest.approx(1.5)
        _simulate_dpg_button_press("btn_log_interval_minus")
        assert dpg.get_value("input_log_interval_s") == pytest.approx(1.0)

        # 7. Action and Toggle buttons
        _simulate_dpg_button_press("btn_hold_target_field")
        assert gui.controller.mode.value == "SET_FIELD"
        _simulate_dpg_button_press("btn_stop_top")
        assert gui.controller.mode.value == "IDLE"
        _simulate_dpg_button_press("btn_start_sweep")
        assert gui.controller.mode.value == "SWEEP"
        _simulate_dpg_button_press("btn_stop_tab1")
        assert gui.controller.mode.value == "IDLE"
    finally:
        gui._dpg_context_ready = False
        gui.controller.disconnect_instruments()
        dpg.destroy_context()

