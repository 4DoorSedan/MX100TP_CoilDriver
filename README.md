# TTi MX100TP & Keysight 34461A Solenoid Magnetic Field Controller

A Python testing and control application for regulating the magnetic field inside a large solenoid by adjusting the output voltage of an **Aim-TTi MX100TP** DC power supply and monitoring coil current with a **Keysight 34461A** digital multimeter over Ethernet SCPI.

---

## Overview

### Circuit Topology & Physics
As illustrated in `External Documentation/Circuit Diagram.jpg`, the power supply drives a parallel network consisting of:
1. A **Bleeder Resistor** ($R_{\text{bleed}}$)
2. A **330 F Supercapacitor Bank** ($C = 330\text{ F}$)
3. The **Keysight 34461A Ammeter** in series with the **Solenoid Load Resistor** ($R_{\text{load}}$)

Because the 330 F capacitor introduces a large RC charging lag and the TTi MX100TP is a 1-quadrant power supply (unable to actively sink current from the capacitor), any voltage overshoot on the capacitor takes a long time to passively decay through the bleeder and load resistors.

To solve this, the application implements an **Anti-Overshoot PID + Feedforward Controller** (`src/coil_driver/pid.py`) with derivative-on-measurement braking and conditional anti-windup integration so the coil current gently encroaches onto the target setpoint without overshooting and holds steady-state accuracy within **<1%**.

### Key Features
- **Set Field Mode**: Holds the solenoid magnetic field constant at a target value (in nT) with `<1%` holding accuracy. Supports lower target fields by reducing PSU output to `0 V` and allowing passive RC discharge into the `<1%` band.
- **Sweep Mode (Strictly Low-to-High)**: Steps the magnetic field in discrete increments from `start_field_nt` to `end_field_nt`. Enforces low-to-high progression (`start < end`, `step > 0`, and current field $\le$ `start_field`), waits for `<1%` settling before counting each step's dwell timer, and holds the final field when the sweep finishes.
- **Safety Hard Limits**: Enforces `max_voltage_v` and `max_current_a` from `config.ini` across setpoint validation, PID output clamping, SCPI instrument drivers, and runtime over-current/over-voltage emergency cutoff (`0 V`, `OP 0`, `FAULT` state).
- **Virtual Testing Mode**: Includes a physics-accurate 330 F RC circuit simulator (`VirtualCircuitModel`, `VirtualTTiMX100TP`, `VirtualKeysight34461A`) so the full application and `pytest` suite run without physical hardware connected.
- **Touch-First Dear PyGui HMI**: Designed for 1280×800 / 1366×768 touchscreen panels without requiring a physical mouse or keyboard:
  - **Persistent Top Header Bar**: Displays live system status, settling state, key telemetry readouts, and an always-accessible giant **STOP / OUTPUT OFF (0V)** button.
  - **Non-Scrolling Tabbed Control Panel**: Organizes `[Set Field]`, `[Sweep]`, `[Telemetry]`, and `[Settings / PID]` into large touch tabs with tall OS-keyboard-ready input boxes, large `[-]`/`[+]` step buttons, presets, and color-coded ON/OFF toggle buttons.
  - **Tabbed Real-Time Graphs**: Switch between `[All 3 Stacked]` and full-height `[Field (nT)]`, `[Voltage (V)]`, or `[Current (A)]` plots with large touch toolbar controls.
- **CSV Data Logging**: Periodically logs all engineering units to a CSV file at the interval configured in `config.ini`.

---

## Project Structure

```text
MX100TP_CoilDriver/
├── AGENTS.md                        # Architectural & testing standards
├── README.md                        # Living project documentation
├── config.ini                       # Hardware, safety, PID, conversion, and logging config
├── main.py                          # Application CLI entry point
├── run.sh                           # Launcher script (activates .venv, bridges X11, runs main.py)
├── pyproject.toml                   # Package metadata & pytest configuration
├── External Documentation/
│   └── Circuit Diagram.jpg          # Electrical schematic (TTi MX100TP + 330F + DMM + Load)
├── src/
│   └── coil_driver/
│       ├── __init__.py
│       ├── config.py                # INI parser, validation, and A/nT conversion helpers
│       ├── physics.py               # 330 F RC + 1-quadrant PSU physics simulation
│       ├── instruments.py           # Ethernet TCP SCPI drivers & virtual mocks
│       ├── pid.py                   # Anti-overshoot PID + feedforward controller
│       ├── controller.py            # Set Field & Low-to-High Sweep state machine
│       ├── logger.py                # Interval-based CSV telemetry logger
│       ├── hmi.py                   # Headless touch-stepping & responsive plot layout helpers
│       └── gui.py                   # Touch-optimized Dear PyGui HMI & real-time plots
└── tests/
    ├── test_config.py               # Configuration & unit conversion tests
    ├── test_physics_and_instruments.py # 330F physics & TCP socket SCPI driver tests
    ├── test_pid_and_controller.py   # PID zero-overshoot, <1% accuracy, sweep & safety tests
    ├── test_logger.py               # CSV logging interval and schema tests
    └── test_hmi.py                  # Touch HMI step math, toggle labels & plot sizing tests
```

---

## Setup & Installation

1. **Create and activate a virtual environment** (Python 3.10+ required):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies**:
   ```bash
   pip install dearpygui pytest
   ```

---

## Configuration (`config.ini`)

All instrument IP addresses, ports, safety hard limits, `A/nT` conversion rate, PID gains, circuit parameters, and CSV logging intervals are defined in `config.ini`:

- `[instruments]`: `virtual_mode` (`true`/`false`), `psu_ip`, `psu_port` (default `9221`), `psu_channel` (`1`–`3`), `dmm_ip`, `dmm_port` (default `5025`), `socket_timeout_s`.
- `[safety]`: `max_voltage_v` and `max_current_a` hard limits.
- `[conversion]`: `a_per_nt` conversion rate ($\text{Current [A]} = \text{Field [nT]} \times \text{a\_per\_nt}$).
- `[pid]`: `kp`, `ki`, `kd`, `holding_tolerance_ratio` (`0.01` = 1%), `control_interval_s`, `max_overdrive_v`.
- `[circuit_model]`: `capacitance_f` (`330.0`), `load_resistance_ohm`, `bleeder_resistance_ohm`, `source_resistance_ohm`.
- `[logging]`: `enabled`, `log_interval_s`, `log_file_path`.

---

## Running the Application

Use the included `run.sh` launcher script, which automatically creates/activates `.venv`, bridges X11/XWayland display access if running inside a Flatpak IDE terminal, and launches `main.py`:

```bash
./run.sh

# Or pass CLI flags directly through run.sh:
./run.sh --config config.ini --virtual
```

You can also invoke `main.py` directly once `.venv` is active and `DISPLAY` is set:

```bash
# Run with settings from config.ini (defaults to Virtual Testing Mode)
.venv/bin/python main.py --config config.ini

# Explicitly force Virtual Testing Mode from the command line
.venv/bin/python main.py --config config.ini --virtual
```

---

## Running the Test Suite

All core logic, physics modeling, SCPI drivers, PID settling, sweep constraints, safety cutoffs, and CSV logging are tested in `tests/` using `pytest`:

```bash
.venv/bin/pytest -v
```
