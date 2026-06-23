#!/usr/bin/env bash
# ICSD Panel installer — distro & mirror aware, full web stack
# نصب‌کنندهٔ پنل ICSD — آگاه از توزیع و میرورهای ایرانی + نصب کل وب‌استک
# Supports: Ubuntu 22/24, Debian 12, AlmaLinux 9, Rocky Linux 9
#
# Usage:
#   sudo bash install.sh                 # interactive
#   ICSD_STACK=full sudo bash install.sh # install nginx/php/db/acme/fail2ban too
#   ICSD_STACK=panel sudo bash install.sh# panel only (old behavior)
#   ICSD_MAIL=yes  ...                   # also install Postfix+Dovecot (heavy)
set -euo pipefail

PANEL_USER="icsdpanel"
INSTALL_DIR="/opt/icsdpanel"
SERVICE_NAME="icsdpanel"
PORT="${ICSD_PORT:-8088}"
USE_IRAN_MIRROR="${ICSD_IRAN_MIRROR:-ask}"   # ask | yes | no
INSTALL_STACK="${ICSD_STACK:-ask}"           # ask | full | panel
INSTALL_MAIL="${ICSD_MAIL:-no}"              # yes | no
DB_ENGINE="${ICSD_DB:-mariadb}"              # mariadb | none

c_green='\033[0;32m'; c_yellow='\033[1;33m'; c_red='\033[0;31m'; c_reset='\033[0m'
log()  { echo -e "${c_green}[ICSD]${c_reset} $*"; }
warn() { echo -e "${c_yellow}[ICSD]${c_reset} $*"; }
err()  { echo -e "${c_red}[ICSD]${c_reset} $*" >&2; }

[[ $EUID -eq 0 ]] || { err "این اسکریپت باید با root اجرا شود. / Run as root."; exit 1; }

# ---------- Detect distribution ----------
. /etc/os-release 2>/dev/null || { err "/etc/os-release not found."; exit 1; }
OS_ID="${ID:-unknown}"; OS_VER="${VERSION_ID:-}"
log "توزیع شناسایی‌شده / Detected: ${PRETTY_NAME:-$OS_ID $OS_VER}"

case "$OS_ID" in
  ubuntu|debian) PKG="apt"; FAMILY="debian" ;;
  almalinux|rocky|rhel) PKG="dnf"; FAMILY="rhel" ;;
  centos)
    MAJOR="${OS_VER%%.*}"
    if [[ "$MAJOR" == "7" || "$MAJOR" == "8" ]]; then
      err "CentOS Linux $MAJOR به پایان عمر (EOL) رسیده — به AlmaLinux 9 یا Rocky Linux 9 مهاجرت کنید."
      err "CentOS Linux $MAJOR is EOL. Please migrate to AlmaLinux 9 or Rocky Linux 9."
      exit 1
    fi
    PKG="dnf"; FAMILY="rhel"
    warn "CentOS Stream — برای پروداکشن پایدار AlmaLinux/Rocky توصیه می‌شود."
    ;;
  *) err "توزیع پشتیبانی‌نشده: $OS_ID. Ubuntu/Debian یا AlmaLinux/Rocky را استفاده کنید."; exit 1 ;;
esac

# ---------- Interactive choices ----------
if [[ "$INSTALL_STACK" == "ask" ]]; then
  echo ""
  warn "وب‌استک کامل (nginx, php-fpm, $DB_ENGINE, acme.sh, fail2ban) نصب شود؟"
  read -rp "Install the full web stack now? (Y/n) " a
  [[ "$a" =~ ^[Nn]$ ]] && INSTALL_STACK="panel" || INSTALL_STACK="full"
fi
if [[ "$INSTALL_STACK" == "full" && "$INSTALL_MAIL" == "no" && "${ICSD_MAIL:-}" == "" ]]; then
  read -rp "سرور ایمیل (Postfix+Dovecot) هم نصب شود؟ سنگین است. / Install mail server too? (y/N) " a
  [[ "$a" =~ ^[Yy]$ ]] && INSTALL_MAIL="yes"
fi

# ---------- Iranian mirror option ----------
if [[ "$USE_IRAN_MIRROR" == "ask" ]]; then
  read -rp "آیا روی سرور ایران هستید و از میرور داخلی pip استفاده شود؟ (y/N) / Use Iranian pip mirror? " ans
  [[ "$ans" =~ ^[Yy]$ ]] && USE_IRAN_MIRROR="yes" || USE_IRAN_MIRROR="no"
fi
PIP_ARGS=""
if [[ "$USE_IRAN_MIRROR" == "yes" ]]; then
  PIP_ARGS="-i https://mirror-pypi.runflare.com/simple"
  log "استفاده از میرور pip ایرانی فعال شد."
fi

# ---------- Package helpers ----------
pkg_update() {
  if [[ "$PKG" == "apt" ]]; then export DEBIAN_FRONTEND=noninteractive; apt-get update -y
  else dnf makecache -y || true; fi
}
pkg_install() {
  if [[ "$PKG" == "apt" ]]; then apt-get install -y "$@"
  else dnf install -y "$@"; fi
}
svc_enable() { systemctl enable --now "$1" 2>/dev/null || warn "نتوانست $1 را فعال کند (شاید نصب نشده)"; }

