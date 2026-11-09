"""Touch-friendly Dear PyGui HMI for the TTi MX100TP & Keysight 34461A Solenoid Controller."""

from __future__ import annotations

import os
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import dearpygui.dearpygui as dpg

from .config import AppConfig, load_config, save_config
from .controller import OperationMode, SweepParameters, SystemController
from .hmi import (
    compute_single_plot_height,
    compute_stacked_plot_height,
    format_toggle_label,
    step_numeric_value,
)


def _start_flatpak_x11_bridge_if_needed() -> None:
    """Ensure DISPLAY and /tmp/.X11-unix socket exist when running inside a Flatpak on Wayland."""
    if sys.platform != "linux":
        return

    display = os.environ.get("DISPLAY", "")
    if display.startswith(":"):
        disp_num = display[1:].split(".")[0]
        if Path(f"/tmp/.X11-unix/X{disp_num}").is_socket():
            return

    if not Path("/.flatpak-info").exists() or shutil.which("flatpak-spawn") is None:
        if not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "No X11 DISPLAY environment variable found. "
                "Please run inside a graphical desktop session or use ./run.sh."
            )
        return

    try:
        host_disp = subprocess.check_output(
            ["flatpak-spawn", "--host", "printenv", "DISPLAY"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        host_disp = ":0"

    disp_num = host_disp.lstrip(":").split(".")[0] or "0"

    try:
        host_xauth = subprocess.check_output(
            ["flatpak-spawn", "--host", "printenv", "XAUTHORITY"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if host_xauth:
            xauth_bytes = subprocess.check_output(
                ["flatpak-spawn", "--host", "cat", host_xauth],
                stderr=subprocess.DEVNULL,
            )
            local_xauth = Path("/tmp/.Xauthority")
            local_xauth.write_bytes(xauth_bytes)
            os.environ["XAUTHORITY"] = str(local_xauth)
    except Exception:
        pass

    x11_dir = Path("/tmp/.X11-unix")
    x11_dir.mkdir(parents=True, exist_ok=True)
    local_sock_path = f"/tmp/.X11-unix/X{disp_num}"
    host_sock_path = f"/tmp/.X11-unix/X{disp_num}"

    if not Path(local_sock_path).is_socket():
        try:
            os.unlink(local_sock_path)
        except FileNotFoundError:
            pass

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(local_sock_path)
        server.listen(16)

        def _bridge_client(client_sock: socket.socket) -> None:
            proc = subprocess.Popen(
                [
                    "flatpak-spawn",
                    "--host",
                    "python3",
                    "-c",
                    (
                        "import os, select, socket; "
                        f"s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect({host_sock_path!r}); "
                        "os.set_blocking(0, False); s.setblocking(False);\n"
                        "while True:\n"
                        "  r, _, _ = select.select([0, s], [], [])\n"
                        "  if 0 in r:\n"
                        "    d = os.read(0, 65536)\n"
                        "    if not d: break\n"
                        "    s.sendall(d)\n"
                        "  if s in r:\n"
                        "    d = s.recv(65536)\n"
                        "    if not d: break\n"
                        "    os.write(1, d)\n"
                    ),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                bufsize=0,
            )
            assert proc.stdin is not None and proc.stdout is not None
            p_out_fd = proc.stdout.fileno()
            p_in_fd = proc.stdin.fileno()
            os.set_blocking(p_out_fd, False)
            client_sock.setblocking(False)
            try:
                while True:
                    r, _, _ = select.select([client_sock, p_out_fd], [], [])
                    if client_sock in r:
                        data = client_sock.recv(65536)
                        if not data:
                            break
                        os.write(p_in_fd, data)
                    if p_out_fd in r:
                        data = os.read(p_out_fd, 65536)
                        if not data:
                            break
                        client_sock.sendall(data)
            except Exception:
                pass
            finally:
                client_sock.close()
                proc.terminate()

        def _accept_loop() -> None:
            while True:
                try:
                    conn, _ = server.accept()
                    threading.Thread(target=_bridge_client, args=(conn,), daemon=True).start()
                except Exception:
                    break

        threading.Thread(target=_accept_loop, daemon=True).start()

    os.environ["DISPLAY"] = f":{disp_num}"


class CoilDriverGUI:
    """Touch-optimized Dear PyGui HMI front-end decoupled from SystemController."""

    STACKED_SERIES_TAGS = (
        "series_field_meas",
        "series_field_targ",
        "series_volt_set",
        "series_volt_meas",
        "series_curr_coil",
        "series_curr_targ",
        "series_curr_psu",
    )

    SINGLE_SERIES_TAGS = (
        "series_field_meas_single",
        "series_field_targ_single",
        "series_volt_set_single",
        "series_volt_meas_single",
        "series_curr_coil_single",
        "series_curr_targ_single",
        "series_curr_psu_single",
    )

    ALL_PLOT_AXES = (
        "xaxis_field",
        "yaxis_field",
        "xaxis_volt",
        "yaxis_volt",
        "xaxis_curr",
        "yaxis_curr",
        "xaxis_field_single",
        "yaxis_field_single",
        "xaxis_volt_single",
        "yaxis_volt_single",
        "xaxis_curr_single",
        "yaxis_curr_single",
    )

    def __init__(self, config: AppConfig, config_path: str | Path = "config.ini") -> None:
        self.config = config
        self.config_path = Path(config_path)
        self.controller = SystemController(config)
        self.controller.connect_instruments()

        self._last_tick_s: float = time.monotonic()
        self.autofit_plots: bool = True
        self._dpg_context_ready: bool = False
        self._status_banner_msg: str = (
            "Ready (Connected in Virtual Mode)"
            if config.instruments.virtual_mode
            else "Connected to SCPI Hardware"
        )

    def _item_exists(self, tag: str) -> bool:
        """Safely check if a Dear PyGui item exists only when the DPG context is active."""
        return self._dpg_context_ready and bool(dpg.does_item_exist(tag))

    def _rebuild_controller(self, virtual_mode: bool) -> None:
        """Switch between Virtual Testing Mode and Real SCPI Hardware mode."""
        try:
            self.controller.disconnect_instruments()
        except Exception:
            pass

        self.config.instruments.virtual_mode = virtual_mode
        self.controller = SystemController(self.config)
        try:
            self.controller.connect_instruments()
            mode_label = "Virtual 330F Simulator" if virtual_mode else "Real SCPI Hardware"
            self._set_banner(f"Connected ({mode_label})", is_error=False)
        except Exception as exc:
            self._set_banner(f"Connection failed: {exc}", is_error=True)

    def _set_banner(self, message: str, is_error: bool = False) -> None:
        self._status_banner_msg = message
        if self._item_exists("status_banner_text"):
            dpg.set_value("status_banner_text", message)
            color = (255, 95, 95, 255) if is_error else (130, 225, 150, 255)
            dpg.configure_item("status_banner_text", color=color)

    def _apply_toggle_button_style(self, button_tag: str, label_prefix: str, state: bool) -> None:
        """Update label and ON/OFF color theme on a touch toggle button."""
        if not self._item_exists(button_tag):
            return
        dpg.configure_item(button_tag, label=format_toggle_label(label_prefix, state))
        theme_tag = "theme_toggle_on" if state else "theme_toggle_off"
        if self._item_exists(theme_tag):
            dpg.bind_item_theme(button_tag, theme_tag)

    def _step_input_field(
        self,
        input_tag: str,
        delta: float,
        min_value: float | None = 0.0,
        max_value: float | None = None,
        decimals: int = 4,
        after_change: Callable[[], None] | None = None,
    ) -> None:
        """Adjust a Dear PyGui float input by delta when a touch [-]/[+] button is tapped."""
        if not self._item_exists(input_tag):
            return
        current = float(dpg.get_value(input_tag))
        updated = step_numeric_value(
            current=current,
            delta=delta,
            min_value=min_value,
            max_value=max_value,
            decimals=decimals,
        )
        dpg.set_value(input_tag, updated)
        if after_change is not None:
            after_change()

    def _set_input_field(self, input_tag: str, value: float) -> None:
        """Directly set a numeric input box value from a preset touch button."""
        if self._item_exists(input_tag):
            dpg.set_value(input_tag, float(value))

    def _make_step_callback(
        self,
        input_tag: str,
        delta: float,
        min_value: float | None = 0.0,
        max_value: float | None = None,
        decimals: int = 4,
        after_change: Callable[[], None] | None = None,
    ) -> Callable[[], None]:
        """Create a 0-arg closure callback for Dear PyGui step buttons.

        Dear PyGui inspects callback.__code__.co_argcount: functions with 3 args
        receive (sender, app_data, user_data=None) and functions with >3 args are
        not called at all. Returning a 0-arg closure avoids parameter overwriting.
        """

        def _cb() -> None:
            self._step_input_field(
                input_tag=input_tag,
                delta=delta,
                min_value=min_value,
                max_value=max_value,
                decimals=decimals,
                after_change=after_change,
            )

        return _cb

    def _make_preset_callback(self, input_tag: str, value: float) -> Callable[[], None]:
        """Create a 0-arg closure callback for Dear PyGui preset buttons."""

        def _cb() -> None:
            self._set_input_field(input_tag, value)

        return _cb

    # ------------------------------------------------------------------
    # UI Callbacks
    # ------------------------------------------------------------------

    def _on_toggle_virtual_mode_btn(self) -> None:
        new_mode = not self.config.instruments.virtual_mode
        self._rebuild_controller(new_mode)
        self._apply_toggle_button_style("btn_toggle_virtual_mode", "Virtual 330F Mode", new_mode)

    def _on_toggle_logging_btn(self) -> None:
        new_enabled = not self.config.logging.enabled
        self.config.logging.enabled = new_enabled
        self._apply_toggle_button_style("btn_toggle_logging", "CSV Logging", new_enabled)
        self._on_logging_changed()

    def _on_toggle_autofit_btn(self) -> None:
        self.autofit_plots = not self.autofit_plots
        self._apply_toggle_button_style("btn_toggle_autofit", "Auto-Fit Axes", self.autofit_plots)

    def _on_reset_virtual_capacitor(self) -> None:
        if self.controller.virtual_circuit is not None:
            self.controller.stop()
            self.controller.virtual_circuit.reset(0.0)
            self.controller.history.clear()
            self._set_banner("Virtual 330F capacitor discharged to 0.0 V.", is_error=False)
        else:
            self._set_banner(
                "Capacitor instant-reset is only available in Virtual Mode.",
                is_error=True,
            )

    def _on_start_set_field(self) -> None:
        target_nt = float(dpg.get_value("input_set_field_nt"))
        try:
            if not self.controller.connected:
                self.controller.connect_instruments()
            self.controller.start_set_field_mode(target_nt)
            req_curr = self.config.field_nt_to_current_a(target_nt)
            self._set_banner(
                f"Set Field Mode active: target = {target_nt:.1f} nT ({req_curr:.4f} A)",
                is_error=False,
            )
        except Exception as exc:
            self._set_banner(f"Set Field Error: {exc}", is_error=True)

    def _on_start_sweep(self) -> None:
        start_nt = float(dpg.get_value("input_sweep_start_nt"))
        end_nt = float(dpg.get_value("input_sweep_end_nt"))
        step_nt = float(dpg.get_value("input_sweep_step_nt"))
        dwell_s = float(dpg.get_value("input_sweep_dwell_s"))
        params = SweepParameters(
            start_field_nt=start_nt,
            end_field_nt=end_nt,
            step_field_nt=step_nt,
            dwell_time_s=dwell_s,
        )
        try:
            if not self.controller.connected:
                self.controller.connect_instruments()
            self.controller.start_sweep_mode(params)
            n_steps = len(self.controller.sweep_steps_nt)
            self._set_banner(
                f"Low-to-High Sweep active: {start_nt:.1f} -> {end_nt:.1f} nT ({n_steps} steps)",
                is_error=False,
            )
        except Exception as exc:
            self._set_banner(f"Sweep Error: {exc}", is_error=True)

    def _on_stop_output(self) -> None:
        try:
            self.controller.stop()
            self._set_banner("Output stopped (PSU set to 0.0 V, OP OFF).", is_error=False)
        except Exception as exc:
            self._set_banner(f"Stop Error: {exc}", is_error=True)

    def _on_clear_history(self) -> None:
        self.controller.history.clear()
        for tag in (*self.STACKED_SERIES_TAGS, *self.SINGLE_SERIES_TAGS):
            if self._item_exists(tag):
                dpg.set_value(tag, [[], []])
        self._set_banner("Plot history cleared.", is_error=False)

    def _on_logging_changed(self) -> None:
        enabled = self.config.logging.enabled
        interval_s = (
            max(0.1, float(dpg.get_value("input_log_interval_s")))
            if self._item_exists("input_log_interval_s")
            else self.config.logging.log_interval_s
        )
        file_path = (
            str(dpg.get_value("input_log_path")).strip()
            if self._item_exists("input_log_path")
            else self.config.logging.log_file_path
        ) or "logs/coil_driver_log.csv"

        self.config.logging.log_interval_s = interval_s
        self.config.logging.log_file_path = file_path

        self.controller.logger.enabled = enabled
        self.controller.logger.log_interval_s = interval_s
        if str(self.controller.logger.file_path) != file_path:
            self.controller.logger.set_file_path(file_path)

        self._set_banner(
            f"Logging {'enabled' if enabled else 'disabled'} ({interval_s:.1f}s -> {file_path})",
            is_error=False,
        )

    def _on_save_config(self) -> None:
        try:
            self._on_pid_gains_changed()
            save_config(self.config, self.config_path)
            self._set_banner(f"Configuration saved to {self.config_path}", is_error=False)
        except Exception as exc:
            self._set_banner(f"Save Config Error: {exc}", is_error=True)

    def _on_pid_gains_changed(self) -> None:
        if self._item_exists("input_pid_kp"):
            self.config.pid.kp = max(0.0, float(dpg.get_value("input_pid_kp")))
        if self._item_exists("input_pid_ki"):
            self.config.pid.ki = max(0.0, float(dpg.get_value("input_pid_ki")))
        if self._item_exists("input_pid_kd"):
            self.config.pid.kd = max(0.0, float(dpg.get_value("input_pid_kd")))

    def _on_viewport_resize(self) -> None:
        """Dynamically resize plots to fit the current HMI screen height without scrolling."""
        if not self._dpg_context_ready:
            return
        vp_height = dpg.get_viewport_client_height() or 800
        stacked_h = compute_stacked_plot_height(vp_height)
        single_h = compute_single_plot_height(vp_height)

        for plot_tag in ("plot_field_stacked", "plot_volt_stacked", "plot_curr_stacked"):
            if self._item_exists(plot_tag):
                dpg.configure_item(plot_tag, height=stacked_h)

        for plot_tag in ("plot_field_single", "plot_volt_single", "plot_curr_single"):
            if self._item_exists(plot_tag):
                dpg.configure_item(plot_tag, height=single_h)

    # ------------------------------------------------------------------
    # Periodic Update & Plot Refresh
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        now = time.monotonic()
        wall_dt = max(0.01, min(0.5, now - self._last_tick_s))
        self._last_tick_s = now

        sim_speed = (
            max(1.0, float(dpg.get_value("input_sim_speed")))
            if self._item_exists("input_sim_speed")
            else 1.0
        )
        dt_s = wall_dt * (sim_speed if self.config.instruments.virtual_mode else 1.0)

        if not self.controller.connected:
            return

        try:
            sample = self.controller.update(dt_s)
        except Exception as exc:
            self._set_banner(f"Control Loop Fault: {exc}", is_error=True)
            return

        if not self._dpg_context_ready:
            return

        # Update persistent top header & full telemetry tab fields
        dpg.set_value("txt_mode", sample.mode)
        dpg.set_value("txt_meas_field", f"{sample.measured_field_nt:,.2f} nT")
        dpg.set_value("txt_target_field", f"{sample.target_field_nt:,.2f} nT")
        dpg.set_value("txt_error_pct", f"{sample.holding_error_pct:.3f} %")

        dpg.set_value("txt_hdr_mode", sample.mode)
        dpg.set_value(
            "txt_hdr_field",
            f"{sample.measured_field_nt:,.1f} / {sample.target_field_nt:,.1f} nT",
        )
        dpg.set_value("txt_hdr_error", f"{sample.holding_error_pct:.2f} %")
        dpg.set_value("txt_hdr_curr", f"{sample.coil_current_a:.4f} A")
        dpg.set_value(
            "txt_hdr_volt",
            f"{sample.psu_voltage_meas_v:.2f} / {sample.psu_voltage_set_v:.2f} V",
        )

        if sample.mode in (OperationMode.SET_FIELD.value, OperationMode.SWEEP.value):
            if sample.within_tolerance:
                status_text = "SETTLED (<1% Holding Accuracy)"
                status_color = (80, 240, 120, 255)
            else:
                status_text = "ENCROACHING (Charging / Settling)"
                status_color = (255, 205, 80, 255)
        elif sample.mode == OperationMode.FAULT.value:
            status_text = f"FAULT: {sample.fault_message}"
            status_color = (255, 80, 80, 255)
        else:
            status_text = "IDLE (Output Disabled)"
            status_color = (180, 180, 180, 255)

        for tag in ("txt_settled_status", "txt_telemetry_settled_status"):
            if self._item_exists(tag):
                dpg.set_value(tag, status_text)
                dpg.configure_item(tag, color=status_color)

        dpg.set_value("txt_coil_current", f"{sample.coil_current_a:.5f} A")
        dpg.set_value("txt_target_current", f"{sample.target_current_a:.5f} A")
        dpg.set_value("txt_psu_v_set", f"{sample.psu_voltage_set_v:.3f} V")
        dpg.set_value("txt_psu_v_meas", f"{sample.psu_voltage_meas_v:.3f} V")
        dpg.set_value("txt_psu_i_meas", f"{sample.psu_current_meas_a:.4f} A")
        dpg.set_value("txt_log_rows", str(self.controller.logger.rows_written))

        if sample.sweep_total_steps > 0:
            dwell_target = max(1e-6, self.controller.sweep_dwell_time_s)
            progress = min(1.0, sample.sweep_dwell_elapsed_s / dwell_target)
            dpg.set_value("sweep_progress_bar", progress)
            dpg.configure_item(
                "sweep_progress_bar",
                overlay=(
                    f"Step {sample.sweep_step_index}/{sample.sweep_total_steps} | "
                    f"Dwell {sample.sweep_dwell_elapsed_s:.1f}/{self.controller.sweep_dwell_time_s:.1f}s"
                ),
            )
        else:
            dpg.set_value("sweep_progress_bar", 0.0)
            dpg.configure_item("sweep_progress_bar", overlay="No Active Sweep")

        # Update plot data series (both stacked view and single-graph tabs)
        history = self.controller.history
        if history:
            t_vals = [s.elapsed_s for s in history]
            b_meas = [s.measured_field_nt for s in history]
            b_targ = [s.target_field_nt for s in history]
            v_set = [s.psu_voltage_set_v for s in history]
            v_meas = [s.psu_voltage_meas_v for s in history]
            i_coil = [s.coil_current_a for s in history]
            i_targ = [s.target_current_a for s in history]
            i_psu = [s.psu_current_meas_a for s in history]

            for suffix in ("", "_single"):
                dpg.set_value(f"series_field_meas{suffix}", [t_vals, b_meas])
                dpg.set_value(f"series_field_targ{suffix}", [t_vals, b_targ])
                dpg.set_value(f"series_volt_set{suffix}", [t_vals, v_set])
                dpg.set_value(f"series_volt_meas{suffix}", [t_vals, v_meas])
                dpg.set_value(f"series_curr_coil{suffix}", [t_vals, i_coil])
                dpg.set_value(f"series_curr_targ{suffix}", [t_vals, i_targ])
                dpg.set_value(f"series_curr_psu{suffix}", [t_vals, i_psu])

            if self.autofit_plots:
                for axis_tag in self.ALL_PLOT_AXES:
                    if self._item_exists(axis_tag):
                        dpg.fit_axis_data(axis_tag)

    # ------------------------------------------------------------------
    # Touch Themes & Layout Construction
    # ------------------------------------------------------------------

    def _setup_touch_themes(self) -> None:
        """Create global touch-friendly padding/sizing and high-contrast button themes."""
        dpg.set_global_font_scale(1.22)

        with dpg.theme(tag="theme_global_touch"):
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 12, 10)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 10, 9)
                dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 10, 8)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 8)
                dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 6)
                dpg.add_theme_style(dpg.mvStyleVar_GrabMinSize, 26)
                dpg.add_theme_style(dpg.mvStyleVar_ScrollbarSize, 24)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)

        dpg.bind_theme("theme_global_touch")

        # Emergency STOP button theme (industrial high-contrast red)
        with dpg.theme(tag="theme_btn_stop"):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (195, 38, 38, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (225, 55, 55, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (150, 25, 25, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255, 255))

        # Primary Start / Hold button theme (industrial green)
        with dpg.theme(tag="theme_btn_start"):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (32, 138, 75, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (45, 168, 92, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (24, 110, 58, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255, 255))

        # Toggle ON theme (emerald green)
        with dpg.theme(tag="theme_toggle_on"):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (36, 148, 84, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (48, 175, 102, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (28, 120, 68, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255, 255))

        # Toggle OFF theme (muted slate gray)
        with dpg.theme(tag="theme_toggle_off"):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (72, 78, 92, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (92, 100, 116, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (58, 64, 76, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (220, 225, 235, 255))

        # Step [-]/[+] and preset button theme (tactile steel blue)
        with dpg.theme(tag="theme_btn_step"):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (52, 76, 112, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (68, 98, 142, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (40, 60, 90, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (240, 245, 255, 255))

    def _build_top_header_bar(self) -> None:
        """Build the persistent top HMI bar with status, live readouts, and giant Emergency STOP."""
        with dpg.child_window(height=98, border=True, no_scrollbar=True):
            with dpg.table(header_row=False, borders_innerV=False, borders_outerH=False):
                dpg.add_table_column(init_width_or_weight=1.0)
                dpg.add_table_column( width_fixed=True, init_width_or_weight=255)

                with dpg.table_row():
                    # Left side: Status line + key live telemetry strip
                    with dpg.group():
                        with dpg.group(horizontal=True):
                            dpg.add_text("STATUS:", color=(140, 200, 255))
                            dpg.add_text(
                                self._status_banner_msg,
                                tag="status_banner_text",
                                color=(130, 225, 150, 255),
                            )
                            dpg.add_text(" | ")
                            dpg.add_text(
                                "IDLE (Output Disabled)",
                                tag="txt_settled_status",
                                color=(180, 180, 180, 255),
                            )

                        with dpg.group(horizontal=True):
                            dpg.add_text("Mode:", color=(170, 185, 205))
                            dpg.add_text("IDLE", tag="txt_hdr_mode", color=(255, 255, 255))
                            dpg.add_text(" | Field:", color=(170, 185, 205))
                            dpg.add_text("0.0 / 0.0 nT", tag="txt_hdr_field", color=(120, 230, 255))
                            dpg.add_text(" | Err:", color=(170, 185, 205))
                            dpg.add_text("0.00 %", tag="txt_hdr_error", color=(255, 230, 130))
                            dpg.add_text(" | Coil I:", color=(170, 185, 205))
                            dpg.add_text("0.0000 A", tag="txt_hdr_curr", color=(255, 255, 255))
                            dpg.add_text(" | PSU V:", color=(170, 185, 205))
                            dpg.add_text("0.00 / 0.00 V", tag="txt_hdr_volt", color=(255, 255, 255))

                    # Right side: Giant Emergency STOP button always accessible
                    btn_stop_top = dpg.add_button(
                        label="STOP / OUTPUT OFF (0V)",
                        tag="btn_stop_top",
                        width=245,
                        height=72,
                        callback=lambda: self._on_stop_output(),
                    )
                    dpg.bind_item_theme(btn_stop_top, "theme_btn_stop")

    def _build_left_control_panel(self) -> None:
        """Build the 500px left control panel with 4 large touch tabs (zero scrolling)."""
        with dpg.child_window(width=500, border=True, no_scrollbar=True):
            with dpg.tab_bar(tag="control_tab_bar"):
                # ----------------------------------------------------------
                # TAB 1: Set Field Mode
                # ----------------------------------------------------------
                with dpg.tab(label="  Set Field  "):
                    dpg.add_spacer(height=4)
                    dpg.add_text(
                        "Set Constant Magnetic Field (<1% Accuracy)",
                        color=(140, 200, 255),
                    )
                    dpg.add_text(
                        "Tap input box for OS keyboard or use [-]/[+] step buttons:",
                        color=(185, 195, 210),
                    )
                    dpg.add_spacer(height=4)

                    dpg.add_text("Target Magnetic Field (nT):")
                    dpg.add_input_float(
                        label="",
                        tag="input_set_field_nt",
                        default_value=10000.0,
                        min_value=0.0,
                        min_clamped=True,
                        step=0.0,
                        format="%.2f",
                        width=-1,
                    )

                    dpg.add_text("Step Adjust Target (nT):", color=(185, 195, 210))
                    with dpg.group(horizontal=True):
                        for btn_tag, lbl, delta in [
                            ("btn_set_step_m1000", "-1000", -1000.0),
                            ("btn_set_step_m100", "-100", -100.0),
                            ("btn_set_step_p100", "+100", 100.0),
                            ("btn_set_step_p1000", "+1000", 1000.0),
                        ]:
                            b = dpg.add_button(
                                label=lbl,
                                tag=btn_tag,
                                width=110,
                                height=52,
                                callback=self._make_step_callback(
                                    "input_set_field_nt", delta, min_value=0.0, decimals=2
                                ),
                            )
                            dpg.bind_item_theme(b, "theme_btn_step")

                    dpg.add_text("Quick Presets (nT):", color=(185, 195, 210))
                    with dpg.group(horizontal=True):
                        for btn_tag, lbl, preset_val in [
                            ("btn_set_preset_0", "0 nT", 0.0),
                            ("btn_set_preset_20000", "20,000 nT", 20000.0),
                            ("btn_set_preset_45000", "45,000 nT", 45000.0),
                            ("btn_set_preset_70000", "70,000 nT", 70000.0),
                        ]:
                            b = dpg.add_button(
                                label=lbl,
                                tag=btn_tag,
                                width=110,
                                height=48,
                                callback=self._make_preset_callback(
                                    "input_set_field_nt", preset_val
                                ),
                            )
                            dpg.bind_item_theme(b, "theme_btn_step")

                    dpg.add_spacer(height=10)
                    btn_hold = dpg.add_button(
                        label="HOLD TARGET FIELD",
                        tag="btn_hold_target_field",
                        width=-1,
                        height=68,
                        callback=lambda: self._on_start_set_field(),
                    )
                    dpg.bind_item_theme(btn_hold, "theme_btn_start")

                    dpg.add_spacer(height=6)
                    btn_stop_tab1 = dpg.add_button(
                        label="STOP / OUTPUT OFF (0V)",
                        tag="btn_stop_tab1",
                        width=-1,
                        height=60,
                        callback=lambda: self._on_stop_output(),
                    )
                    dpg.bind_item_theme(btn_stop_tab1, "theme_btn_stop")

                # ----------------------------------------------------------
                # TAB 2: Sweep Mode (Strictly Low-to-High)
                # ----------------------------------------------------------
                with dpg.tab(label="  Sweep  "):
                    dpg.add_spacer(height=2)
                    dpg.add_text(
                        "Sweep Mode (Strictly Low-to-High)",
                        color=(140, 200, 255),
                    )

                    sweep_fields = [
                        (
                            "Start Field (nT)",
                            "input_sweep_start_nt",
                            5000.0,
                            0.0,
                            500.0,
                            "%.1f",
                            "-500",
                            "+500",
                            "btn_sweep_start_minus",
                            "btn_sweep_start_plus",
                        ),
                        (
                            "End Field (nT)",
                            "input_sweep_end_nt",
                            20000.0,
                            0.0,
                            500.0,
                            "%.1f",
                            "-500",
                            "+500",
                            "btn_sweep_end_minus",
                            "btn_sweep_end_plus",
                        ),
                        (
                            "Step Size (nT)",
                            "input_sweep_step_nt",
                            5000.0,
                            1.0,
                            500.0,
                            "%.1f",
                            "-500",
                            "+500",
                            "btn_sweep_step_minus",
                            "btn_sweep_step_plus",
                        ),
                        (
                            "Dwell Time (s)",
                            "input_sweep_dwell_s",
                            10.0,
                            0.0,
                            1.0,
                            "%.1f",
                            "-1 s",
                            "+1 s",
                            "btn_sweep_dwell_minus",
                            "btn_sweep_dwell_plus",
                        ),
                    ]

                    for (
                        label_text,
                        tag,
                        default_val,
                        min_val,
                        step_delta,
                        fmt,
                        minus_lbl,
                        plus_lbl,
                        minus_btn_tag,
                        plus_btn_tag,
                    ) in sweep_fields:
                        dpg.add_text(label_text)
                        with dpg.group(horizontal=True):
                            dpg.add_input_float(
                                label="",
                                tag=tag,
                                default_value=default_val,
                                min_value=min_val,
                                min_clamped=True,
                                step=0.0,
                                format=fmt,
                                width=275,
                            )
                            b_minus = dpg.add_button(
                                label=minus_lbl,
                                tag=minus_btn_tag,
                                width=90,
                                height=44,
                                callback=self._make_step_callback(
                                    tag, -step_delta, min_value=min_val, decimals=2
                                ),
                            )
                            b_plus = dpg.add_button(
                                label=plus_lbl,
                                tag=plus_btn_tag,
                                width=90,
                                height=44,
                                callback=self._make_step_callback(
                                    tag, step_delta, min_value=min_val, decimals=2
                                ),
                            )
                            dpg.bind_item_theme(b_minus, "theme_btn_step")
                            dpg.bind_item_theme(b_plus, "theme_btn_step")

                    dpg.add_spacer(height=4)
                    dpg.add_progress_bar(
                        tag="sweep_progress_bar",
                        default_value=0.0,
                        width=-1,
                        height=36,
                        overlay="No Active Sweep",
                    )
                    dpg.add_spacer(height=4)
                    btn_sweep = dpg.add_button(
                        label="START LOW-TO-HIGH SWEEP",
                        tag="btn_start_sweep",
                        width=-1,
                        height=62,
                        callback=lambda: self._on_start_sweep(),
                    )
                    dpg.bind_item_theme(btn_sweep, "theme_btn_start")

                # ----------------------------------------------------------
                # TAB 3: Live System Telemetry
                # ----------------------------------------------------------
                with dpg.tab(label="  Telemetry  "):
                    dpg.add_spacer(height=4)
                    dpg.add_text("Live System Telemetry", color=(140, 200, 255))
                    dpg.add_text(
                        "IDLE (Output Disabled)",
                        tag="txt_telemetry_settled_status",
                        color=(180, 180, 180, 255),
                    )
                    dpg.add_spacer(height=4)

                    with dpg.table(
                        header_row=False,
                        borders_innerH=True,
                        borders_outerH=True,
                        borders_innerV=True,
                        borders_outerV=True,
                    ):
                        dpg.add_table_column(init_width_or_weight=0.50)
                        dpg.add_table_column(init_width_or_weight=0.50)

                        for label, tag, init_val in [
                            ("Active Mode:", "txt_mode", "IDLE"),
                            ("Measured Field:", "txt_meas_field", "0.00 nT"),
                            ("Target Field:", "txt_target_field", "0.00 nT"),
                            ("Holding Error:", "txt_error_pct", "0.000 %"),
                            ("Coil Current (DMM):", "txt_coil_current", "0.00000 A"),
                            ("Target Current:", "txt_target_current", "0.00000 A"),
                            ("PSU Set Voltage:", "txt_psu_v_set", "0.000 V"),
                            ("PSU Terminal Voltage:", "txt_psu_v_meas", "0.000 V"),
                            ("PSU Output Current:", "txt_psu_i_meas", "0.0000 A"),
                        ]:
                            with dpg.table_row():
                                dpg.add_text(label)
                                dpg.add_text(init_val, tag=tag)

                    dpg.add_spacer(height=8)
                    dpg.add_separator()
                    dpg.add_text("Hardware & Safety Limits", color=(140, 200, 255))
                    dpg.add_text(
                        f"PSU: {self.config.instruments.psu_ip}:{self.config.instruments.psu_port} "
                        f"(Ch {self.config.instruments.psu_channel})"
                    )
                    dpg.add_text(
                        f"DMM: {self.config.instruments.dmm_ip}:{self.config.instruments.dmm_port}"
                    )
                    dpg.add_text(
                        f"Safety: V_max = {self.config.safety.max_voltage_v:.1f} V | "
                        f"I_max = {self.config.safety.max_current_a:.2f} A"
                    )
                    dpg.add_text(
                        f"Conversion: {self.config.conversion.a_per_nt:g} A/nT"
                    )

                # ----------------------------------------------------------
                # TAB 4: Settings, Simulator & Anti-Overshoot PID
                # ----------------------------------------------------------
                with dpg.tab(label="  Settings / PID  "):
                    dpg.add_spacer(height=2)
                    dpg.add_text("1. Hardware & 330F Simulator", color=(140, 200, 255))
                    with dpg.group(horizontal=True):
                        dpg.add_button(
                            label=format_toggle_label(
                                "Virtual 330F Mode", self.config.instruments.virtual_mode
                            ),
                            tag="btn_toggle_virtual_mode",
                            width=255,
                            height=48,
                            callback=lambda: self._on_toggle_virtual_mode_btn(),
                        )
                        b_dis = dpg.add_button(
                            label="Discharge 330F (0V)",
                            tag="btn_discharge_cap",
                            width=205,
                            height=48,
                            callback=lambda: self._on_reset_virtual_capacitor(),
                        )
                        dpg.bind_item_theme(b_dis, "theme_btn_step")

                    with dpg.group(horizontal=True):
                        dpg.add_text("Sim Speed (x):")
                        dpg.add_input_float(
                            label="",
                            tag="input_sim_speed",
                            default_value=5.0,
                            min_value=1.0,
                            max_value=50.0,
                            min_clamped=True,
                            max_clamped=True,
                            step=0.0,
                            format="%.1f",
                            width=145,
                        )
                        b_sim_m = dpg.add_button(
                            label="-5x",
                            tag="btn_sim_speed_minus",
                            width=85,
                            height=44,
                            callback=self._make_step_callback(
                                "input_sim_speed", -5.0, min_value=1.0, max_value=50.0, decimals=1
                            ),
                        )
                        b_sim_p = dpg.add_button(
                            label="+5x",
                            tag="btn_sim_speed_plus",
                            width=85,
                            height=44,
                            callback=self._make_step_callback(
                                "input_sim_speed", 5.0, min_value=1.0, max_value=50.0, decimals=1
                            ),
                        )
                        dpg.bind_item_theme(b_sim_m, "theme_btn_step")
                        dpg.bind_item_theme(b_sim_p, "theme_btn_step")

                    dpg.add_separator()
                    dpg.add_text("2. Anti-Overshoot PID Gains", color=(140, 200, 255))

                    for pid_lbl, pid_tag, pid_val, pid_step, minus_tag, plus_tag in [
                        (
                            "Kp:",
                            "input_pid_kp",
                            self.config.pid.kp,
                            1.0,
                            "btn_pid_kp_minus",
                            "btn_pid_kp_plus",
                        ),
                        (
                            "Ki:",
                            "input_pid_ki",
                            self.config.pid.ki,
                            0.05,
                            "btn_pid_ki_minus",
                            "btn_pid_ki_plus",
                        ),
                        (
                            "Kd:",
                            "input_pid_kd",
                            self.config.pid.kd,
                            1.0,
                            "btn_pid_kd_minus",
                            "btn_pid_kd_plus",
                        ),
                    ]:
                        with dpg.group(horizontal=True):
                            dpg.add_text(pid_lbl)
                            dpg.add_input_float(
                                label="",
                                tag=pid_tag,
                                default_value=pid_val,
                                min_value=0.0,
                                min_clamped=True,
                                step=0.0,
                                format="%.3f",
                                width=220,
                                callback=lambda: self._on_pid_gains_changed(),
                            )
                            b_pm = dpg.add_button(
                                label=f"-{pid_step:g}",
                                tag=minus_tag,
                                width=95,
                                height=42,
                                callback=self._make_step_callback(
                                    pid_tag,
                                    -pid_step,
                                    min_value=0.0,
                                    decimals=3,
                                    after_change=self._on_pid_gains_changed,
                                ),
                            )
                            b_pp = dpg.add_button(
                                label=f"+{pid_step:g}",
                                tag=plus_tag,
                                width=95,
                                height=42,
                                callback=self._make_step_callback(
                                    pid_tag,
                                    pid_step,
                                    min_value=0.0,
                                    decimals=3,
                                    after_change=self._on_pid_gains_changed,
                                ),
                            )
                            dpg.bind_item_theme(b_pm, "theme_btn_step")
                            dpg.bind_item_theme(b_pp, "theme_btn_step")

                    dpg.add_separator()
                    dpg.add_text("3. CSV Telemetry Logging", color=(140, 200, 255))
                    with dpg.group(horizontal=True):
                        dpg.add_button(
                            label=format_toggle_label("CSV Logging", self.config.logging.enabled),
                            tag="btn_toggle_logging",
                            width=255,
                            height=46,
                            callback=lambda: self._on_toggle_logging_btn(),
                        )
                        dpg.add_text("Rows:")
                        dpg.add_text("0", tag="txt_log_rows", color=(130, 225, 150))

                    with dpg.group(horizontal=True):
                        dpg.add_text("Interval (s):")
                        dpg.add_input_float(
                            label="",
                            tag="input_log_interval_s",
                            default_value=self.config.logging.log_interval_s,
                            min_value=0.1,
                            min_clamped=True,
                            step=0.0,
                            format="%.1f",
                            width=155,
                            callback=lambda: self._on_logging_changed(),
                        )
                        b_lm = dpg.add_button(
                            label="-0.5s",
                            tag="btn_log_interval_minus",
                            width=90,
                            height=42,
                            callback=self._make_step_callback(
                                "input_log_interval_s",
                                -0.5,
                                min_value=0.1,
                                decimals=1,
                                after_change=self._on_logging_changed,
                            ),
                        )
                        b_lp = dpg.add_button(
                            label="+0.5s",
                            tag="btn_log_interval_plus",
                            width=90,
                            height=42,
                            callback=self._make_step_callback(
                                "input_log_interval_s",
                                0.5,
                                min_value=0.1,
                                decimals=1,
                                after_change=self._on_logging_changed,
                            ),
                        )
                        dpg.bind_item_theme(b_lm, "theme_btn_step")
                        dpg.bind_item_theme(b_lp, "theme_btn_step")

                    with dpg.group(horizontal=True):
                        dpg.add_input_text(
                            label="",
                            tag="input_log_path",
                            default_value=self.config.logging.log_file_path,
                            width=295,
                            callback=lambda: self._on_logging_changed(),
                        )
                        b_save = dpg.add_button(
                            label="Save Config",
                            tag="btn_save_config",
                            width=165,
                            height=46,
                            callback=lambda: self._on_save_config(),
                        )
                        dpg.bind_item_theme(b_save, "theme_btn_start")

    def _build_right_graphs_panel(self) -> None:
        """Build the right-hand panel with touch toolbar and tabbed stacked/individual plots."""
        stacked_h = compute_stacked_plot_height(800)
        single_h = compute_single_plot_height(800)

        with dpg.child_window(border=True, no_scrollbar=True):
            # Top Touch Graph Toolbar
            with dpg.group(horizontal=True):
                dpg.add_text("Real-Time Graphs", color=(140, 200, 255))
                dpg.add_spacer(width=10)
                dpg.add_button(
                    label=format_toggle_label("Auto-Fit Axes", self.autofit_plots),
                    tag="btn_toggle_autofit",
                    width=225,
                    height=46,
                    callback=lambda: self._on_toggle_autofit_btn(),
                )
                b_clear = dpg.add_button(
                    label="Clear Plots",
                    tag="btn_clear_plots",
                    width=175,
                    height=46,
                    callback=lambda: self._on_clear_history(),
                )
                dpg.bind_item_theme(b_clear, "theme_btn_step")

            # Tabbed Graph View: [All 3 Stacked], [Field (nT)], [Voltage (V)], [Current (A)]
            with dpg.tab_bar(tag="plot_tab_bar"):
                with dpg.tab(label="  All 3 Stacked  "):
                    with dpg.plot(
                        label="Solenoid Magnetic Field (nT)",
                        tag="plot_field_stacked",
                        height=stacked_h,
                        width=-1,
                    ):
                        dpg.add_plot_legend()
                        dpg.add_plot_axis(dpg.mvXAxis, label="Elapsed Time (s)", tag="xaxis_field")
                        with dpg.plot_axis(
                            dpg.mvYAxis, label="Field (nT)", tag="yaxis_field"
                        ):
                            dpg.add_line_series(
                                [], [], label="Measured Field (nT)", tag="series_field_meas"
                            )
                            dpg.add_line_series(
                                [], [], label="Target Field (nT)", tag="series_field_targ"
                            )

                    with dpg.plot(
                        label="TTi MX100TP Output & Capacitor Voltage (V)",
                        tag="plot_volt_stacked",
                        height=stacked_h,
                        width=-1,
                    ):
                        dpg.add_plot_legend()
                        dpg.add_plot_axis(dpg.mvXAxis, label="Elapsed Time (s)", tag="xaxis_volt")
                        with dpg.plot_axis(dpg.mvYAxis, label="Voltage (V)", tag="yaxis_volt"):
                            dpg.add_line_series(
                                [], [], label="Commanded Voltage (V)", tag="series_volt_set"
                            )
                            dpg.add_line_series(
                                [], [], label="Terminal Voltage (V)", tag="series_volt_meas"
                            )

                    with dpg.plot(
                        label="Keysight 34461A Coil Current & PSU Current (A)",
                        tag="plot_curr_stacked",
                        height=stacked_h,
                        width=-1,
                    ):
                        dpg.add_plot_legend()
                        dpg.add_plot_axis(dpg.mvXAxis, label="Elapsed Time (s)", tag="xaxis_curr")
                        with dpg.plot_axis(dpg.mvYAxis, label="Current (A)", tag="yaxis_curr"):
                            dpg.add_line_series(
                                [], [], label="Measured Coil Current (A)", tag="series_curr_coil"
                            )
                            dpg.add_line_series(
                                [], [], label="Target Coil Current (A)", tag="series_curr_targ"
                            )
                            dpg.add_line_series(
                                [], [], label="PSU Sourced Current (A)", tag="series_curr_psu"
                            )

                with dpg.tab(label="  Field (nT)  "):
                    with dpg.plot(
                        label="Solenoid Magnetic Field (nT) — Detailed View",
                        tag="plot_field_single",
                        height=single_h,
                        width=-1,
                    ):
                        dpg.add_plot_legend()
                        dpg.add_plot_axis(
                            dpg.mvXAxis, label="Elapsed Time (s)", tag="xaxis_field_single"
                        )
                        with dpg.plot_axis(
                            dpg.mvYAxis, label="Magnetic Field (nT)", tag="yaxis_field_single"
                        ):
                            dpg.add_line_series(
                                [],
                                [],
                                label="Measured Field (nT)",
                                tag="series_field_meas_single",
                            )
                            dpg.add_line_series(
                                [],
                                [],
                                label="Target Field (nT)",
                                tag="series_field_targ_single",
                            )

                with dpg.tab(label="  Voltage (V)  "):
                    with dpg.plot(
                        label="TTi MX100TP Output & Capacitor Voltage (V) — Detailed View",
                        tag="plot_volt_single",
                        height=single_h,
                        width=-1,
                    ):
                        dpg.add_plot_legend()
                        dpg.add_plot_axis(
                            dpg.mvXAxis, label="Elapsed Time (s)", tag="xaxis_volt_single"
                        )
                        with dpg.plot_axis(
                            dpg.mvYAxis, label="Voltage (V)", tag="yaxis_volt_single"
                        ):
                            dpg.add_line_series(
                                [],
                                [],
                                label="PSU Commanded Voltage (V)",
                                tag="series_volt_set_single",
                            )
                            dpg.add_line_series(
                                [],
                                [],
                                label="PSU Measured Terminal Voltage (V)",
                                tag="series_volt_meas_single",
                            )

                with dpg.tab(label="  Current (A)  "):
                    with dpg.plot(
                        label="Keysight 34461A Coil Current & PSU Current (A) — Detailed View",
                        tag="plot_curr_single",
                        height=single_h,
                        width=-1,
                    ):
                        dpg.add_plot_legend()
                        dpg.add_plot_axis(
                            dpg.mvXAxis, label="Elapsed Time (s)", tag="xaxis_curr_single"
                        )
                        with dpg.plot_axis(
                            dpg.mvYAxis, label="Current (A)", tag="yaxis_curr_single"
                        ):
                            dpg.add_line_series(
                                [],
                                [],
                                label="Measured Coil Current (A)",
                                tag="series_curr_coil_single",
                            )
                            dpg.add_line_series(
                                [],
                                [],
                                label="Target Coil Current (A)",
                                tag="series_curr_targ_single",
                            )
                            dpg.add_line_series(
                                [],
                                [],
                                label="PSU Sourced Current (A)",
                                tag="series_curr_psu_single",
                            )

    def _build_window_layout(self) -> None:
        """Construct the Dear PyGui touch themes, primary window, tabs, and plots."""
        self._setup_touch_themes()

        with dpg.window(
            tag="primary_window",
            label="TTi MX100TP & Keysight 34461A Solenoid Field Controller (Touch HMI)",
            no_scrollbar=True,
        ):
            self._build_top_header_bar()

            with dpg.group(horizontal=True):
                self._build_left_control_panel()
                self._build_right_graphs_panel()

        # Apply initial ON/OFF themes to all touch toggle buttons
        self._apply_toggle_button_style(
            "btn_toggle_virtual_mode",
            "Virtual 330F Mode",
            self.config.instruments.virtual_mode,
        )
        self._apply_toggle_button_style(
            "btn_toggle_logging",
            "CSV Logging",
            self.config.logging.enabled,
        )
        self._apply_toggle_button_style(
            "btn_toggle_autofit",
            "Auto-Fit Axes",
            self.autofit_plots,
        )

    def build_and_run(self) -> None:
        """Create the Dear PyGui context, construct the touch HMI, and start the render loop."""
        _start_flatpak_x11_bridge_if_needed()
        dpg.create_context()
        self._dpg_context_ready = True

        try:
            self._build_window_layout()

            dpg.create_viewport(
                title="TTi MX100TP & Keysight 34461A — Solenoid Magnetic Field Controller",
                width=1280,
                height=800,
            )
            dpg.set_viewport_resize_callback(lambda: self._on_viewport_resize())
            dpg.setup_dearpygui()
            dpg.show_viewport()
            dpg.set_primary_window("primary_window", True)
            self._on_viewport_resize()

            while dpg.is_dearpygui_running():
                self._tick()
                dpg.render_dearpygui_frame()
        finally:
            self._dpg_context_ready = False
            self.controller.disconnect_instruments()
            dpg.destroy_context()


def launch_gui(config_path: str | Path = "config.ini", force_virtual: bool = False) -> None:
    """Load configuration and launch the Dear PyGui touch HMI application."""
    config = load_config(config_path)
    if force_virtual:
        config.instruments.virtual_mode = True
    app = CoilDriverGUI(config=config, config_path=config_path)
    app.build_and_run()
