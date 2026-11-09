"""SCPI instrument drivers and virtual hardware mocks for TTi MX100TP and Keysight 34461A."""

from __future__ import annotations

import re
import socket
from abc import ABC, abstractmethod

from .config import SafetyConfig
from .physics import VirtualCircuitModel

_FLOAT_PATTERN = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _parse_scpi_float(response: str) -> float:
    """Extract a floating-point number from an instrument SCPI response string.

    Handles both plain numbers ('+1.23450000E-01') and suffixed responses
    from the TTi MX100TP ('12.34V', 'V1 12.34', '0.500A').
    """
    cleaned = response.strip()
    # If response starts with a channel prefix like 'V1 12.34' or 'I2 0.50', strip the prefix token
    parts = cleaned.split()
    target = parts[-1] if len(parts) > 1 else cleaned
    match = _FLOAT_PATTERN.search(target)
    if not match:
        raise ValueError(f"Could not parse float from SCPI response: {response!r}")
    return float(match.group(0))


class PowerSupplyInterface(ABC):
    """Abstract interface for a single channel of the TTi MX100TP power supply."""

    @abstractmethod
    def connect(self) -> None:
        """Open connection to the power supply."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection to the power supply."""

    @abstractmethod
    def set_voltage(self, voltage_v: float) -> None:
        """Set target output voltage in Volts (clamped to safety limits)."""

    @abstractmethod
    def set_current_limit(self, current_a: float) -> None:
        """Set output current limit in Amperes (clamped to safety limits)."""

    @abstractmethod
    def set_output(self, enabled: bool) -> None:
        """Enable or disable the power supply output."""

    @abstractmethod
    def get_set_voltage(self) -> float:
        """Return the currently configured setpoint voltage in Volts."""

    @abstractmethod
    def get_measured_voltage(self) -> float:
        """Query the actual terminal output voltage in Volts."""

    @abstractmethod
    def get_measured_current(self) -> float:
        """Query the actual sourced output current in Amperes."""

    @abstractmethod
    def is_output_enabled(self) -> bool:
        """Return whether the channel output is enabled."""


class MultimeterInterface(ABC):
    """Abstract interface for the Keysight 34461A digital multimeter."""

    @abstractmethod
    def connect(self) -> None:
        """Open connection to the multimeter."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection to the multimeter."""

    @abstractmethod
    def measure_current_dc(self) -> float:
        """Measure and return DC current in Amperes."""


class TTiMX100TP(PowerSupplyInterface):
    """Ethernet SCPI driver for the Aim-TTi MX100TP triple-output DC power supply."""

    def __init__(
        self,
        ip: str,
        port: int = 9221,
        channel: int = 1,
        safety: SafetyConfig | None = None,
        timeout_s: float = 2.0,
    ) -> None:
        if channel not in (1, 2, 3):
            raise ValueError(f"TTi MX100TP channel must be 1, 2, or 3, got {channel}.")
        self.ip = ip
        self.port = port
        self.channel = channel
        self.safety = safety or SafetyConfig()
        self.timeout_s = timeout_s
        self._sock: socket.socket | None = None
        self._last_set_voltage_v: float = 0.0
        self._output_enabled: bool = False

    def connect(self) -> None:
        """Connect to the TTi MX100TP TCP socket and initialize safe state."""
        if self._sock is not None:
            return
        sock = socket.create_connection((self.ip, self.port), timeout=self.timeout_s)
        sock.settimeout(self.timeout_s)
        self._sock = sock
        # Initialize current limit and 0 V output for safety
        self.set_current_limit(self.safety.max_current_a)
        self.set_voltage(0.0)

    def disconnect(self) -> None:
        """Close the TCP socket connection."""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _send_command(self, cmd: str) -> None:
        if self._sock is None:
            raise RuntimeError("TTiMX100TP is not connected.")
        payload = f"{cmd.strip()}\n".encode("ascii")
        self._sock.sendall(payload)

    def _query(self, cmd: str) -> str:
        if self._sock is None:
            raise RuntimeError("TTiMX100TP is not connected.")
        self._send_command(cmd)
        chunks: list[bytes] = []
        while True:
            chunk = self._sock.recv(1024)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        return b"".join(chunks).decode("ascii", errors="replace").strip()

    def set_voltage(self, voltage_v: float) -> None:
        """Set output voltage (V<ch> <val>), strictly clamped to [0.0, max_voltage_v]."""
        clamped_v = max(0.0, min(float(voltage_v), self.safety.max_voltage_v))
        self._send_command(f"V{self.channel} {clamped_v:.4f}")
        self._last_set_voltage_v = clamped_v

    def set_current_limit(self, current_a: float) -> None:
        """Set output current limit (I<ch> <val>), strictly clamped to [0.0, max_current_a]."""
        clamped_i = max(0.0, min(float(current_a), self.safety.max_current_a))
        self._send_command(f"I{self.channel} {clamped_i:.4f}")

    def set_output(self, enabled: bool) -> None:
        """Enable or disable channel output (OP<ch> 1 / OP<ch> 0)."""
        flag = 1 if enabled else 0
        self._send_command(f"OP{self.channel} {flag}")
        self._output_enabled = bool(enabled)

    def get_set_voltage(self) -> float:
        """Query the setpoint voltage (V<ch>?)."""
        resp = self._query(f"V{self.channel}?")
        self._last_set_voltage_v = _parse_scpi_float(resp)
        return self._last_set_voltage_v

    def get_measured_voltage(self) -> float:
        """Query the measured output readback voltage (V<ch>O?)."""
        resp = self._query(f"V{self.channel}O?")
        return _parse_scpi_float(resp)

    def get_measured_current(self) -> float:
        """Query the measured output readback current (I<ch>O?)."""
        resp = self._query(f"I{self.channel}O?")
        return _parse_scpi_float(resp)

    def is_output_enabled(self) -> bool:
        """Query the output enable state (OP<ch>?)."""
        resp = self._query(f"OP{self.channel}?")
        val = int(_parse_scpi_float(resp))
        self._output_enabled = val != 0
        return self._output_enabled


