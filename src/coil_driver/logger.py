"""CSV data logger for recording solenoid magnetic field, current, and voltage telemetry."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TelemetrySample:
    """Single point-in-time snapshot of all system parameters in engineering units."""

    timestamp_iso: str
    elapsed_s: float
    mode: str
    target_field_nt: float
    measured_field_nt: float
    holding_error_pct: float
    within_tolerance: bool
    target_current_a: float
    coil_current_a: float
    psu_voltage_set_v: float
    psu_voltage_meas_v: float
    psu_current_meas_a: float
    sweep_step_index: int = 0
    sweep_total_steps: int = 0
    sweep_dwell_elapsed_s: float = 0.0
    fault_message: str = ""


CSV_COLUMNS = [
    "timestamp_iso",
    "elapsed_s",
    "mode",
    "target_field_nt",
    "measured_field_nt",
    "holding_error_pct",
    "within_tolerance",
    "target_current_a",
    "coil_current_a",
    "psu_voltage_set_v",
    "psu_voltage_meas_v",
    "psu_current_meas_a",
    "sweep_step_index",
    "sweep_total_steps",
    "sweep_dwell_elapsed_s",
    "fault_message",
]


class CSVDataLogger:
    """Periodically writes TelemetrySample rows to a CSV file at a configured time interval."""

    def __init__(
        self,
        file_path: str | Path,
        log_interval_s: float = 1.0,
        enabled: bool = True,
    ) -> None:
        if log_interval_s <= 0:
            raise ValueError(f"log_interval_s must be > 0, got {log_interval_s}.")
        self.file_path = Path(file_path)
        self.log_interval_s = float(log_interval_s)
        self.enabled = bool(enabled)
        self._last_logged_elapsed_s: float | None = None
        self.rows_written: int = 0

    def reset_timer(self) -> None:
        """Reset interval tracking so the next sample is logged immediately."""
        self._last_logged_elapsed_s = None

    def set_file_path(self, new_path: str | Path) -> None:
        """Update the destination CSV file path and reset timer."""
        self.file_path = Path(new_path)
        self._last_logged_elapsed_s = None
        self.rows_written = 0

    def maybe_log(self, sample: TelemetrySample, force: bool = False) -> bool:
        """Write a CSV row if logging is enabled and log_interval_s has elapsed.

        Returns True if a row was written, False otherwise.
        """
        if not self.enabled and not force:
            return False

        if (
            not force
            and self._last_logged_elapsed_s is not None
            and (sample.elapsed_s - self._last_logged_elapsed_s) < (self.log_interval_s - 1e-9)
        ):
            return False

        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.file_path.exists() or self.file_path.stat().st_size == 0

        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp)
            if write_header:
                writer.writerow(CSV_COLUMNS)
            writer.writerow(
                [
                    sample.timestamp_iso,
                    f"{sample.elapsed_s:.3f}",
                    sample.mode,
                    f"{sample.target_field_nt:.4f}",
                    f"{sample.measured_field_nt:.4f}",
                    f"{sample.holding_error_pct:.4f}",
                    str(sample.within_tolerance),
                    f"{sample.target_current_a:.6f}",
                    f"{sample.coil_current_a:.6f}",
                    f"{sample.psu_voltage_set_v:.4f}",
                    f"{sample.psu_voltage_meas_v:.4f}",
                    f"{sample.psu_current_meas_a:.6f}",
                    sample.sweep_step_index,
                    sample.sweep_total_steps,
                    f"{sample.sweep_dwell_elapsed_s:.3f}",
                    sample.fault_message,
                ]
            )

        self._last_logged_elapsed_s = sample.elapsed_s
        self.rows_written += 1
        return True
