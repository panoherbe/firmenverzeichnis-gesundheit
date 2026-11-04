#!/usr/bin/env bash
# Bootstrap a fresh Hetzner Ubuntu 24.04 server as PRIMARY or STANDBY. Idempotent: safe to run again after a change.
# Normally called by ops/server/provision.sh from the Mac; by hand (as root, code already in /srv/firmen/app):
#
#   /srv/firmen/app/ops/server/bootstrap.sh primary|standby
#   /srv/firmen/app/ops/server/bootstrap.sh authorize-standby "<standby's ssh public key>"   # on the PRIMARY
#   /srv/firmen/app/ops/server/bootstrap.sh demote     # after a failback: promoted server -> standby again
#
# What it does: apt upgrade; Postgres 16 (Ubuntu package, local only), Python venv, Caddy (official apt repo),
# ufw, fail2ban, unattended-upgrades with nightly reboot window, SSH keys only, swap, journald limits, user `firmen`,
# DB role + database + schemas, systemd units/timers for the role, Caddy sites for the role, optional Floating IP.
#
# Why apt Postgres and not Docker: one fewer moving part (no daemon, no image updates, no volume), pg_dump and
# pg_restore in exactly the server's version on PATH, security updates through unattended-upgrades.
set -euo pipefail
APP=/srv/firmen/app
ROLE=${1:-}
ENVF=$APP/.env
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 2; }

getenv() { grep -E "^$1=" "$ENVF" | tail -1 | cut -d= -f2- ; }

authorize_standby() {  # PRIMARY: let the standby pull backups, read-only, nothing else
  local key=$1 rr
  rr=$(command -v rrsync || echo /usr/share/doc/rsync/scripts/rrsync)
  [ -x "$rr" ] || { echo "rrsync not found"; exit 1; }
  install -d -m 700 -o firmen -g firmen /srv/firmen/.ssh
  local line="command=\"$rr -ro /srv/firmen/backup/\",restrict $key"
  touch /srv/firmen/.ssh/authorized_keys
  grep -qF "$key" /srv/firmen/.ssh/authorized_keys || echo "$line" >> /srv/firmen/.ssh/authorized_keys
  chown firmen:firmen /srv/firmen/.ssh/authorized_keys; chmod 600 /srv/firmen/.ssh/authorized_keys
  echo "standby key authorized (read-only rsync of /srv/firmen/backup)"
}

if [ "$ROLE" = authorize-standby ]; then authorize_standby "${2:?public key}"; exit 0; fi
if [ "$ROLE" = demote ]; then  # a promoted standby (or an old primary) goes back to standby duty: explicit, never implicit
  rm -f "$APP/var/ops/PROMOTED"
  systemctl disable --now firmen-app.service firmen-app-health.timer firmen-backup.timer firmen-restore-test.timer \
    firmen-refresh-monthly.timer firmen-refresh-quarterly.timer firmen-prune.timer firmen-monitor-watchdog.timer || true
  rm -f /etc/caddy/sites-enabled/app.caddy; ln -sf /etc/caddy/sites-available/status.caddy /etc/caddy/sites-enabled/
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy
  systemctl enable --now firmen-monitor.timer firmen-backup-pull.timer firmen-standby-restore.timer firmen-sla-report.timer
  echo "demoted to standby"; exit 0
fi
case "$ROLE" in primary|standby) ;; *) sed -n '2,12p' "$0"; exit 2 ;; esac
[ -f "$ENVF" ] || { echo "$ENVF missing (provision.sh copies .env.$ROLE there)"; exit 2; }
grep -q CHANGE_ME "$ENVF" && { echo "$ENVF still contains CHANGE_ME placeholders"; exit 2; }
DOMAIN=$(getenv DOMAIN); STATUS_DOMAIN=$(getenv STATUS_DOMAIN); ACME_EMAIL=$(getenv ACME_EMAIL)
PGPW=$(getenv PGPASSWORD_FIRMEN); FIP=$(getenv FLOATING_IP); ADMIN_CIDR=$(getenv ADMIN_SSH_CIDR)
PEER=$(getenv PEER_IP); REBOOT=$(getenv REBOOT_TIME); REBOOT=${REBOOT:-04:40}

