@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run.py %*
    exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 run.py %*
    exit /b
)
where python >nul 2>nul
if errorlevel 1 (
    echo Python 3.10+ не найден. Установите Python с python.org и включите Add Python to PATH.
    exit /b 1
)
python run.py %*
exit /b
