"""Anti-overshoot PID controller designed for the 330 F capacitor + solenoid coil system."""

from __future__ import annotations

from .config import CircuitModelConfig, PIDConfig, SafetyConfig


class AntiOvershootPID:
    """PID controller with feedforward, derivative-on-measurement braking, and anti-windup.

    Because the 330 F supercapacitor is driven by a 1-quadrant power supply (TTi MX100TP),
    any voltage overshoot on the capacitor cannot be actively sunk by the supply and must
    passively decay through the bleeder and load resistors. This controller prevents
    overshoot by:
      1. Computing baseline equilibrium feedforward voltage from the circuit model.
      2. Applying derivative-on-measurement (-Kd * dI/dt) to brake as current rises.
      3. Using trajectory-compensated conditional integration (integrating only the deficit
         between error and natural RC convergence rate tau_cv * dI/dt when not current-saturated),
         preventing ramp windup while still eliminating steady-state model mismatch.
      4. Enforcing hard safety clamping to [0.0, max_voltage_v].
    """

    def __init__(
        self,
        pid_config: PIDConfig | None = None,
        safety_config: SafetyConfig | None = None,
        circuit_config: CircuitModelConfig | None = None,
    ) -> None:
        self.pid_config = pid_config or PIDConfig()
        self.safety_config = safety_config or SafetyConfig()
        self.circuit_config = circuit_config or CircuitModelConfig()

        self.integral_v: float = 0.0
        self._prev_measurement_a: float | None = None
        self._filtered_derivative_a_per_s: float = 0.0
        self._last_target_a: float = 0.0

    def reset(self) -> None:
        """Reset internal PID state (integral accumulator and derivative history)."""
        self.integral_v = 0.0
        self._prev_measurement_a = None
        self._filtered_derivative_a_per_s = 0.0
        self._last_target_a = 0.0

    def compute(
        self,
        target_current_a: float,
        measured_current_a: float,
        dt_s: float,
    ) -> float:
        """Compute the next PSU voltage command (V) for the given target and measured coil current.

        Args:
            target_current_a: Desired coil current in Amperes.
            measured_current_a: Present coil current in Amperes (from Keysight 34461A).
            dt_s: Time elapsed since previous control step in seconds.

        Returns:
            Commanded power supply voltage in Volts, strictly clamped to [0.0, max_voltage_v].
        """
        if dt_s <= 0:
            raise ValueError(f"dt_s must be > 0, got {dt_s}.")

        if target_current_a <= 0.0:
            self.reset()
            return 0.0

        error_a = target_current_a - measured_current_a

        # If target changed, adjust integral state to prevent step-change overshoot
        if abs(target_current_a - self._last_target_a) > 1e-6:
            if target_current_a < self._last_target_a:
                self.integral_v = min(0.0, self.integral_v)
            else:
                self.integral_v = 0.0
            self._last_target_a = target_current_a

        # 1. Derivative on measurement (with low-pass smoothing to reject DMM noise)
        if self._prev_measurement_a is None:
            raw_deriv = 0.0
        else:
            raw_deriv = (measured_current_a - self._prev_measurement_a) / dt_s
        self._prev_measurement_a = measured_current_a

        alpha = 0.4
        self._filtered_derivative_a_per_s = (
            alpha * raw_deriv + (1.0 - alpha) * self._filtered_derivative_a_per_s
        )

        # 2. Equilibrium feedforward voltage for the target current
        v_ff = self.circuit_config.steady_state_psu_voltage_for_coil_current(target_current_a)

        # Estimate current capacitor voltage and voltage ceiling before PSU current limit saturation
        v_cap_est = measured_current_a * self.circuit_config.load_resistance_ohm
        v_cc_ceiling = (
            v_cap_est + self.safety_config.max_current_a * self.circuit_config.source_resistance_ohm
        )

        # If measured current is above target by more than 0.2% of target, cut drive
        # so the 330 F capacitor can passively bleed down toward the target.
        if error_a < -0.002 * max(target_current_a, 1e-6):
            self.integral_v = max(
                -self.pid_config.max_overdrive_v,
                self.integral_v + 2.0 * self.pid_config.ki * error_a * dt_s,
            )
            return 0.0

        # 3. Proportional and derivative braking terms
        p_term = self.pid_config.kp * error_a
        rising_rate = max(0.0, self._filtered_derivative_a_per_s)
        d_term = -self.pid_config.kd * rising_rate

        dynamic_overdrive = p_term + d_term
        if error_a >= 0:
            dynamic_overdrive = max(0.0, dynamic_overdrive)

        # 4. Trajectory-compensated anti-windup integration:
        # CV time constant tau_cv = (R_source || R_parallel) * C
        r_par = self.circuit_config.parallel_resistance_ohm
        r_src = self.circuit_config.source_resistance_ohm
        r_th = (r_src * r_par) / (r_src + r_par)
        tau_cv = max(1.0, r_th * self.circuit_config.capacitance_f)

        rel_error = abs(error_a) / max(target_current_a, 1e-6)
        is_current_saturated = (v_ff + dynamic_overdrive + self.integral_v) > v_cc_ceiling
        in_integral_band = (rel_error < 0.10) and not is_current_saturated

        if error_a < 0:
            self.integral_v += 2.0 * self.pid_config.ki * error_a * dt_s
        elif in_integral_band:
            # Only integrate the steady-state deficit (error_a - tau_cv * dI/dt) so that
            # natural capacitor charging never winds up positive integral excess.
            ss_deficit_a = max(0.0, error_a - tau_cv * rising_rate)
            self.integral_v += self.pid_config.ki * ss_deficit_a * dt_s

        max_int = max(2.0, self.pid_config.max_overdrive_v)
        self.integral_v = max(-max_int, min(max_int, self.integral_v))

        raw_voltage = v_ff + dynamic_overdrive + self.integral_v

        # Strictly enforce safety hard limits [0.0, max_voltage_v]
        clamped_voltage = max(0.0, min(raw_voltage, self.safety_config.max_voltage_v))
        return clamped_voltage