# ---------- Install base prerequisites ----------
log "نصب پیش‌نیازها / Installing prerequisites..."
pkg_update
pkg_install python3 python3-pip curl tar gzip ca-certificates socat cron || \
  pkg_install python3 python3-pip curl tar gzip ca-certificates socat
[[ "$PKG" == "apt" ]] && pkg_install python3-venv || true

# ---------- Full web stack ----------
if [[ "$INSTALL_STACK" == "full" ]]; then
  log "نصب وب‌سرور Nginx..."
  pkg_install nginx && svc_enable nginx

  log "نصب PHP-FPM و افزونه‌های رایج..."
  if [[ "$PKG" == "apt" ]]; then
    pkg_install php-fpm php-cli php-mysql php-pgsql php-curl php-gd php-mbstring \
                php-xml php-zip php-intl php-bcmath || warn "برخی افزونه‌های PHP نصب نشدند"
    PHP_FPM_SVC="$(systemctl list-unit-files | grep -oE 'php[0-9.]*-fpm' | head -1 || echo php-fpm)"
  else
    pkg_install php php-fpm php-cli php-mysqlnd php-pgsql php-gd php-mbstring \
                php-xml php-bcmath php-intl || warn "برخی افزونه‌های PHP نصب نشدند"
    PHP_FPM_SVC="php-fpm"
  fi
  svc_enable "${PHP_FPM_SVC:-php-fpm}"

  if [[ "$DB_ENGINE" == "mariadb" ]]; then
    log "نصب MariaDB..."
    pkg_install mariadb-server mariadb-client 2>/dev/null || pkg_install mariadb-server mariadb || \
      warn "MariaDB نصب نشد"
    svc_enable mariadb || svc_enable mysql
    warn "بعد از نصب، حتماً 'mysql_secure_installation' را اجرا کنید."
  fi

  log "نصب PostgreSQL (اختیاری، برای پروژه‌های مدرن)..."
  pkg_install postgresql postgresql-contrib 2>/dev/null && {
    if [[ "$PKG" == "dnf" ]]; then
      [[ -d /var/lib/pgsql/data/base ]] || postgresql-setup --initdb 2>/dev/null || true
    fi
    svc_enable postgresql
  } || warn "PostgreSQL نصب نشد (می‌توانید بعداً نصب کنید)"

  log "نصب fail2ban (محافظت برابر بروت‌فورس)..."
  pkg_install fail2ban && svc_enable fail2ban || warn "fail2ban نصب نشد"

  log "نصب acme.sh (گواهی SSL لتس‌انکریپت)..."
  if [[ ! -d /root/.acme.sh ]]; then
    if curl -fsSL https://get.acme.sh -o /tmp/acme_install.sh 2>/dev/null; then
      sh /tmp/acme_install.sh -m "admin@localhost" >/dev/null 2>&1 || warn "نصب acme.sh ناموفق بود (شبکه؟)"
      rm -f /tmp/acme_install.sh
    else
      warn "دانلود acme.sh ناموفق — در شبکهٔ محدود، بعداً دستی نصب کنید."
    fi
  fi

  # default web root
  mkdir -p /var/www && chown -R "www-data":"www-data" /var/www 2>/dev/null || \
    chown -R nginx:nginx /var/www 2>/dev/null || true

  # mail server (optional, heavy)
  if [[ "$INSTALL_MAIL" == "yes" ]]; then
    log "نصب سرور ایمیل (Postfix + Dovecot)..."
    if [[ "$PKG" == "apt" ]]; then
      debconf-set-selections <<< "postfix postfix/main_mailer_type string 'Internet Site'" || true
      debconf-set-selections <<< "postfix postfix/mailname string $(hostname -f)" || true
      pkg_install postfix dovecot-core dovecot-imapd dovecot-pop3d || warn "سرور ایمیل کامل نصب نشد"
    else
      pkg_install postfix dovecot || warn "سرور ایمیل کامل نصب نشد"
    fi
    svc_enable postfix; svc_enable dovecot
    warn "پیکربندی نگاشت‌های مجازی از طریق پنل (بخش ایمیل) تولید می‌شود."
  fi
else
  warn "نصب فقط پنل (بدون وب‌استک). بعداً می‌توانید با ICSD_STACK=full دوباره اجرا کنید."
fi

# ---------- Create system user ----------
if ! id -u "$PANEL_USER" >/dev/null 2>&1; then
  log "ساخت کاربر سیستمی کم‌دسترسی / Creating low-privilege user: $PANEL_USER"
  useradd --system --create-home --shell /usr/sbin/nologin "$PANEL_USER" || \
  useradd --system --create-home --shell /sbin/nologin "$PANEL_USER"
fi

# ---------- Copy files ----------
log "کپی فایل‌ها به $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -r "$SCRIPT_DIR/backend" "$SCRIPT_DIR/frontend" "$INSTALL_DIR/"

