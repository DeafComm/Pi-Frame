#!/bin/bash
# Start the photo frame on the Pi's screen. Safe to run from an SSH session.

DIR="$(cd "$(dirname "$0")" && pwd)"

if pgrep -u "$(id -u)" -f "python3 .*photoframe\.py" >/dev/null; then
    echo "Photo frame is already running. Use ./stop_frame.sh first."
    exit 1
fi

# Point the program at the desktop on the Pi's screen (not the SSH session).
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DISPLAY="${FRAME_DISPLAY:-:0}"
export WAYLAND_DISPLAY="${FRAME_WAYLAND_DISPLAY:-wayland-0}"

nohup python3 "$DIR/photoframe.py" >/dev/null 2>&1 &
sleep 3

if pgrep -u "$(id -u)" -f "python3 .*photoframe\.py" >/dev/null; then
    echo "Photo frame started."
else
    echo "Photo frame did not start. Last lines of the log:"
    tail -n 10 "$DIR/photoframe.log" 2>/dev/null
    exit 1
fi
