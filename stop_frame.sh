#!/bin/bash
# Stop the photo frame.

if ! pgrep -u "$(id -u)" -f "python3 .*photoframe\.py" >/dev/null; then
    echo "Photo frame is not running."
    exit 0
fi

pkill -TERM -u "$(id -u)" -f "python3 .*photoframe\.py"

for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.5
    pgrep -u "$(id -u)" -f "python3 .*photoframe\.py" >/dev/null || { echo "Photo frame stopped."; exit 0; }
done

pkill -KILL -u "$(id -u)" -f "python3 .*photoframe\.py"
echo "Photo frame was forced to stop."
