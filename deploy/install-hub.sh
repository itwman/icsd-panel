#!/usr/bin/env bash
# ICSD Panel hub — install WITHOUT Docker, next to sites that already run
# on this server (Django, WordPress…). Nothing existing is modified:
#
#   * the hub runs as user `icsd`, listening on 127.0.0.1 only (systemd)
#   * nginx gets ONE new site file for the panel's domain; `nginx -t` is
#     checked before reload and the file is removed again if it fails
#   * certbot issues a certificate for that domain only
#   * the agent binaries are built once with a temporary Go toolchain,
#     which is deleted afterwards
#
# Usage (from the cloned repository, e.g. /opt/icsd-panel):
#   sudo bash deploy/install-hub.sh --domain panel.example.com --email you@example.com
#   sudo bash deploy/install-hub.sh --upgrade            # after `git pull`
#
# Options:
#   --domain D     the panel's domain (an A record must point to this server)
#   --email E      for Let's Encrypt expiry notices (optional)
#   --port P       local port (default 8088)
#   --no-nginx     do not touch nginx; print the config to add by hand
#   --no-ssl       skip certbot (e.g. you already have a certificate)
#   --upgrade      reinstall dependencies, rebuild the agent, restart
set -euo pipefail

DOMAIN=""; EMAIL=""; PORT=8088; NGINX=1; SSL=1; MODE=install
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --no-nginx) NGINX=0; shift ;;
    --no-ssl) SSL=0; shift ;;
    --upgrade) MODE=upgrade; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;32m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
