@echo off
rem Запуск бота-администратора автомойки на Windows:
rem создаёт виртуальное окружение .venv, ставит зависимости и запускает run.py.

chcp 65001 >nul
title Автомойка — бот-администратор
cd /d "%~dp0"

set "PY=py -3"
%PY% --version >nul 2>nul || set "PY=python"

if not exist ".venv\Scripts\python.exe" (
    echo Создаю виртуальное окружение .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo Python не найден. Установите Python 3.10+ с https://www.python.org/downloads/
        echo и запустите этот файл ещё раз.
        pause
        exit /b 1
    )
)

echo Проверяю зависимости...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 echo ВНИМАНИЕ: не удалось обновить зависимости, пробую запустить с уже установленными.

".venv\Scripts\python.exe" run.py
pause
