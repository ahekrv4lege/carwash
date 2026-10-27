"""Фоновый запуск и автозапуск для одного Telegram-бота."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


def _run(arguments, *, check=True):
    return subprocess.run(arguments, check=check)


def _quoted(value):
    value = str(value)
    if "\n" in value or "\r" in value:
        raise RuntimeError("Путь или настройка автозапуска содержит перевод строки")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def linux_unit(root: Path, proxy_environment: dict, profile: str | None = None) -> str:
    directory = str(root).replace("%", "%%")
    if "\n" in directory or "\r" in directory:
        raise RuntimeError("Путь автозапуска содержит перевод строки")
    environment = "\n".join("Environment=" + _quoted(key + "=" + value)
                            for key, value in proxy_environment.items())
    profile_args = " --profile " + _quoted(profile) if profile else ""
    return f"""[Unit]
Description=Carwash Telegram bot
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={directory}
ExecStart={_quoted(root / '.venv/bin/python')} {_quoted(root / 'run.py')} --no-install{profile_args}
Environment=PYTHONUNBUFFERED=1
{environment}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=control-group
UMask=0077

[Install]
WantedBy=default.target
"""


def windows_task(root: Path, username: str, profile: str | None = None) -> str:
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)
    def add(parent, tag, value=None):
        element = ET.SubElement(parent, "{" + namespace + "}" + tag)
        element.text = value
        return element
    task = ET.Element("{" + namespace + "}Task", version="1.2")
    trigger = add(add(task, "Triggers"), "LogonTrigger")
    add(trigger, "UserId", username)
    add(trigger, "Enabled", "true")
    principal = add(add(task, "Principals"), "Principal")
    principal.set("id", "Author")
    add(principal, "UserId", username)
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(task, "Settings")
    for key, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                       ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "true"),
                       ("ExecutionTimeLimit", "PT0S")):
        add(settings, key, value)
    restart = add(settings, "RestartOnFailure")
    add(restart, "Interval", "PT1M")
    add(restart, "Count", "999")
    actions = add(task, "Actions")
    actions.set("Context", "Author")
    action = add(actions, "Exec")
    add(action, "Command", str(root / ".venv/Scripts/pythonw.exe"))
    add(action, "Arguments", subprocess.list2cmdline([str(root / "run.py"), "--no-install", "--host-worker", *(["--profile", profile] if profile else [])]))
    add(action, "WorkingDirectory", str(root))
    return ET.tostring(task, encoding="unicode", xml_declaration=True)


def manage(action: str, root: Path, bot_id: str, *, profile: str | None = None) -> int:
    if not re.fullmatch(r"\d+", bot_id):
        raise RuntimeError("Для автозапуска нужен корректный BOT_TOKEN")
    name = "carwash-" + bot_id
    if sys.platform.startswith("linux"):
        if not shutil.which("systemctl"):
            raise RuntimeError("Для фонового хостинга нужен systemd; обычный запуск: ./start.sh")
        unit = name + ".service"
        if action == "host":
            folder = Path.home() / ".config/systemd/user"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / unit
            accepted = {"WorkingDirectory=" + str(root).replace("%", "%%"), "WorkingDirectory=" + _quoted(root)}
            if path.exists() and not accepted.intersection(path.read_text().splitlines()):
                raise RuntimeError("Автозапуск этого бота уже привязан к другой папке; сначала остановите прежнюю копию")
            variables = {key: value for key, value in os.environ.items()
                         if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
            path.write_text(linux_unit(root, variables, profile), encoding="utf-8")
            path.chmod(0o600)
            if shutil.which("systemd-analyze"):
                _run(["systemd-analyze", "--user", "verify", str(path)])
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", unit])
            _run(["systemctl", "--user", "restart", unit])
            _run(["systemctl", "--user", "is-active", unit])
            print("Фоновая служба включена. Автозапуск — при входе в систему.")
            print("Состояние: ./start.sh --status; журнал: ./start.sh --logs; остановка: ./start.sh --stop")
            return 0
        if action == "stop":
            return _run(["systemctl", "--user", "disable", "--now", unit], check=False).returncode
        if action == "logs":
            return _run(["journalctl", "--user", "-u", unit, "-n", "60", "--no-pager"], check=False).returncode
        return _run(["systemctl", "--user", "status", unit, "--no-pager"], check=False).returncode
    if os.name == "nt":
        if action == "host":
            username = subprocess.check_output(["whoami"], text=True).strip()
            folder = root / ".runtime"
            folder.mkdir(exist_ok=True)
            path = folder / "host-task.xml"
            path.write_text(windows_task(root, username, profile), encoding="utf-16")
            _run(["schtasks", "/Create", "/TN", name, "/XML", str(path), "/F"])
            _run(["schtasks", "/Run", "/TN", name])
            print("Задача запущена в фоне и будет запускаться при входе в Windows.")
            print("Журнал: start.bat --logs; остановка: start.bat --stop")
            return 0
        if action == "stop":
            _run(["schtasks", "/End", "/TN", name], check=False)
            return _run(["schtasks", "/Change", "/TN", name, "/DISABLE"], check=False).returncode
        if action == "logs":
            path = root / ".runtime/host.log"
            print("\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]) if path.exists() else "Журнал ещё не создан")
            return 0
        return _run(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"], check=False).returncode
    raise RuntimeError("Фоновый хостинг поддержан для Linux с systemd и Windows; обычный запуск: python run.py")
