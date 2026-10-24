#!/usr/bin/env bash
# Запуск бота на Linux/macOS. Подготовкой окружения управляет run.py.
set -eu
cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
if [ -x ".venv/bin/python" ]; then
    PY=.venv/bin/python
fi
exec "$PY" run.py "$@"