class Keysight34461A(MultimeterInterface):
    """Ethernet SCPI driver for the Keysight 34461A Truevolt Digital Multimeter."""

    def __init__(self, ip: str, port: int = 5025, timeout_s: float = 2.0) -> None:
        self.ip = ip
        self.port = port
        self.timeout_s = timeout_s
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        """Connect to the Keysight 34461A SCPI TCP socket."""
        if self._sock is not None:
            return
        sock = socket.create_connection((self.ip, self.port), timeout=self.timeout_s)
        sock.settimeout(self.timeout_s)
        self._sock = sock

    def disconnect(self) -> None:
        """Close the TCP socket connection."""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _query(self, cmd: str) -> str:
        if self._sock is None:
            raise RuntimeError("Keysight34461A is not connected.")
        payload = f"{cmd.strip()}\n".encode("ascii")
        self._sock.sendall(payload)
        chunks: list[bytes] = []
        while True:
            chunk = self._sock.recv(1024)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        return b"".join(chunks).decode("ascii", errors="replace").strip()

    def measure_current_dc(self) -> float:
        """Query DC current in Amperes via MEAS:CURR:DC?."""
        resp = self._query("MEAS:CURR:DC?")
        return _parse_scpi_float(resp)


class VirtualTTiMX100TP(PowerSupplyInterface):
    """Virtual mocked TTi MX100TP backed by a shared VirtualCircuitModel."""

    def __init__(
        self,
        circuit: VirtualCircuitModel,
        channel: int = 1,
        safety: SafetyConfig | None = None,
    ) -> None:
        if channel not in (1, 2, 3):
            raise ValueError(f"TTi MX100TP channel must be 1, 2, or 3, got {channel}.")
        self.circuit = circuit
        self.channel = channel
        self.safety = safety or SafetyConfig()
        self.connected: bool = False
        self.command_log: list[str] = []

    def connect(self) -> None:
        self.connected = True
        self.set_current_limit(self.safety.max_current_a)
        self.set_voltage(0.0)

    def disconnect(self) -> None:
        self.connected = False

    def _ensure_connected(self) -> None:
        if not self.connected:
            raise RuntimeError("VirtualTTiMX100TP is not connected.")

    def set_voltage(self, voltage_v: float) -> None:
        self._ensure_connected()
        clamped_v = max(0.0, min(float(voltage_v), self.safety.max_voltage_v))
        self.circuit.psu_set_voltage_v = clamped_v
        self.command_log.append(f"V{self.channel} {clamped_v:.4f}")

    def set_current_limit(self, current_a: float) -> None:
        self._ensure_connected()
        clamped_i = max(0.0, min(float(current_a), self.safety.max_current_a))
        self.circuit.psu_current_limit_a = clamped_i
        self.command_log.append(f"I{self.channel} {clamped_i:.4f}")

    def set_output(self, enabled: bool) -> None:
        self._ensure_connected()
        self.circuit.psu_output_enabled = bool(enabled)
        self.command_log.append(f"OP{self.channel} {1 if enabled else 0}")

    def get_set_voltage(self) -> float:
        self._ensure_connected()
        self.command_log.append(f"V{self.channel}?")
        return self.circuit.psu_set_voltage_v

    def get_measured_voltage(self) -> float:
        self._ensure_connected()
        self.command_log.append(f"V{self.channel}O?")
        return self.circuit.psu_measured_voltage_v

    def get_measured_current(self) -> float:
        self._ensure_connected()
        self.command_log.append(f"I{self.channel}O?")
        return self.circuit.psu_measured_current_a

    def is_output_enabled(self) -> bool:
        self._ensure_connected()
        self.command_log.append(f"OP{self.channel}?")
        return self.circuit.psu_output_enabled


class VirtualKeysight34461A(MultimeterInterface):
    """Virtual mocked Keysight 34461A multimeter backed by a shared VirtualCircuitModel."""

    def __init__(self, circuit: VirtualCircuitModel) -> None:
        self.circuit = circuit
        self.connected: bool = False
        self.command_log: list[str] = []

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def measure_current_dc(self) -> float:
        if not self.connected:
            raise RuntimeError("VirtualKeysight34461A is not connected.")
        self.command_log.append("MEAS:CURR:DC?")
        return self.circuit.coil_current_a
