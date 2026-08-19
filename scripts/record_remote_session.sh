#!/usr/bin/env bash
# Auto-detecting Screen Recorder for Remote & Local Sessions
mkdir -p /home/sanel/personal-assistant-bot/logs

export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

# Auto-detect active user display
if [ -n "$1" ]; then
    TARGET_DISP="$1"
else
    # Check common display sockets
    for d in :10 :1 :0 :99; do
        num="${d#:}"
        if [ -e "/tmp/.X11-unix/X${num}" ]; then
            if xdpyinfo -display "${d}" >/dev/null 2>&1; then
                TARGET_DISP="${d}"
                break
            fi
        fi
    done
fi

TARGET_DISP="${TARGET_DISP:-:0}"
OUTPUT_FILE="/home/sanel/personal-assistant-bot/logs/session_recording_$(date +%Y%m%d_%H%M%S).mp4"

echo "=================================================="
echo "  RECORDING SESSION ON DISPLAY: ${TARGET_DISP}"
echo "  Target File: ${OUTPUT_FILE}"
echo "=================================================="

RES=$(xdpyinfo -display "${TARGET_DISP}" 2>/dev/null | grep 'dimensions:' | awk '{print $2}')
RES="${RES:-1920x1080}"

echo "Resolution: ${RES}"
echo "Recording started with ffmpeg..."

exec ffmpeg -y -video_size "${RES}" -framerate 25 -f x11grab -i "${TARGET_DISP}.0" \
     -c:v libx264 -preset ultrafast -pix_fmt yuv420p "${OUTPUT_FILE}"
