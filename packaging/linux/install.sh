#!/usr/bin/env bash
# CyberYinYu Linux installer.
# Usage: sudo bash install.sh [--start]
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/cyberyinyu}"
SERVICE_NAME="${SERVICE_NAME:-cyberyinyu}"
PYTHON="${PYTHON:-python3}"
START=0
[ "${1:-}" = "--start" ] && START=1

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ] && [ "$APP_DIR" = "/opt/cyberyinyu" ]; then
  echo "请用 root 运行，或指定可写的 APP_DIR，例如：" >&2
  echo "  APP_DIR=\"$HOME/cyberyinyu\" bash install.sh" >&2
  exit 1
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "找不到 $PYTHON，请先安装 Python 3.10+。" >&2
  exit 1
fi

"$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("需要 Python 3.10 或更高版本，当前为 %s" % sys.version.split()[0])
PY

echo "[1/5] 复制文件到 $APP_DIR"
mkdir -p "$APP_DIR"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  tar -C "$SRC_DIR" \
      --exclude='.git' --exclude='__pycache__' --exclude='dist' \
      --exclude='otowa.env' --exclude='logs' --exclude='data' \
      -cf - . | tar -C "$APP_DIR" -xf -
fi
mkdir -p "$APP_DIR/logs" "$APP_DIR/data"

echo "[2/5] 创建虚拟环境"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  "$PYTHON" -m venv "$APP_DIR/.venv" || {
    echo "创建 venv 失败。Debian/Ubuntu 可能需要：apt install python3-venv" >&2
    exit 1
  }
fi
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

echo "[3/5] 初始化配置"
if [ ! -f "$APP_DIR/otowa.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/otowa.env"
  chmod 600 "$APP_DIR/otowa.env"
  echo "已生成 $APP_DIR/otowa.env，请填入 BOT_QQ / NAPCAT_TOKEN / AI_API_KEY / OWNER_QQ。"
else
  echo "保留现有 $APP_DIR/otowa.env"
fi

echo "[4/5] 安装 systemd 服务"
UNIT_SRC="$SRC_DIR/cyberyinyu.service"
[ -f "$UNIT_SRC" ] || UNIT_SRC="$SRC_DIR/packaging/linux/cyberyinyu.service"
sed "s|__APP_DIR__|$APP_DIR|g" "$UNIT_SRC" > "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo "[5/5] 完成"
echo
echo "下一步："
echo "  1. sudoedit $APP_DIR/otowa.env"
echo "  2. 在 NapCat 中配置 HTTP 3000 与反向 WS ws://127.0.0.1:8080/onebot/v11/ws"
echo "  3. sudo systemctl restart $SERVICE_NAME"
echo "  4. curl http://127.0.0.1:8080/health"
echo
if [ "$START" = "1" ]; then
  systemctl restart "$SERVICE_NAME"
  systemctl --no-pager --full status "$SERVICE_NAME" || true
fi