echo "=== 0. SSH safety: refuse to disable passwords if no key is installed"
[ -s /root/.ssh/authorized_keys ] || { echo "no /root/.ssh/authorized_keys — add your key in Hetzner first"; exit 2; }

echo "=== 1. packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get -y -q -o Dpkg::Options::=--force-confold upgrade
apt-get install -y -q postgresql-16 postgresql-client-16 python3-venv python3-pip ufw fail2ban python3-systemd \
  unattended-upgrades rsync curl jq sqlite3 debian-keyring debian-archive-keyring apt-transport-https gnupg
if ! command -v caddy >/dev/null; then  # official Caddy repository (https://caddyserver.com/docs/install#debian-ubuntu-raspbian)
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi
timedatectl set-timezone Europe/Berlin

echo "=== 2. user, folders"
id firmen >/dev/null 2>&1 || useradd --system --home-dir /srv/firmen --shell /bin/bash firmen
usermod -aG systemd-journal firmen
install -d -m 755 -o firmen -g firmen /srv/firmen "$APP/var" "$APP/var/status"
install -d -m 750 -o firmen -g firmen "$APP/var/log" "$APP/var/ops" "$APP/var/monitor" "$APP/var/reports" /srv/firmen/raw
install -d -m 700 -o firmen -g firmen /srv/firmen/backup /srv/firmen/.ssh
chown -R firmen:firmen "$APP"; chmod 600 "$ENVF"
chmod 755 "$APP"   # Caddy (user caddy) must traverse to var/status on the standby; .env stays 0600

echo "=== 3. swap (2 GB) + journald"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
install -d /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=1G\nMaxRetentionSec=90day\n' > /etc/systemd/journald.conf.d/firmen.conf
systemctl restart systemd-journald

echo "=== 4. postgres"
cat > /etc/postgresql/16/main/conf.d/firmen.conf <<'EOF'
listen_addresses = 'localhost'
shared_buffers = 1GB
effective_cache_size = 3GB
work_mem = 32MB
maintenance_work_mem = 256MB
log_min_duration_statement = 2000
EOF
systemctl enable --now postgresql
systemctl restart postgresql
runuser -u postgres -- psql -Xq -v ON_ERROR_STOP=1 -v pw="$PGPW" <<'EOF'
SELECT format('CREATE ROLE firmen LOGIN CREATEDB PASSWORD %L', :'pw') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'firmen') \gexec
SELECT format('ALTER ROLE firmen LOGIN CREATEDB PASSWORD %L', :'pw') \gexec
SELECT 'CREATE DATABASE firmen OWNER firmen' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'firmen') \gexec
EOF

echo "=== 5. python venv + schemas"
runuser -u firmen -- python3 -m venv "$APP/.venv"
runuser -u firmen -- "$APP/.venv/bin/pip" install -q --upgrade pip
runuser -u firmen -- "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt" python-multipart
cd "$APP"
runuser -u firmen -- .venv/bin/python -m ops.schema   # db/schema.sql + enrich columns + auth/support/matchmaking

echo "=== 6. ssh: keys only"
cat > /etc/ssh/sshd_config.d/10-firmen.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 30
EOF
sshd -t && systemctl reload ssh

echo "=== 7. firewall + fail2ban"
ufw default deny incoming
ufw default allow outgoing
if [ -n "$ADMIN_CIDR" ]; then ufw allow from "$ADMIN_CIDR" to any port 22 proto tcp; else ufw allow 22/tcp; fi
[ -n "$PEER" ] && ufw allow from "$PEER" to any port 22 proto tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
cat > /etc/fail2ban/jail.d/firmen.local <<'EOF'
[DEFAULT]
backend = systemd
bantime = 1h
bantime.increment = true
findtime = 10m
maxretry = 5

[sshd]
enabled = true
mode = aggressive
EOF
systemctl enable --now fail2ban && systemctl restart fail2ban

