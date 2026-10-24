#!/usr/bin/env bash
# Запуск бота-администратора автомойки на Linux/macOS:
# создаёт виртуальное окружение .venv, ставит зависимости и запускает run.py.
# Остановка — Ctrl+C.
set -eu
cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python

if [ ! -x ".venv/bin/python" ]; then
    echo "Создаю виртуальное окружение .venv ..."
    if ! "$PY" -m venv .venv; then
        echo
        echo "Python не найден. Установите Python 3.10+ (например, sudo apt install python3 python3-venv)"
        echo "и запустите этот файл ещё раз."
        exit 1
    fi
fi

echo "Проверяю зависимости..."
".venv/bin/python" -m pip install --quiet --disable-pip-version-check -r requirements.txt \
    || echo "ВНИМАНИЕ: не удалось обновить зависимости, пробую запустить с уже установленными."

exec ".venv/bin/python" run.py "$@"
