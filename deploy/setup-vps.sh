#!/usr/bin/env bash
# Настройка Carwash на свежем VPS (Debian 13, 1 vCPU, ~1 GB RAM, 4 GB swap).
#
# Использование (от root, из клона репозитория):
#   bash deploy/setup-vps.sh
#
# Перед запуском вручную положите секреты в /root/carwash:
#   .env               — токен бота и настройки AI
#   credentials.json   — сервисный аккаунт Google
# Скрипт не содержит и не создаёт секретов.
set -euo pipefail

APP_DIR="/root/carwash"
SERVICE_NAME="carwash-8377993064.service"   # id бота из BOT_TOKEN до ":"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> 1/8 Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv git >/dev/null

echo "==> 2/8 Код приложения"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch origin dev
    git -C "$APP_DIR" reset --hard origin/dev
else
    git clone -b dev https://github.com/ahekrv4lege/carwash.git "$APP_DIR"
fi
[ -f "$APP_DIR/.env" ]            || { echo "Положите $APP_DIR/.env и повторите"; exit 1; }
[ -f "$APP_DIR/credentials.json" ] || { echo "Положите $APP_DIR/credentials.json и повторите"; exit 1; }

echo "==> 3/8 Виртуальное окружение и зависимости (run.py делает это сам при старте)"
"$APP_DIR/run.py" --no-install >/dev/null 2>&1 || true

echo "==> 4/8 Sysctl: значения бывшего профиля tuned virtual-guest (статично)"
cat > /etc/sysctl.d/99-carwash.conf <<'EOF'
vm.swappiness = 30
vm.dirty_ratio = 30
net.core.somaxconn = 4096
kernel.numa_balancing = 0
EOF
sysctl -p /etc/sysctl.d/99-carwash.conf >/dev/null

echo "==> 5/8 Readahead диска (udev, вместо tuned)"
cat > /etc/udev/rules.d/99-carwash-readahead.rules <<'EOF'
ACTION=="add|change", KERNEL=="vda", ATTR{queue/read_ahead_kb}="4096"
EOF
udevadm control --reload-rules
blockdev --setra 8192 /dev/vda 2>/dev/null || true

echo "==> 6/8 Отключение лишних демонов (tuned ~27 МБ, swapspace дублирует /swapfile)"
systemctl disable --now tuned swapspace 2>/dev/null || true
sysctl -p /etc/sysctl.d/99-carwash.conf >/dev/null   # tuned при остановке откатывает значения

echo "==> 7/8 Journald: лимит на маленький SSD"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/99-carwash.conf <<'EOF'
[Journal]
SystemMaxUse=200M
EOF
systemctl restart systemd-journald 2>/dev/null || systemctl kill --signal=SIGUSR2 systemd-journald 2>/dev/null || true

echo "==> 8/8 Юнит systemd (пользовательский, root) и запуск"
mkdir -p /root/.config/systemd/user "$APP_DIR/.runtime"
cat > "/root/.config/systemd/user/$SERVICE_NAME" <<EOF
[Unit]
Description=Carwash Telegram bot
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart="$APP_DIR/.venv/bin/python" "$APP_DIR/run.py" --no-install
Environment=PYTHONUNBUFFERED=1

Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=control-group
UMask=0077

[Install]
WantedBy=default.target
EOF
mkdir -p "/root/.config/systemd/user/$SERVICE_NAME.d"
cat > "/root/.config/systemd/user/$SERVICE_NAME.d/tuning.conf" <<'EOF'
# Оптимизация для 1 vCPU / 1 GB RAM:
# - MemoryHigh: мягкий предел, при утечке бот тормозится раньше, чем система умирает
# - OOMScoreAdjust: при жёсткой нехватке памяти уничтожается сначала бот, а не sshd/journald
# - MALLOC_ARENA_MAX: меньше арена glibc (VSZ растёт медленнее, фрагментация ниже)
[Service]
MemoryHigh=350M
OOMScoreAdjust=500
Environment=MALLOC_ARENA_MAX=2
EOF
loginctl show-user root | grep -qi "Linger=yes" && loginctl enable-linger root
systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"
sleep 8
systemctl --user --no-pager status "$SERVICE_NAME" | head -5
journalctl --user -u "$SERVICE_NAME" --since "1 minute ago" --no-pager | tail -5

echo "Готово. Проверка: systemctl --user status $SERVICE_NAME"
