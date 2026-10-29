#!/usr/bin/env bash
# One-shot installer for a Debian/Ubuntu server.  Run as root from the extracted bundle folder:
#   sudo bash deploy/install_linux.sh
# It never touches anything outside /opt/tiktok-guard (+ the systemd unit and a dedicated user).
set -euo pipefail

APP_DIR=/opt/tiktok-guard
BUNDLE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "== 1/6 system packages =="
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl ca-certificates fonts-noto-color-emoji fonts-liberation
# Brave (the browser the saved profiles belong to):
if ! command -v brave-browser >/dev/null 2>&1; then
  curl -fsSLo /usr/share/keyrings/brave-browser-archive-keyring.gpg \
       https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/brave-browser-archive-keyring.gpg] https://brave-browser-apt-release.s3.brave.com/ stable main" \
       > /etc/apt/sources.list.d/brave-browser-release.list
  apt-get update -y && apt-get install -y brave-browser
fi

echo "== 2/6 user + folders =="
id tiktokguard >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin tiktokguard
mkdir -p "$APP_DIR"
cp -a "$BUNDLE_DIR"/. "$APP_DIR"/
chown -R tiktokguard:tiktokguard "$APP_DIR"
chmod 700 "$APP_DIR" "$APP_DIR/profiles" 2>/dev/null || true

echo "== 3/6 python environment (no browser download) =="
sudo -u tiktokguard python3 -m venv "$APP_DIR/venv"
sudo -u tiktokguard env PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "== 4/6 .env =="
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.template" "$APP_DIR/.env"
  sed -i "s#^BFP_PATCHED_CHROMIUM=.*#BFP_PATCHED_CHROMIUM=$(command -v brave-browser)#" "$APP_DIR/.env"
  chown tiktokguard:tiktokguard "$APP_DIR/.env"; chmod 600 "$APP_DIR/.env"
  echo ">> EDIT $APP_DIR/.env now: OPENROUTER_API_KEY and TELEGRAM_BOT_TOKEN (then re-run this script's steps 5-6)."
  exit 0
fi

echo "== 5/6 rebuild the profiles from the bundle (TikTok-only state) =="
cd "$APP_DIR"
sudo -u tiktokguard env $(grep -v '^#' .env | xargs) "$APP_DIR/venv/bin/python" run.py restore-profiles --bundle "$APP_DIR"
echo ">> add the printed BFP_USER_DATA_DIR=... line to $APP_DIR/.env if it is not there yet."

echo "== 6/6 service =="
cp "$APP_DIR/deploy/tiktok-guard.service" /etc/systemd/system/tiktok-guard.service
systemctl daemon-reload
systemctl enable --now tiktok-guard
systemctl --no-pager status tiktok-guard | head -12