echo "=== 8. unattended-upgrades (security + updates, reboot at $REBOOT if needed)"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
cat > /etc/apt/apt.conf.d/52firmen-unattended <<EOF
Unattended-Upgrade::Origins-Pattern { "origin=Ubuntu,archive=\${distro_codename}-security"; "origin=Ubuntu,archive=\${distro_codename}-updates"; "origin=cloudsmith/caddy/stable"; };
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "$REBOOT";
EOF

echo "=== 9. floating IP (optional)"
if [ -n "$FIP" ]; then
  cat > /etc/netplan/60-floating-ip.yaml <<EOF
network:
  version: 2
  ethernets:
    eth0:
      addresses:
        - $FIP/32
EOF
  chmod 600 /etc/netplan/60-floating-ip.yaml && netplan apply
fi

echo "=== 10. caddy"
install -d /etc/caddy/sites-available /etc/caddy/sites-enabled
sed "s|__ACME_EMAIL__|$ACME_EMAIL|g" "$APP/ops/server/Caddyfile" > /etc/caddy/Caddyfile
for s in app status; do
  sed -e "s|__DOMAIN__|$DOMAIN|g" -e "s|__STATUS_DOMAIN__|$STATUS_DOMAIN|g" "$APP/ops/server/$s.caddy" > "/etc/caddy/sites-available/$s.caddy"
done
rm -f /etc/caddy/sites-enabled/*.caddy
if [ "$ROLE" = primary ] || [ -e "$APP/var/ops/PROMOTED" ]; then ln -sf /etc/caddy/sites-available/app.caddy /etc/caddy/sites-enabled/; fi
[ "$ROLE" = standby ] && ln -sf /etc/caddy/sites-available/status.caddy /etc/caddy/sites-enabled/
install -d -o caddy -g caddy /var/log/caddy
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
chown -R caddy:caddy /var/log/caddy  # validate runs as root and creates access.log as root (trap, 2026-11-04)
systemctl enable caddy && systemctl reload-or-restart caddy

echo "=== 11. systemd units"
cp "$APP"/ops/systemd/firmen-*.service "$APP"/ops/systemd/firmen-*.timer /etc/systemd/system/
systemctl daemon-reload
PRIMARY_UNITS="firmen-app.service firmen-app-health.timer firmen-backup.timer firmen-restore-test.timer firmen-refresh-monthly.timer firmen-refresh-quarterly.timer firmen-prune.timer firmen-monitor-watchdog.timer"
STANDBY_UNITS="firmen-monitor.timer firmen-backup-pull.timer firmen-standby-restore.timer firmen-sla-report.timer"
if [ "$ROLE" = primary ]; then
  # shellcheck disable=SC2086
  systemctl disable --now $STANDBY_UNITS 2>/dev/null || true
  # shellcheck disable=SC2086
  systemctl enable --now $PRIMARY_UNITS
else
  # shellcheck disable=SC2086
  [ -e "$APP/var/ops/PROMOTED" ] || systemctl disable --now $PRIMARY_UNITS 2>/dev/null || true
  # shellcheck disable=SC2086
  systemctl enable --now $STANDBY_UNITS
  [ -f /srv/firmen/.ssh/id_ed25519 ] || runuser -u firmen -- ssh-keygen -q -t ed25519 -N '' -C "firmen-standby" -f /srv/firmen/.ssh/id_ed25519
  echo
  echo ">>> On the PRIMARY run:  /srv/firmen/app/ops/server/bootstrap.sh authorize-standby \"$(cat /srv/firmen/.ssh/id_ed25519.pub)\""
  echo ">>> Storage Box: add the same public key (Hetzner Robot -> Storage Box -> SSH keys, or"
  echo "    'cat /srv/firmen/.ssh/id_ed25519.pub | ssh -p23 uXXXXX@uXXXXX.your-storagebox.de install-ssh-key')"
fi

echo "=== done ($ROLE). Check: systemctl list-timers 'firmen-*'; curl -s http://127.0.0.1:8601/api/health"
