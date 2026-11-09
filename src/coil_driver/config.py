"""Configuration management and unit conversion utilities for the solenoid coil driver."""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InstrumentConfig:
    """Network and channel configuration for the SCPI instruments."""

    virtual_mode: bool = True
    psu_ip: str = "192.168.1.100"
    psu_port: int = 9221
    psu_channel: int = 1
    dmm_ip: str = "192.168.1.101"
    dmm_port: int = 5025
    socket_timeout_s: float = 2.0

    def validate(self) -> None:
        """Validate instrument configuration fields."""
        if not self.psu_ip.strip():
            raise ValueError("psu_ip must not be empty.")
        if not self.dmm_ip.strip():
            raise ValueError("dmm_ip must not be empty.")
        if not (1 <= self.psu_port <= 65535):
            raise ValueError(f"psu_port must be in 1..65535, got {self.psu_port}.")
        if not (1 <= self.dmm_port <= 65535):
            raise ValueError(f"dmm_port must be in 1..65535, got {self.dmm_port}.")
        if self.psu_channel not in (1, 2, 3):
            raise ValueError(f"TTi MX100TP psu_channel must be 1, 2, or 3, got {self.psu_channel}.")
        if self.socket_timeout_s <= 0:
            raise ValueError(f"socket_timeout_s must be > 0, got {self.socket_timeout_s}.")


@dataclass
class SafetyConfig:
    """Hard safety limits for power supply voltage and current."""

    max_voltage_v: float = 35.0
    max_current_a: float = 3.0

    def validate(self) -> None:
        """Validate safety hard limits."""
        if self.max_voltage_v <= 0:
            raise ValueError(f"max_voltage_v must be > 0, got {self.max_voltage_v}.")
        if self.max_current_a <= 0:
            raise ValueError(f"max_current_a must be > 0, got {self.max_current_a}.")


@dataclass
class ConversionConfig:
    """Conversion rate between coil current (Amperes) and solenoid magnetic field (nanoTesla)."""

    a_per_nt: float = 0.0001

    def validate(self) -> None:
        """Validate conversion factor."""
        if self.a_per_nt <= 0:
            raise ValueError(f"a_per_nt must be > 0, got {self.a_per_nt}.")

    def field_nt_to_current_a(self, field_nt: float) -> float:
        """Convert magnetic field in nanoTesla (nT) to coil current in Amperes (A)."""
        return field_nt * self.a_per_nt

    def current_a_to_field_nt(self, current_a: float) -> float:
        """Convert coil current in Amperes (A) to magnetic field in nanoTesla (nT)."""
        return current_a / self.a_per_nt


@dataclass
class PIDConfig:
    """PID control loop parameters and settling criteria."""

    kp: float = 18.0
    ki: float = 0.35
    kd: float = 25.0
    holding_tolerance_ratio: float = 0.01
    control_interval_s: float = 0.1
    max_overdrive_v: float = 2.5

    def validate(self) -> None:
        """Validate PID settings."""
        if self.kp < 0 or self.ki < 0 or self.kd < 0:
            raise ValueError("PID gains (kp, ki, kd) must be non-negative.")
        if not (0 < self.holding_tolerance_ratio < 1.0):
            raise ValueError(
                f"holding_tolerance_ratio must be in (0, 1), got {self.holding_tolerance_ratio}."
            )
        if self.control_interval_s <= 0:
            raise ValueError(f"control_interval_s must be > 0, got {self.control_interval_s}.")
        if self.max_overdrive_v < 0:
            raise ValueError(f"max_overdrive_v must be >= 0, got {self.max_overdrive_v}.")


