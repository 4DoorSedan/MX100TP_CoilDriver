#!/usr/bin/env bash
# ==============================================================================
# run.sh — Activate .venv, ensure X11/XWayland display access, and launch main.py
# ==============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# 1. If executed from the host shell where host Python (e.g. 3.14) differs from
#    the Flatpak SDK Python (3.13) used to build .venv, re-enter via Flatpak with X11 enabled.
if [[ ! -f "/.flatpak-info" ]] && [[ -x ".venv/bin/python" ]]; then
    if ! .venv/bin/python -c "import dearpygui.dearpygui" >/dev/null 2>&1; then
        if command -v flatpak >/dev/null 2>&1 && flatpak info com.visualstudio.code >/dev/null 2>&1; then
            echo "[run.sh] Launching via Flatpak SDK runtime with X11 socket enabled..."
            exec flatpak run --socket=x11 --filesystem="$PROJECT_DIR" --command=bash \
                com.visualstudio.code "$PROJECT_DIR/run.sh" "$@"
        fi
    fi
fi

# 2. Create virtual environment and install dependencies if missing
if [[ ! -f ".venv/bin/activate" ]]; then
    echo "[run.sh] Creating Python virtual environment in .venv..."
    python3 -m venv .venv
    # shellcheck disable=SC1091
    source ".venv/bin/activate"
    pip install --upgrade pip
    pip install dearpygui pytest
else
    # shellcheck disable=SC1091
    source ".venv/bin/activate"
fi

# 3. If running inside a Flatpak container where fallback-x11 hid DISPLAY / /tmp/.X11-unix,
#    automatically bridge the host's XWayland socket and Xauthority so Dear PyGui opens cleanly.
X11_BRIDGE_PID=""
cleanup() {
    if [[ -n "$X11_BRIDGE_PID" ]] && kill -0 "$X11_BRIDGE_PID" 2>/dev/null; then
        kill "$X11_BRIDGE_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

if [[ -f "/.flatpak-info" ]] && command -v flatpak-spawn >/dev/null 2>&1; then
    HOST_DISPLAY="$(flatpak-spawn --host printenv DISPLAY 2>/dev/null || echo ":0")"
    DISPLAY_NUM="${HOST_DISPLAY#:}"
    DISPLAY_NUM="${DISPLAY_NUM%%.*}"
    [[ -z "$DISPLAY_NUM" ]] && DISPLAY_NUM="0"

    HOST_XAUTH="$(flatpak-spawn --host printenv XAUTHORITY 2>/dev/null || true)"
    if [[ -n "$HOST_XAUTH" ]]; then
        flatpak-spawn --host cat "$HOST_XAUTH" > "/tmp/.Xauthority" 2>/dev/null || true
        export XAUTHORITY="/tmp/.Xauthority"
    fi

    mkdir -p /tmp/.X11-unix
    LOCAL_XSOCK="/tmp/.X11-unix/X${DISPLAY_NUM}"
    HOST_XSOCK="/tmp/.X11-unix/X${DISPLAY_NUM}"

    if [[ ! -S "$LOCAL_XSOCK" ]]; then
        echo "[run.sh] Bridging host XWayland socket (${HOST_XSOCK}) into Flatpak environment..."
        python - "$LOCAL_XSOCK" "$HOST_XSOCK" <<'PYEOF' &
import os
import select
import socket
import subprocess
import sys
import threading

local_sock_path, host_sock_path = sys.argv[1], sys.argv[2]
try:
    os.unlink(local_sock_path)
except FileNotFoundError:
    pass

server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(local_sock_path)
server.listen(16)

def bridge_client(client_sock: socket.socket) -> None:
    proc = subprocess.Popen(
        [
            "flatpak-spawn",
            "--host",
            "python3",
            "-c",
            (
                "import os, select, socket, sys; "
                f"s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect({host_sock_path!r}); "
                "os.set_blocking(0, False); s.setblocking(False); "
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

while True:
    conn, _ = server.accept()
    threading.Thread(target=bridge_client, args=(conn,), daemon=True).start()
PYEOF
        X11_BRIDGE_PID=$!
        for _ in {1..30}; do
            [[ -S "$LOCAL_XSOCK" ]] && break
            sleep 0.05
        done
    fi
    export DISPLAY=":${DISPLAY_NUM}"
fi

echo "[run.sh] Launching Solenoid Magnetic Field Controller (DISPLAY=${DISPLAY:-unset})..."
python "$PROJECT_DIR/main.py" "$@"