command -v systemctl >/dev/null || die "systemd is required"
command -v apt-get >/dev/null || die "this installer supports Debian/Ubuntu (apt)"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$REPO/hub/icsd_hub/main.py" && -d "$REPO/agent" ]] || die "run this from the icsd-panel repository"
case "$REPO" in
  /root/*|/home/*) die "clone the repository to /opt/icsd-panel (the service cannot read $REPO)" ;;
esac
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/hub/icsd_hub/__init__.py")"
ENV_FILE=/etc/icsd-hub.env
DATA=/var/lib/icsd-hub
DIST="$DATA/dist"
VENV="$REPO/.venv"
UNIT=/etc/systemd/system/icsd-hub.service
GO_VERSION=1.24.7

if [[ $MODE == upgrade ]]; then
  [[ -f "$ENV_FILE" ]] || die "not installed yet (no $ENV_FILE); run without --upgrade"
  DOMAIN="$(sed -n 's|^ICSD_PUBLIC_URL=https\?://||p' "$ENV_FILE")"
  PORT="$(sed -n 's/^ICSD_PORT=//p' "$ENV_FILE")"; PORT="${PORT:-8088}"
else
  [[ -n "$DOMAIN" ]] || die "--domain is required (e.g. --domain panel.example.com)"
  [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "invalid domain: $DOMAIN"
fi
[[ "$PORT" =~ ^[0-9]+$ ]] || die "invalid port: $PORT"

# ── 0. pre-flight: fail before changing anything ────────────────────
if [[ ! -f "$ENV_FILE" ]] && ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$PORT\$"; then
  die "port $PORT is already in use; choose another with --port"
fi
if [[ $MODE == install && $NGINX == 1 ]] && command -v nginx >/dev/null \
   && grep -rqs "server_name[^;]*\b$DOMAIN\b" /etc/nginx --exclude=icsd-panel.conf; then
  die "nginx already has a site for $DOMAIN; remove it or use --no-nginx"
fi

# ── 1. packages: only what is missing ────────────────────────────────
need=()
command -v python3 >/dev/null || need+=("python3")
command -v curl >/dev/null || need+=("curl")
command -v python3 >/dev/null && python3 -c 'import venv, ensurepip' 2>/dev/null || need+=("python3-venv")
if [[ ${#need[@]} -gt 0 ]]; then
  say "installing: ${need[*]}"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" >/dev/null
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10+ is required (found: $(python3 --version 2>&1)); Ubuntu 22.04+ / Debian 12+ have it"

# ── 2. user, data, Python environment ────────────────────────────────
id icsd >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin icsd
install -d -m 0700 -o icsd -g icsd "$DATA"
install -d -m 0755 -o icsd -g icsd "$DIST"
if [[ ! -x "$VENV/bin/python" ]]; then
  say "creating Python environment in $VENV"
  python3 -m venv "$VENV"
fi
say "installing Python dependencies"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$REPO/hub/requirements.txt"

# ── 3. agent binaries (temporary Go, vendored dependencies) ──────────
if [[ $MODE == install || ! -f "$DIST/VERSION" || "$(cat "$DIST/VERSION")" != "$VERSION" || "${REBUILD_AGENT:-0}" == 1 ]]; then
  case "$(uname -m)" in
    x86_64|amd64) HOST_ARCH=amd64 ;;
    aarch64|arm64) HOST_ARCH=arm64 ;;
    *) die "unsupported CPU: $(uname -m)" ;;
  esac
  GOTMP="$(mktemp -d)"; trap 'rm -rf "$GOTMP"' EXIT
  say "downloading Go $GO_VERSION (temporary, only to build the agent)"
  curl -fsSL --retry 3 "https://go.dev/dl/go$GO_VERSION.linux-$HOST_ARCH.tar.gz" | tar -xz -C "$GOTMP"
  for arch in amd64 arm64; do
    say "building agent $VERSION for linux/$arch"
    ( cd "$REPO/agent" && \
      HOME="$GOTMP" GOCACHE="$GOTMP/cache" GOPATH="$GOTMP/gopath" GOFLAGS=-mod=vendor \
      CGO_ENABLED=0 GOOS=linux GOARCH=$arch "$GOTMP/go/bin/go" build -trimpath \
        -ldflags "-s -w -X main.Version=$VERSION" -o "$DIST/icsd-agent-linux-$arch" . )
  done
  ( cd "$DIST" && sha256sum icsd-agent-linux-* > SHA256SUMS && echo "$VERSION" > VERSION )
  chown -R icsd:icsd "$DIST"
  rm -rf "$GOTMP"; trap - EXIT
fi

# ── 4. configuration (kept on upgrade) ──────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  cat > "$ENV_FILE" <<EOF
# ICSD Panel hub — read by systemd (icsd-hub.service)
ICSD_PUBLIC_URL=https://$DOMAIN
ICSD_DATA_DIR=$DATA
ICSD_DIST_DIR=$DIST
ICSD_REQUIRE_2FA=1
ICSD_SAMPLE_INTERVAL=5
ICSD_PORT=$PORT
# Trust X-Forwarded-For only from the local nginx.
FORWARDED_ALLOW_IPS=127.0.0.1,::1
EOF
  chmod 0640 "$ENV_FILE"; chgrp icsd "$ENV_FILE"
  umask 022
fi

# ── 5. systemd service ───────────────────────────────────────────────
cat > "$UNIT" <<EOF
[Unit]
Description=ICSD Panel hub
After=network-online.target
Wants=network-online.target

[Service]
User=icsd
Group=icsd
EnvironmentFile=$ENV_FILE
WorkingDirectory=$REPO/hub
ExecStart=$VENV/bin/uvicorn icsd_hub.main:app --host 127.0.0.1 --port $PORT --proxy-headers --no-server-header
Restart=always
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DATA
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=
MemoryMax=300M

[Install]
WantedBy=multi-user.target
EOF
cat > /usr/local/bin/icsd-hub <<EOF
#!/bin/sh
# Hub command line (create-admin, set-password, reset-2fa, users) as the icsd user.
set -a; . $ENV_FILE; set +a
cd $REPO/hub && exec runuser -u icsd -- $VENV/bin/python -m icsd_hub "\$@"
EOF
chmod 0755 /usr/local/bin/icsd-hub
chmod -R a+rX "$REPO"
systemctl daemon-reload
systemctl enable icsd-hub >/dev/null 2>&1
systemctl restart icsd-hub
for _ in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 \
  || { journalctl -u icsd-hub -n 30 --no-pager; die "the hub did not start (log above)"; }
say "hub $VERSION is running on 127.0.0.1:$PORT"

# ── 6. nginx: one new site, verified before reload ──────────────────
LISTEN6=""
[[ -s /proc/net/if_inet6 ]] && LISTEN6=$'\n    listen [::]:80;'   # only where IPv6 exists
NGINX_SNIPPET="server {
    listen 80;$LISTEN6
    server_name $DOMAIN;

    client_max_body_size 600m;   # file uploads from the file manager

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \"upgrade\";
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 600s;
        proxy_request_buffering off;
    }
}"

if [[ $MODE == upgrade ]]; then
  # v2.1: uploads up to 512 MB and long downloads through the panel.
  for f in /etc/nginx/sites-available/icsd-panel.conf /etc/nginx/conf.d/icsd-panel.conf; do
    [[ -f $f ]] || continue
    if grep -q "client_max_body_size 2m;" "$f"; then
      cp "$f" "$f.bak"
      sed -i 's/client_max_body_size 2m;/client_max_body_size 600m;/; s/proxy_read_timeout 120s;/proxy_read_timeout 600s;/' "$f"
      if nginx -t >/dev/null 2>&1; then
        systemctl reload nginx; rm -f "$f.bak"; say "nginx: upload limit raised to 600 MB"
      else
        mv "$f.bak" "$f"; warn "nginx rejected the change; left as it was"
      fi
    fi
  done
fi

if [[ $MODE == install ]]; then
  if [[ $NGINX == 1 ]] && command -v nginx >/dev/null; then
    if [[ -d /etc/nginx/sites-available && -d /etc/nginx/sites-enabled ]]; then
      SITE=/etc/nginx/sites-available/icsd-panel.conf; LINK=/etc/nginx/sites-enabled/icsd-panel.conf
    else
      SITE=/etc/nginx/conf.d/icsd-panel.conf; LINK=""
    fi
    printf '%s\n' "$NGINX_SNIPPET" > "$SITE"
    [[ -n "$LINK" ]] && ln -sf "$SITE" "$LINK"
    if ! nginx -t >/dev/null 2>&1; then
      rm -f "$SITE" ${LINK:+"$LINK"}
      nginx -t || true
      die "nginx rejected the configuration; nothing was changed"
    fi
    systemctl reload nginx
    say "nginx: added $SITE (other sites untouched)"

    if [[ $SSL == 1 ]]; then
      if ! command -v certbot >/dev/null || ! certbot plugins 2>/dev/null | grep -q nginx; then
        say "installing certbot (nginx plugin)"
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
      fi
      MAIL=(--register-unsafely-without-email); [[ -n "$EMAIL" ]] && MAIL=(-m "$EMAIL")
      if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect "${MAIL[@]}"; then
        say "SSL certificate issued for $DOMAIN"
      else
        warn "certbot failed. Check that the A record of $DOMAIN points to this server, then run:"
        echo "    certbot --nginx -d $DOMAIN --redirect"
      fi
    fi
  else
    warn "nginx not used. Put this in front of the hub (WebSocket support is required):"
    echo "$NGINX_SNIPPET"
  fi
fi

echo
say "done. Next steps:"
if [[ "$(/usr/local/bin/icsd-hub users 2>/dev/null | grep -c .)" == 0 ]]; then
  echo "    1) create the first admin:   sudo icsd-hub create-admin"
  echo "    2) open https://$DOMAIN, log in and turn on 2FA"
  echo "    3) «افزودن سرور» → run the command it shows on each server (this one too)"
else
  echo "    open https://$DOMAIN — if a server shows «agent update available», run the --upgrade command it shows"
fi
