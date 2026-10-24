from contextlib import contextmanager
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import run
from config import load_config
from tests.fakes import config

ROOT = Path(__file__).resolve().parents[1]


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = config(self.root)

    def credentials(self):
        path = self.root / "credentials.json"
        path.write_text(json.dumps({"type": "service_account", "client_email": "synthetic@example.invalid",
                                    "private_key": "synthetic-unused", "token_uri": "https://example.invalid/token"}))
        (self.root / ".env").write_text("")
        return replace(self.cfg, google_credentials_file=str(path))

    def test_missing_env_error(self):
        with patch.object(run, "ROOT", self.root), self.assertRaisesRegex(run.StartupError, "Нет .env"):
            run.validate_config()

    def test_missing_required_value_names_variable_without_token(self):
        (self.root / ".env").write_text("")
        with patch.object(run, "ROOT", self.root), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(run.StartupError, "BOT_TOKEN"):
                run.validate_config()

    def test_missing_credentials(self):
        (self.root / ".env").write_text("")
        with patch.object(run, "ROOT", self.root), patch("config.load_config", return_value=self.cfg):
            with self.assertRaisesRegex(run.StartupError, "GOOGLE_CREDENTIALS_FILE"):
                run.validate_config()

    def test_invalid_credentials_json(self):
        cfg = self.credentials()
        Path(cfg.google_credentials_file).write_text("invalid")
        with patch.object(run, "ROOT", self.root), patch("config.load_config", return_value=cfg):
            with self.assertRaisesRegex(run.StartupError, "JSON"):
                run.validate_config()

    def test_valid_config_is_offline(self):
        cfg = self.credentials()
        with patch.object(run, "ROOT", self.root), patch("config.load_config", return_value=cfg):
            self.assertEqual(run.validate_config(), cfg)

    def test_invalid_timezone(self):
        cfg = replace(self.credentials(), timezone="Invalid/Zone")
        with patch.object(run, "ROOT", self.root), patch("config.load_config", return_value=cfg):
            with self.assertRaisesRegex(run.StartupError, "TIMEZONE"):
                run.validate_config()

    def test_bot_guard_requires_matching_id_and_confirmation(self):
        with self.assertRaisesRegex(run.StartupError, "LOCAL_BOT_ID"):
            run.validate_local_bot(self.cfg)
        cfg = replace(self.cfg, local_bot_id="123456")
        with self.assertRaisesRegex(run.StartupError, "BOT_START_CONFIRMED"):
            run.validate_local_bot(cfg)
        run.validate_local_bot(replace(cfg, bot_start_confirmed=True))

    def test_invalid_flags_and_intervals(self):
        for var, value in [("SHEETS_ADAPTIVE_ENABLED", "perhaps"), ("SHEETS_SCHEMA_CHECK_INTERVAL_HOURS", "nan"),
                           ("SHEETS_SCHEMA_CHECK_INTERVAL_HOURS", "-1"), ("SLOTS_CACHE_SEC", "bad")]:
            with self.subTest(var=var, value=value), patch.dict(os.environ, {"SPREADSHEET_ID": "test", var: value}):
                with self.assertRaisesRegex(RuntimeError, var):
                    load_config(sheets_only=True)

    def test_missing_project_file_before_environment_creation(self):
        with patch.object(run, "ROOT", self.root), self.assertRaisesRegex(run.StartupError, "bot.py"):
            run.prepare_environment()
        self.assertFalse((self.root / ".venv").exists())

    def test_missing_venv_with_install_disabled(self):
        for name in ("bot.py", "config.py", "requirements.txt"):
            (self.root / name).write_text("")
        with patch.object(run, "ROOT", self.root), self.assertRaisesRegex(run.StartupError, "Нет .venv"):
            run.prepare_environment(no_install=True)

    def test_duplicate_lock_and_release(self):
        path = self.root / "instance.lock"
        code = "from pathlib import Path; from run import InstanceLock; import sys; " \
               "lock=InstanceLock(Path(sys.argv[1])); lock.__enter__()"
        with run.InstanceLock(path):
            proc = subprocess.run([sys.executable, "-c", code, str(path)], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("локальный экземпляр", proc.stderr)
        with run.InstanceLock(path):
            pass

    def test_lock_released_when_process_terminates(self):
        path = self.root / "process.lock"
        code = "from pathlib import Path; from run import InstanceLock; import sys,time; " \
               "lock=InstanceLock(Path(sys.argv[1])); lock.__enter__(); print('ready',flush=True); time.sleep(30)"
        proc = subprocess.Popen([sys.executable, "-c", code, str(path)], cwd=ROOT, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            with self.assertRaises(run.StartupError):
                with run.InstanceLock(path):
                    pass
            proc.terminate()
            proc.wait(timeout=5)
            # В venv на Windows python.exe — транпмолин: terminate() убивает его,
            # а процесс-наследник, держащий блокировку, завершается чуть позже.
            # Даём блокировке освободиться, но не дольше 5 секунд.
            deadline = time.monotonic() + 5
            while True:
                try:
                    with run.InstanceLock(path):
                        break
                except run.StartupError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()

    @unittest.skipUnless(os.name == "posix", "Linux launcher")
    def test_linux_launcher_from_other_working_directory_and_venv_reuse(self):
        stamp = ROOT / ".venv/.requirements.sha256"
        before = stamp.stat().st_mtime_ns
        proc = subprocess.run([str(ROOT / "start.sh"), "--setup-only", "--no-install"], cwd=self.root,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stamp.stat().st_mtime_ns, before)
        self.assertNotIn("Устанавливаю", proc.stdout)

    @unittest.skipUnless(os.name == "posix", "Linux signal handling")
    def test_real_aiogram_polling_graceful_sigterm_without_network(self):
        # Настоящий Dispatcher и Bot; заменён только сетевой транспорт Telegram.
        script = ROOT / "tests/polling_fixture.py"
        marker = self.root / "closed"
        proc = subprocess.Popen([sys.executable, str(script), str(marker)], cwd=self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline().strip()
            self.assertEqual(line, "polling-ready")
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err)
            self.assertTrue(marker.exists())
            self.assertIn("SIGTERM", err)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