@dataclass
class CircuitModelConfig:
    """Physical circuit parameters matching External Documentation/Circuit Diagram.jpg."""

    capacitance_f: float = 330.0
    load_resistance_ohm: float = 10.0
    bleeder_resistance_ohm: float = 100.0
    source_resistance_ohm: float = 0.5

    def validate(self) -> None:
        """Validate circuit model values."""
        if self.capacitance_f <= 0:
            raise ValueError(f"capacitance_f must be > 0, got {self.capacitance_f}.")
        if self.load_resistance_ohm <= 0:
            raise ValueError(f"load_resistance_ohm must be > 0, got {self.load_resistance_ohm}.")
        if self.bleeder_resistance_ohm <= 0:
            raise ValueError(
                f"bleeder_resistance_ohm must be > 0, got {self.bleeder_resistance_ohm}."
            )
        if self.source_resistance_ohm <= 0:
            raise ValueError(
                f"source_resistance_ohm must be > 0, got {self.source_resistance_ohm}."
            )

    @property
    def parallel_resistance_ohm(self) -> float:
        """Equivalent parallel resistance of the bleeder resistor and load resistor."""
        return 1.0 / (1.0 / self.load_resistance_ohm + 1.0 / self.bleeder_resistance_ohm)

    def steady_state_psu_voltage_for_coil_current(self, target_coil_current_a: float) -> float:
        """Compute steady-state PSU voltage needed to sustain a given coil current.

        At steady state (dV_c/dt = 0):
          V_c = I_coil * R_load
          I_bleed = V_c / R_bleed
          I_psu = I_coil + I_bleed
          V_psu = V_c + I_psu * R_source
        """
        v_cap = target_coil_current_a * self.load_resistance_ohm
        i_total = target_coil_current_a + (v_cap / self.bleeder_resistance_ohm)
        return v_cap + i_total * self.source_resistance_ohm


@dataclass
class LoggingConfig:
    """CSV data logging settings."""

    enabled: bool = True
    log_interval_s: float = 1.0
    log_file_path: str = "logs/coil_driver_log.csv"

    def validate(self) -> None:
        """Validate logging configuration."""
        if self.log_interval_s <= 0:
            raise ValueError(f"log_interval_s must be > 0, got {self.log_interval_s}.")
        if not self.log_file_path.strip():
            raise ValueError("log_file_path must not be empty.")


