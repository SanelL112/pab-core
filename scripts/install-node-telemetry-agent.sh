#!/usr/bin/env bash
# Run locally on a node as root: sudo bash install-node-telemetry-agent.sh
set -Eeuo pipefail

SOURCE_DIR=$(cd "$(dirname "$0")" && pwd)
INSTALL_DIR=/opt/node-telemetry
ENV_FILE=/etc/node-telemetry-agent.env
UNIT_FILE=/etc/systemd/system/node-telemetry-agent.service

if [[ $(id -u) -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 77
fi

read -r -s -p "Node telemetry token: " token
echo
[[ $token =~ ^[A-Za-z0-9._~-]{32,128}$ ]] || { echo "Use a 32-128 character URL-safe token." >&2; exit 65; }

install -d -o root -g root -m 0755 "$INSTALL_DIR"
install -o root -g root -m 0644 "$SOURCE_DIR/node_telemetry.py" "$SOURCE_DIR/node_telemetry_agent.py" "$INSTALL_DIR/"
install -o root -g root -m 0644 "$SOURCE_DIR/node-telemetry-agent.service" "$UNIT_FILE"

temp=$(mktemp)
trap 'rm -f "$temp"' EXIT
{
    printf 'NODE_TELEMETRY_TOKEN=%s\n' "$token"
    printf 'NODE_TELEMETRY_HOST=0.0.0.0\n'
    printf 'NODE_TELEMETRY_PORT=8767\n'
} >"$temp"
install -o root -g root -m 0600 "$temp" "$ENV_FILE"

systemctl daemon-reload
systemctl enable --now node-telemetry-agent.service
systemctl is-active --quiet node-telemetry-agent.service
echo "Node telemetry agent is active on port 8767."
