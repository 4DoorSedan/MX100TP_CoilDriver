"""Unit tests for periodic CSV data logging in all engineering units."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from coil_driver.logger import CSV_COLUMNS, CSVDataLogger, TelemetrySample


def _make_sample(elapsed_s: float, field_nt: float = 10000.0) -> TelemetrySample:
    return TelemetrySample(
        timestamp_iso="2026-11-08T20:00:00+00:00",
        elapsed_s=elapsed_s,
        mode="SET_FIELD",
        target_field_nt=field_nt,
        measured_field_nt=field_nt * 0.995,
        holding_error_pct=0.5,
        within_tolerance=True,
        target_current_a=1.0,
        coil_current_a=0.995,
        psu_voltage_set_v=10.55,
        psu_voltage_meas_v=10.50,
        psu_current_meas_a=1.0945,
        sweep_step_index=0,
        sweep_total_steps=0,
        sweep_dwell_elapsed_s=0.0,
        fault_message="",
    )


def test_csv_logger_interval_and_columns(tmp_path: Path) -> None:
    """Verify CSVDataLogger writes headers and respects log_interval_s."""
    csv_path = tmp_path / "logs" / "telemetry.csv"
    logger = CSVDataLogger(file_path=csv_path, log_interval_s=2.0, enabled=True)

    # t = 0.0 -> logged immediately (row 1)
    assert logger.maybe_log(_make_sample(0.0)) is True
    # t = 1.0 -> skipped (< 2.0 s elapsed)
    assert logger.maybe_log(_make_sample(1.0)) is False
    # t = 1.9 -> skipped (< 2.0 s elapsed)
    assert logger.maybe_log(_make_sample(1.9)) is False
    # t = 2.0 -> logged (row 2)
    assert logger.maybe_log(_make_sample(2.0)) is True
    # t = 4.5 -> logged (row 3)
    assert logger.maybe_log(_make_sample(4.5)) is True

    assert logger.rows_written == 3
    assert csv_path.exists()

    with csv_path.open("r", encoding="utf-8") as fp:
        rows = list(csv.reader(fp))

    assert len(rows) == 4  # 1 header + 3 data rows
    assert rows[0] == CSV_COLUMNS
    assert float(rows[1][1]) == pytest.approx(0.0)
    assert float(rows[2][1]) == pytest.approx(2.0)
    assert float(rows[3][1]) == pytest.approx(4.5)
    assert rows[1][2] == "SET_FIELD"
    assert float(rows[1][3]) == pytest.approx(10000.0)
    assert float(rows[1][8]) == pytest.approx(0.995)


def test_csv_logger_disabled_does_not_write(tmp_path: Path) -> None:
    """Verify disabled CSVDataLogger does not create files unless forced."""
    csv_path = tmp_path / "disabled.csv"
    logger = CSVDataLogger(file_path=csv_path, log_interval_s=1.0, enabled=False)

    assert logger.maybe_log(_make_sample(0.0)) is False
    assert not csv_path.exists()

    # Force write works even when disabled
    assert logger.maybe_log(_make_sample(1.0), force=True) is True
    assert csv_path.exists()
