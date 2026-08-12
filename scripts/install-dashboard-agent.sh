#!/usr/bin/env bash
# Install the authenticated Personal Assistant dashboard agent on the Dell.
# Run locally as: sudo bash scripts/install-dashboard-agent.sh
# The token is read without echo and is never printed or passed as an argument.
set -Eeuo pipefail

PROJECT=/home/sanel/personal-assistant-bot
UNIT_SOURCE="$PROJECT/scripts/pab-dashboard-agent.service"
AGENT_SOURCE="$PROJECT/scripts/dashboard_agent.py"
ACTION_SOURCE="$PROJECT/scripts/pab-dashboard-action"
ENV_FILE=/etc/personal-assistant-dashboard-agent.env
UNIT_DEST=/etc/systemd/system/pab-dashboard-agent.service
ACTION_DEST=/usr/local/libexec/pab-dashboard-action
SUDOERS_DEST=/etc/sudoers.d/pab-dashboard-agent

if [[ $(id -u) -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 77
fi

for required in "$UNIT_SOURCE" "$AGENT_SOURCE" "$ACTION_SOURCE" "$PROJECT/venv/bin/python"; do
    [[ -f $required || -x $required ]] || { echo "Missing required file: $required" >&2; exit 66; }
done

existing_token=''
if [[ -r $ENV_FILE ]]; then
    existing_token=$(awk -F= '$1 == "PAB_DASHBOARD_AGENT_TOKEN" {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE")
fi

read -r -s -p "Shared dashboard token (leave blank to keep existing): " token
echo
token=${token:-$existing_token}
[[ $token =~ ^[A-Za-z0-9._~-]{32,128}$ ]] || { echo "Use a 32-128 character URL-safe token." >&2; exit 65; }

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
{
    printf 'PAB_DASHBOARD_AGENT_TOKEN=%s\n' "$token"
    printf 'PAB_DASHBOARD_AGENT_HOST=0.0.0.0\n'
    printf 'PAB_DASHBOARD_AGENT_PORT=8765\n'
} >"$tmp"
install -o root -g sanel -m 0640 "$tmp" "$ENV_FILE"

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0750 "$ACTION_SOURCE" "$ACTION_DEST"
install -o root -g root -m 0644 "$UNIT_SOURCE" "$UNIT_DEST"

tmp_sudoers=$(mktemp)
trap 'rm -f "$tmp" "$tmp_sudoers"' EXIT
cat >"$tmp_sudoers" <<'EOF'
# The dashboard agent may invoke only this root-owned, allowlisted action runner.
sanel ALL=(root) NOPASSWD: /usr/local/libexec/pab-dashboard-action
EOF
visudo -cf "$tmp_sudoers" >/dev/null
install -o root -g root -m 0440 "$tmp_sudoers" "$SUDOERS_DEST"

systemctl daemon-reload
systemctl enable --now pab-dashboard-agent.service
systemctl is-active --quiet pab-dashboard-agent.service

if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow from 10.0.0.47 to any port 8765 proto tcp
    ufw allow from 100.70.208.60 to any port 8765 proto tcp
    echo "UFW rules added for the Surface LAN and Tailscale addresses."
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.0.0.47" port port="8765" protocol="tcp" accept'
    firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="100.70.208.60" port port="8765" protocol="tcp" accept'
    firewall-cmd --reload
    echo "firewalld rules added for the Surface LAN and Tailscale addresses."
else
    echo "No active UFW/firewalld policy detected; the agent is protected by its bearer token."
fi

echo "pab-dashboard-agent.service is active on port 8765."