# ---------- Python venv ----------
log "ساخت محیط مجازی پایتون و نصب وابستگی‌ها..."
python3 -m venv "$INSTALL_DIR/venv"
# shellcheck disable=SC1091
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip $PIP_ARGS
"$INSTALL_DIR/venv/bin/pip" install $PIP_ARGS -r "$INSTALL_DIR/backend/requirements.txt"

# ---------- Generate secret & .env ----------
if [[ ! -f "$INSTALL_DIR/backend/.env" ]]; then
  SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  cat > "$INSTALL_DIR/backend/.env" <<EOF
ICSD_SECRET_KEY=$SECRET
ICSD_PORT=$PORT
ICSD_DEBUG=false
EOF
fi

chown -R "$PANEL_USER":"$PANEL_USER" "$INSTALL_DIR"

# ---------- sudo rules: grant the panel the privileged commands it needs ----------
# پنل با کاربر کم‌دسترسی اجرا می‌شود؛ این قانون اجازهٔ اجرای دستورهای لازم را می‌دهد.
# مسیر هر دستور هنگام نصب resolve می‌شود تا روی توزیع‌های مختلف کار کند.
SUDO_FILE="/etc/sudoers.d/icsdpanel"
log "افزودن قوانین sudo برای پنل (مدیریت سرویس، کاربر FTP، کرون، دیتابیس، گیت، SSL)..."
SUDO_CMDS=()
for c in systemctl service nginx \
         useradd usermod userdel groupadd chpasswd \
         crontab \
         fail2ban-client \
         mysql mysqldump psql createdb dropdb pg_dump \
         git chown chmod mkdir tee \
         certbot; do
  p="$(command -v "$c" 2>/dev/null || true)"
  [[ -n "$p" ]] && SUDO_CMDS+=("$p")
done
# acme.sh installs under /root/.acme.sh
[[ -x /root/.acme.sh/acme.sh ]] && SUDO_CMDS+=("/root/.acme.sh/acme.sh")
if [[ ${#SUDO_CMDS[@]} -gt 0 ]]; then
  JOINED="$(IFS=, ; echo "${SUDO_CMDS[*]}")"   # comma-separated Cmnd list
  cat > "$SUDO_FILE" <<EOF
# ICSD Panel — privileged commands (managed by installer; do not edit by hand)
# هر دستور با هر آرگومانی قابل اجراست؛ دامنه به همین باینری‌های مدیریتی محدود است.
$PANEL_USER ALL=(root) NOPASSWD: ${JOINED//,/, }
EOF
  chmod 0440 "$SUDO_FILE"
  if visudo -cf "$SUDO_FILE" >/dev/null 2>&1; then
    log "قانون sudo نصب شد (${#SUDO_CMDS[@]} دستور مجاز)."
  else
    warn "قانون sudo نامعتبر بود؛ حذف شد. پنل بدون دسترسی کامل اجرا می‌شود."
    rm -f "$SUDO_FILE"
  fi
else
  warn "هیچ دستور مدیریتی یافت نشد؛ قانون sudo ساخته نشد."
fi

# ---------- systemd service ----------
log "نصب سرویس systemd..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=ICSD Panel - Linux server management panel
After=network.target

[Service]
Type=simple
User=$PANEL_USER
WorkingDirectory=$INSTALL_DIR/backend
EnvironmentFile=$INSTALL_DIR/backend/.env
ExecStart=$INSTALL_DIR/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

# ---------- Firewall (best effort) ----------
if command -v ufw >/dev/null 2>&1; then
  ufw allow "$PORT"/tcp || true
  [[ "$INSTALL_STACK" == "full" ]] && { ufw allow 80/tcp || true; ufw allow 443/tcp || true; }
elif command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="$PORT"/tcp || true
  if [[ "$INSTALL_STACK" == "full" ]]; then
    firewall-cmd --permanent --add-service=http || true
    firewall-cmd --permanent --add-service=https || true
  fi
  firewall-cmd --reload || true
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
log "نصب کامل شد! / Installation complete!"
echo "------------------------------------------------------------"
echo -e "  ${c_green}پنل در دسترس است:${c_reset}  http://${IP:-localhost}:$PORT"
echo "  ورود پیش‌فرض / default login:  admin / admin12345  (حتماً تغییر دهید)"
echo "  وضعیت سرویس: systemctl status $SERVICE_NAME"
echo "  لاگ‌ها:        journalctl -u $SERVICE_NAME -f"
if [[ "$INSTALL_STACK" == "full" ]]; then
  echo "  وب‌استک:       nginx + php-fpm + $DB_ENGINE + fail2ban + acme.sh نصب شد"
  [[ "$DB_ENGINE" == "mariadb" ]] && echo "  ⚠ یادآوری:     mysql_secure_installation را اجرا کنید"
  [[ "$INSTALL_MAIL" == "yes" ]] && echo "  ایمیل:         Postfix + Dovecot نصب شد (پیکربندی از پنل)"
fi
echo "------------------------------------------------------------"
