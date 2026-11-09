"""Physics simulation of the TTi MX100TP + 330F Capacitor + Bleeder + Solenoid Load circuit."""

from __future__ import annotations

from .config import CircuitModelConfig


class VirtualCircuitModel:
    """Simulates the electrical dynamics of the solenoid driver circuit.

    Circuit topology (from External Documentation/Circuit Diagram.jpg):
      - TTi MX100TP DC Power Supply (1-quadrant source: can source current >= 0 up to I_limit,
        cannot sink current from the capacitor when V_set < V_cap).
      - Source/lead resistance R_source between PSU internal source and the capacitor bus.
      - Parallel bank across the capacitor bus:
          1. Bleeder resistor (R_bleed)
          2. 330 F supercapacitor (C)
          3. Keysight 34461A ammeter in series with Load resistor / solenoid coil (R_load)
    """

    def __init__(
        self,
        config: CircuitModelConfig | None = None,
        initial_voltage_v: float = 0.0,
    ) -> None:
        self.config = config or CircuitModelConfig()
        self.config.validate()

        self.capacitor_voltage_v: float = max(0.0, float(initial_voltage_v))
        self.psu_set_voltage_v: float = 0.0
        self.psu_current_limit_a: float = 3.0
        self.psu_output_enabled: bool = False
        self.elapsed_time_s: float = 0.0

    def reset(self, initial_voltage_v: float = 0.0) -> None:
        """Reset the simulated circuit to a known initial capacitor voltage."""
        self.capacitor_voltage_v = max(0.0, float(initial_voltage_v))
        self.psu_set_voltage_v = 0.0
        self.psu_output_enabled = False
        self.elapsed_time_s = 0.0

    def _compute_psu_current(self, v_cap: float) -> float:
        """Compute instantaneous current sourced by the 1-quadrant power supply."""
        if not self.psu_output_enabled or self.psu_current_limit_a <= 0.0:
            return 0.0
        if self.psu_set_voltage_v <= v_cap:
            # 1-quadrant power supply cannot sink reverse current from the capacitor
            return 0.0
        unclamped_current = (self.psu_set_voltage_v - v_cap) / self.config.source_resistance_ohm
        return min(self.psu_current_limit_a, max(0.0, unclamped_current))

    def _dv_dt(self, v_cap: float) -> float:
        """Compute dV_cap/dt = (I_psu - I_bleed - I_load) / C."""
        i_psu = self._compute_psu_current(v_cap)
        i_bleed = v_cap / self.config.bleeder_resistance_ohm
        i_load = v_cap / self.config.load_resistance_ohm
        return (i_psu - i_bleed - i_load) / self.config.capacitance_f

    def step(self, dt_s: float) -> None:
        """Advance the circuit simulation by dt_s seconds using sub-stepped RK4 integration."""
        if dt_s < 0:
            raise ValueError(f"Time step dt_s must be non-negative, got {dt_s}.")
        if dt_s == 0:
            return

        # Characteristic fast time constant during charging is R_source * C (~165 s for 0.5 Ohm * 330 F).
        # Using a max internal sub-step of 0.05 s guarantees high numerical stability and accuracy.
        max_substep = 0.05
        n_steps = max(1, int(dt_s / max_substep) + 1)
        h = dt_s / n_steps

        v = self.capacitor_voltage_v
        for _ in range(n_steps):
            k1 = self._dv_dt(v)
            k2 = self._dv_dt(max(0.0, v + 0.5 * h * k1))
            k3 = self._dv_dt(max(0.0, v + 0.5 * h * k2))
            k4 = self._dv_dt(max(0.0, v + h * k3))
            v = max(0.0, v + (h / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4))

        self.capacitor_voltage_v = v
        self.elapsed_time_s += dt_s

    @property
    def coil_current_a(self) -> float:
        """Current (A) flowing through the multimeter and solenoid load resistor."""
        return self.capacitor_voltage_v / self.config.load_resistance_ohm

    @property
    def bleeder_current_a(self) -> float:
        """Current (A) flowing through the parallel bleeder resistor."""
        return self.capacitor_voltage_v / self.config.bleeder_resistance_ohm

    @property
    def psu_measured_current_a(self) -> float:
        """Current (A) sourced by the TTi MX100TP output terminal."""
        return self._compute_psu_current(self.capacitor_voltage_v)

    @property
    def psu_measured_voltage_v(self) -> float:
        """Terminal voltage (V) measured at the TTi MX100TP output."""
        if not self.psu_output_enabled:
            return self.capacitor_voltage_v
        i_psu = self.psu_measured_current_a
        return self.capacitor_voltage_v + i_psu * self.config.source_resistance_ohm
