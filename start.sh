#!/usr/bin/env bash
set -eu
cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python run.py "$@"
fi
if command -v python3 >/dev/null 2>&1; then
    exec python3 run.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python run.py "$@"
fi
printf '%s\n' 'Python 3.10+ не найден. Установите Python средствами своего дистрибутива.' >&2
exit 1
