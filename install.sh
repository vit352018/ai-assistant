#!/usr/bin/env bash
# Установка/обновление помощника. Запускать от root. Перед этим должен существовать /opt/assistant/.env
set -euo pipefail
APP=/opt/assistant

[ "$(id -u)" = 0 ] || { echo "Запустите от root"; exit 1; }
[ -f "$APP/.env" ] || { echo "Нет файла $APP/.env"; exit 1; }

apt-get update -qq
apt-get install -y -qq python3-venv >/dev/null
id assistant >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin assistant

python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --no-cache-dir -r "$APP/requirements.txt"

chown -R assistant:assistant "$APP"
chmod 600 "$APP/.env"

cat > /etc/systemd/system/assistant.service <<UNIT
[Unit]
Description=Personal AI assistant (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
User=assistant
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=$APP/venv/bin/python -m app.bot
Restart=always
RestartSec=5
MemoryMax=350M
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable assistant >/dev/null 2>&1
systemctl restart assistant
sleep 4
echo "Состояние: $(systemctl is-active assistant)"
journalctl -u assistant -n 12 --no-pager
