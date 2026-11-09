#!/usr/bin/env python3
"""Entry point for the TTi MX100TP & Keysight 34461A Solenoid Magnetic Field Controller."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure src/ is importable when running `python main.py` directly from project root
_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from coil_driver.gui import launch_gui  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the coil driver application."""
    parser = argparse.ArgumentParser(
        description="Solenoid Magnetic Field Control Application (TTi MX100TP & Keysight 34461A)"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.ini",
        help="Path to the INI configuration file (default: config.ini).",
    )
    parser.add_argument(
        "--virtual",
        action="store_true",
        help="Force Virtual Testing Mode (330 F RC physics simulator) regardless of config file.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Launch the Dear PyGui solenoid controller interface."""
    args = parse_args(argv)
    launch_gui(config_path=args.config, force_virtual=args.virtual)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
