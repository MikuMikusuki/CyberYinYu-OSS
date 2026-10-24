#!/usr/bin/env bash
# Run CyberYinYu in the foreground (useful without systemd).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if [ ! -x ".venv/bin/python" ]; then
  echo "找不到 .venv，请先运行 install.sh。" >&2
  exit 1
fi
exec .venv/bin/python bot.py
