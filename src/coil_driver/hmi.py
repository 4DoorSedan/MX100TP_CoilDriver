"""Headless HMI state, touch-stepping math, and layout calculation helpers."""

from __future__ import annotations


def step_numeric_value(
    current: float,
    delta: float,
    min_value: float | None = 0.0,
    max_value: float | None = None,
    decimals: int = 4,
) -> float:
    """Increment or decrement a numeric value by delta and clamp within [min_value, max_value].

    Used by the HMI touch [+] / [-] step buttons so numeric inputs can be adjusted
    accurately with a finger without requiring a physical mouse or keyboard.
    """
    updated = round(float(current) + float(delta), decimals)
    if min_value is not None and updated < min_value:
        updated = float(min_value)
    if max_value is not None and updated > max_value:
        updated = float(max_value)
    return round(updated, decimals)


def format_toggle_label(label: str, state: bool, on_text: str = "ON", off_text: str = "OFF") -> str:
    """Format a clear status label for large touch toggle buttons."""
    suffix = on_text if state else off_text
    return f"{label}: {suffix}"


def compute_stacked_plot_height(
    viewport_height: int,
    reserved_vertical_px: int = 210,
    num_plots: int = 3,
    min_plot_height: int = 150,
) -> int:
    """Compute the pixel height for each stacked plot so all plots fit without scrolling."""
    if num_plots <= 0:
        raise ValueError(f"num_plots must be >= 1, got {num_plots}.")
    available = max(0, int(viewport_height) - int(reserved_vertical_px))
    per_plot = available // num_plots
    return max(min_plot_height, per_plot)


def compute_single_plot_height(
    viewport_height: int,
    reserved_vertical_px: int = 190,
    min_plot_height: int = 400,
) -> int:
    """Compute the pixel height for a single expanded full-height tab plot."""
    available = max(0, int(viewport_height) - int(reserved_vertical_px))
    return max(min_plot_height, available)