@dataclass
class AppConfig:
    """Complete application configuration."""

    instruments: InstrumentConfig = field(default_factory=InstrumentConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    conversion: ConversionConfig = field(default_factory=ConversionConfig)
    pid: PIDConfig = field(default_factory=PIDConfig)
    circuit_model: CircuitModelConfig = field(default_factory=CircuitModelConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def validate(self) -> None:
        """Validate all configuration sections."""
        self.instruments.validate()
        self.safety.validate()
        self.conversion.validate()
        self.pid.validate()
        self.circuit_model.validate()
        self.logging.validate()

    def field_nt_to_current_a(self, field_nt: float) -> float:
        """Convert magnetic field (nT) to coil current (A)."""
        return self.conversion.field_nt_to_current_a(field_nt)

    def current_a_to_field_nt(self, current_a: float) -> float:
        """Convert coil current (A) to magnetic field (nT)."""
        return self.conversion.current_a_to_field_nt(current_a)


def load_config(path: str | Path) -> AppConfig:
    """Load and validate an AppConfig from an INI file."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {file_path}")

    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    parser.read(file_path, encoding="utf-8")

    inst_sec = parser["instruments"] if "instruments" in parser else {}
    safety_sec = parser["safety"] if "safety" in parser else {}
    conv_sec = parser["conversion"] if "conversion" in parser else {}
    pid_sec = parser["pid"] if "pid" in parser else {}
    circuit_sec = parser["circuit_model"] if "circuit_model" in parser else {}
    log_sec = parser["logging"] if "logging" in parser else {}

    config = AppConfig(
        instruments=InstrumentConfig(
            virtual_mode=parser.getboolean("instruments", "virtual_mode", fallback=True),
            psu_ip=str(inst_sec.get("psu_ip", "192.168.1.100")).strip(),
            psu_port=int(inst_sec.get("psu_port", 9221)),
            psu_channel=int(inst_sec.get("psu_channel", 1)),
            dmm_ip=str(inst_sec.get("dmm_ip", "192.168.1.101")).strip(),
            dmm_port=int(inst_sec.get("dmm_port", 5025)),
            socket_timeout_s=float(inst_sec.get("socket_timeout_s", 2.0)),
        ),
        safety=SafetyConfig(
            max_voltage_v=float(safety_sec.get("max_voltage_v", 35.0)),
            max_current_a=float(safety_sec.get("max_current_a", 3.0)),
        ),
        conversion=ConversionConfig(
            a_per_nt=float(conv_sec.get("a_per_nt", 0.0001)),
        ),
        pid=PIDConfig(
            kp=float(pid_sec.get("kp", 18.0)),
            ki=float(pid_sec.get("ki", 0.35)),
            kd=float(pid_sec.get("kd", 25.0)),
            holding_tolerance_ratio=float(pid_sec.get("holding_tolerance_ratio", 0.01)),
            control_interval_s=float(pid_sec.get("control_interval_s", 0.1)),
            max_overdrive_v=float(pid_sec.get("max_overdrive_v", 2.5)),
        ),
        circuit_model=CircuitModelConfig(
            capacitance_f=float(circuit_sec.get("capacitance_f", 330.0)),
            load_resistance_ohm=float(circuit_sec.get("load_resistance_ohm", 10.0)),
            bleeder_resistance_ohm=float(circuit_sec.get("bleeder_resistance_ohm", 100.0)),
            source_resistance_ohm=float(circuit_sec.get("source_resistance_ohm", 0.5)),
        ),
        logging=LoggingConfig(
            enabled=parser.getboolean("logging", "enabled", fallback=True),
            log_interval_s=float(log_sec.get("log_interval_s", 1.0)),
            log_file_path=str(log_sec.get("log_file_path", "logs/coil_driver_log.csv")).strip(),
        ),
    )
    config.validate()
    return config


def save_config(config: AppConfig, path: str | Path) -> None:
    """Validate and save an AppConfig to an INI file."""
    config.validate()
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    parser = configparser.ConfigParser()
    parser["instruments"] = {
        "virtual_mode": str(config.instruments.virtual_mode).lower(),
        "psu_ip": config.instruments.psu_ip,
        "psu_port": str(config.instruments.psu_port),
        "psu_channel": str(config.instruments.psu_channel),
        "dmm_ip": config.instruments.dmm_ip,
        "dmm_port": str(config.instruments.dmm_port),
        "socket_timeout_s": str(config.instruments.socket_timeout_s),
    }
    parser["safety"] = {
        "max_voltage_v": str(config.safety.max_voltage_v),
        "max_current_a": str(config.safety.max_current_a),
    }
    parser["conversion"] = {
        "a_per_nt": str(config.conversion.a_per_nt),
    }
    parser["pid"] = {
        "kp": str(config.pid.kp),
        "ki": str(config.pid.ki),
        "kd": str(config.pid.kd),
        "holding_tolerance_ratio": str(config.pid.holding_tolerance_ratio),
        "control_interval_s": str(config.pid.control_interval_s),
        "max_overdrive_v": str(config.pid.max_overdrive_v),
    }
    parser["circuit_model"] = {
        "capacitance_f": str(config.circuit_model.capacitance_f),
        "load_resistance_ohm": str(config.circuit_model.load_resistance_ohm),
        "bleeder_resistance_ohm": str(config.circuit_model.bleeder_resistance_ohm),
        "source_resistance_ohm": str(config.circuit_model.source_resistance_ohm),
    }
    parser["logging"] = {
        "enabled": str(config.logging.enabled).lower(),
        "log_interval_s": str(config.logging.log_interval_s),
        "log_file_path": config.logging.log_file_path,
    }

    with file_path.open("w", encoding="utf-8") as fp:
        parser.write(fp)
