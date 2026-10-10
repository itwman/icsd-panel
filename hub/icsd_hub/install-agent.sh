#!/usr/bin/env bash
# ICSD Panel — agent installer. Served by the hub at /install.sh.
#
#   curl -fsSL __HUB_URL__/install.sh | sudo bash -s -- --token TOKEN
#   curl -fsSL __HUB_URL__/install.sh | sudo bash -s -- --upgrade
#   curl -fsSL __HUB_URL__/install.sh | sudo bash -s -- --uninstall
#
# What it does: downloads one binary from the hub (checksum verified),
# creates the unprivileged user `icsd-agent`, enrolls, and starts a
# hardened systemd service. It opens no port and installs no packages.
set -euo pipefail

HUB="__HUB_URL__"
TOKEN=""; MODE="install"; DOCKER="auto"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2 ;;
    --upgrade) MODE="upgrade"; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    --no-docker) DOCKER="off"; shift ;;
    --hub) HUB="${2:-}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;32m▸\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
command -v systemctl >/dev/null || die "systemd is required"

BIN=/usr/local/bin/icsd-agent
CONF_DIR=/etc/icsd-agent
UNIT=/etc/systemd/system/icsd-agent.service

if [[ $MODE == uninstall ]]; then
  systemctl disable --now icsd-agent 2>/dev/null || true
  rm -f "$UNIT" "$BIN"; rm -rf "$CONF_DIR"
  systemctl daemon-reload
  id icsd-agent >/dev/null 2>&1 && userdel icsd-agent || true
  say "icsd-agent removed. Delete the server in the panel too."
  exit 0
fi

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "unsupported CPU: $(uname -m)" ;;
esac

if command -v curl >/dev/null; then fetch() { curl -fsSL --retry 3 -o "$2" "$1"; }
elif command -v wget >/dev/null; then fetch() { wget -q -O "$2" "$1"; }
else die "curl or wget is required"; fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
say "downloading agent ($ARCH) from $HUB"
fetch "$HUB/dl/icsd-agent-linux-$ARCH" "$TMP/icsd-agent" || die "download failed"
fetch "$HUB/dl/SHA256SUMS" "$TMP/SHA256SUMS" || die "checksum download failed"
want="$(awk -v f="icsd-agent-linux-$ARCH" '$2==f {print $1}' "$TMP/SHA256SUMS")"
got="$(sha256sum "$TMP/icsd-agent" | awk '{print $1}')"
[[ -n "$want" && "$want" == "$got" ]] || die "checksum mismatch — not installing"
install -m 0755 "$TMP/icsd-agent" "$BIN"
say "installed $($BIN version) → $BIN"

if ! id icsd-agent >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin icsd-agent
fi
# Docker: reading containers needs the docker group. That group is
# root-equivalent, so it can be turned off with --no-docker — and an
# upgrade keeps that choice.
if [[ $MODE == upgrade && -f "$CONF_DIR/agent.json" ]] && grep -Eq '"docker": *"off"' "$CONF_DIR/agent.json"; then
  DOCKER=off
fi
if [[ $DOCKER != off ]] && getent group docker >/dev/null; then
  usermod -aG docker icsd-agent
  say "icsd-agent added to group docker (container monitoring)"
fi

if [[ $MODE == install ]]; then
  [[ -n "$TOKEN" ]] || die "--token is required (copy the command from the panel)"
  DOCKER_FLAG=()
  [[ $DOCKER == off ]] && DOCKER_FLAG=(--docker off)
  "$BIN" enroll --hub "$HUB" --token "$TOKEN" --config "$CONF_DIR/agent.json" ${DOCKER_FLAG[@]+"${DOCKER_FLAG[@]}"}
fi
[[ -f "$CONF_DIR/agent.json" ]] || die "not enrolled: run with --token"
chown -R root:icsd-agent "$CONF_DIR"
chmod 0750 "$CONF_DIR"; chmod 0640 "$CONF_DIR/agent.json"

cat > "$UNIT" <<'EOF'
[Unit]
Description=ICSD Panel agent
After=network-online.target
Wants=network-online.target

[Service]
User=icsd-agent
ExecStart=/usr/local/bin/icsd-agent run --config /etc/icsd-agent/agent.json
Restart=always
RestartSec=5
# Read-only monitoring: the agent can change nothing on this server.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
CapabilityBoundingSet=
MemoryMax=64M
CPUQuota=20%

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable icsd-agent >/dev/null 2>&1
systemctl restart icsd-agent
sleep 2
if systemctl is-active --quiet icsd-agent; then
  say "icsd-agent is running. The server should appear online in the panel within seconds."
else
  journalctl -u icsd-agent -n 20 --no-pager || true
  die "icsd-agent did not start (log above)"
fi
