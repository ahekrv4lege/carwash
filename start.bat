@echo off
setlocal
chcp 65001 >nul
title Автомойка — бот-администратор
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run.py %*
) else (
    py -3 --version >nul 2>nul
    if errorlevel 1 (
        python run.py %*
    ) else (
        py -3 run.py %*
    )
)
set "CODE=%ERRORLEVEL%"
pause
exit /b %CODE%
