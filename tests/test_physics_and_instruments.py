"""Unit tests for the 330F RC physics model and SCPI/Virtual instrument drivers."""

from __future__ import annotations

import socket
import threading

import pytest

from coil_driver.config import CircuitModelConfig, SafetyConfig
from coil_driver.instruments import (
    Keysight34461A,
    TTiMX100TP,
    VirtualKeysight34461A,
    VirtualTTiMX100TP,
)
from coil_driver.physics import VirtualCircuitModel


def test_virtual_circuit_charging_and_steady_state() -> None:
    """Verify the 330F capacitor charges smoothly and settles to exact Ohm's law equilibrium."""
    circuit_cfg = CircuitModelConfig(
        capacitance_f=330.0,
        load_resistance_ohm=10.0,
        bleeder_resistance_ohm=100.0,
        source_resistance_ohm=0.5,
    )
    model = VirtualCircuitModel(circuit_cfg, initial_voltage_v=0.0)

    # Set PSU to steady-state voltage required for 1.0 A coil current (10.55 V)
    v_ss = circuit_cfg.steady_state_psu_voltage_for_coil_current(1.0)
    assert v_ss == pytest.approx(10.55)

    model.psu_current_limit_a = 3.0
    model.psu_set_voltage_v = v_ss
    model.psu_output_enabled = True

    # Initially at t=0, capacitor voltage is 0V, PSU is current-limited at 3.0 A
    assert model.capacitor_voltage_v == pytest.approx(0.0)
    assert model.psu_measured_current_a == pytest.approx(3.0)

    # Step forward 3000 seconds so the 330 F capacitor reaches steady state
    for _ in range(300):
        model.step(10.0)

    assert model.capacitor_voltage_v == pytest.approx(10.0, rel=1e-4)
    assert model.coil_current_a == pytest.approx(1.0, rel=1e-4)
    assert model.bleeder_current_a == pytest.approx(0.1, rel=1e-4)
    assert model.psu_measured_current_a == pytest.approx(1.1, rel=1e-4)


def test_virtual_circuit_one_quadrant_passive_discharge() -> None:
    """Verify the 1-quadrant PSU cannot sink current and capacitor discharges via R_bleed || R_load."""
    circuit_cfg = CircuitModelConfig(
        capacitance_f=330.0,
        load_resistance_ohm=10.0,
        bleeder_resistance_ohm=100.0,
        source_resistance_ohm=0.5,
    )
    model = VirtualCircuitModel(circuit_cfg, initial_voltage_v=10.0)
    model.psu_output_enabled = True
    model.psu_set_voltage_v = 0.0  # Lower than V_cap (10.0 V)

    # 1-quadrant supply sources 0.0 A (cannot pull negative current)
    assert model.psu_measured_current_a == pytest.approx(0.0)

    # Parallel resistance R_p = (10 * 100) / (10 + 100) = 9.090909 Ohm
    # Time constant tau = R_p * C = 9.090909 * 330 = 3000.0 s
    tau = circuit_cfg.parallel_resistance_ohm * circuit_cfg.capacitance_f
    assert tau == pytest.approx(3000.0)

    # After 1 time constant, V_cap should decay to 10.0 * exp(-1)
    model.step(tau)
    import math

    assert model.capacitor_voltage_v == pytest.approx(10.0 * math.exp(-1.0), rel=1e-3)


def test_virtual_instruments_safety_clamping_and_scpi_log() -> None:
    """Verify VirtualTTiMX100TP and VirtualKeysight34461A clamp commands and log SCPI traffic."""
    model = VirtualCircuitModel()
    safety = SafetyConfig(max_voltage_v=30.0, max_current_a=2.5)
    psu = VirtualTTiMX100TP(model, channel=1, safety=safety)
    dmm = VirtualKeysight34461A(model)

    with pytest.raises(RuntimeError):
        psu.set_voltage(5.0)
    with pytest.raises(RuntimeError):
        dmm.measure_current_dc()

    psu.connect()
    dmm.connect()

    # Attempt to set voltage above safety hard limit (50 V > 30 V)
    psu.set_voltage(50.0)
    assert psu.get_set_voltage() == pytest.approx(30.0)

    # Attempt to set current limit above safety hard limit (10 A > 2.5 A)
    psu.set_current_limit(10.0)
    assert model.psu_current_limit_a == pytest.approx(2.5)

    psu.set_output(True)
    assert psu.is_output_enabled() is True

    model.step(5.0)
    assert dmm.measure_current_dc() > 0.0
    assert "V1 30.0000" in psu.command_log
    assert "I1 2.5000" in psu.command_log
    assert "OP1 1" in psu.command_log
    assert "MEAS:CURR:DC?" in dmm.command_log


def test_real_scpi_drivers_over_loopback_tcp_socket() -> None:
    """Verify TTiMX100TP and Keysight34461A TCP socket SCPI drivers against a local TCP server."""
    received_commands: list[str] = []

    def handle_client(conn: socket.socket) -> None:
        with conn:
            buf = b""
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode("ascii").strip()
                    received_commands.append(cmd)
                    if cmd == "V1?":
                        conn.sendall(b"V1 12.50\r\n")
                    elif cmd == "V1O?":
                        conn.sendall(b"12.48V\r\n")
                    elif cmd == "I1O?":
                        conn.sendall(b"1.234A\r\n")
                    elif cmd == "OP1?":
                        conn.sendall(b"1\r\n")
                    elif cmd == "MEAS:CURR:DC?":
                        conn.sendall(b"+1.23450000E+00\n")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    port = server.getsockname()[1]

    threads: list[threading.Thread] = []

    def accept_loop() -> None:
        for _ in range(2):
            conn, _ = server.accept()
            t = threading.Thread(target=handle_client, args=(conn,), daemon=True)
            t.start()
            threads.append(t)

    accept_thread = threading.Thread(target=accept_loop, daemon=True)
    accept_thread.start()

    safety = SafetyConfig(max_voltage_v=35.0, max_current_a=3.0)
    psu = TTiMX100TP(ip="127.0.0.1", port=port, channel=1, safety=safety, timeout_s=2.0)
    dmm = Keysight34461A(ip="127.0.0.1", port=port, timeout_s=2.0)

    try:
        psu.connect()
        dmm.connect()

        psu.set_voltage(12.5)
        psu.set_output(True)
        assert psu.get_set_voltage() == pytest.approx(12.5)
        assert psu.get_measured_voltage() == pytest.approx(12.48)
        assert psu.get_measured_current() == pytest.approx(1.234)
        assert psu.is_output_enabled() is True
        assert dmm.measure_current_dc() == pytest.approx(1.2345)
    finally:
        psu.disconnect()
        dmm.disconnect()
        server.close()

    assert "V1 12.5000" in received_commands
    assert "OP1 1" in received_commands
    assert "MEAS:CURR:DC?" in received_commands
