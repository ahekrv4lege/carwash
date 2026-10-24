"""Linux execution and explicitly simulated Windows branches."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import run
from config import load_config

ROOT = Path(__file__).resolve().parents[1]


class AuditStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="carwash audit ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @unittest.skipUnless(os.name == "posix", "Actual Linux execution")
    def test_direct_bot_script_bootstraps_before_third_party_imports(self):
        # -S removes site packages from the initial interpreter. Reexec must use .venv.
        proc = subprocess.run([getattr(sys, "_base_executable", sys.executable), "-S", str(ROOT / "bot.py"), "--setup-only", "--no-install"],
                              cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("готовы", proc.stdout)

    def test_integer_configuration_does_not_silently_truncate(self):
        with patch.dict(os.environ, {"SPREADSHEET_ID": "synthetic", "MAX_HISTORY": "1.5"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MAX_HISTORY"):
                load_config(sheets_only=True)

    def test_invalid_admin_id_is_reported(self):
        with patch.dict(os.environ, {"SPREADSHEET_ID": "synthetic", "ADMIN_CHAT_ID": "mistyped"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ADMIN_CHAT_ID"):
                load_config(sheets_only=True)

    def test_duplicate_sheet_role_names_are_rejected(self):
        with patch.dict(os.environ, {"SPREADSHEET_ID": "synthetic", "SHEET_BOOKINGS": "Слоты"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SHEET_"):
                load_config(sheets_only=True)

    def test_relative_absolute_and_home_paths(self):
        # Домашние переменные сохраняем: на Windows без USERPROFILE/HOMEDRIVE
        # expanduser('~') не работает, и тест падает не по своей вине.
        home = {k: os.environ[k] for k in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH") if k in os.environ}
        for name in ("relative.json", str(self.root / "absolute.json"), "~/synthetic.json"):
            with self.subTest(name=name), patch.dict(os.environ, {**home, "SPREADSHEET_ID": "synthetic",
                    "GOOGLE_CREDENTIALS_FILE": name, "SHEETS_MAPPING_FILE": name}, clear=True):
                cfg = load_config(sheets_only=True)
                expected = Path(name).expanduser()
                if not expected.is_absolute():
                    expected = ROOT / expected
                self.assertEqual(Path(cfg.google_credentials_file), expected)
                self.assertEqual(Path(cfg.sheets_mapping_file), expected)

    def test_failed_install_does_not_leave_success_stamp(self):
        for name in ("bot.py", "config.py", "requirements.txt"):
            (self.root / name).write_text("")
        env = self.root / ".venv"
        env.mkdir()
        with patch.object(run, "ROOT", self.root), patch.object(sys, "prefix", str(env)), \
             patch.object(run, "has_module", return_value=False), \
             patch.object(run.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["pip"])):
            with self.assertRaises(subprocess.CalledProcessError):
                run.prepare_environment()
        self.assertFalse((env / ".requirements.sha256").exists())

    def test_dependency_signature_change_reinstalls_then_reuses(self):
        for name in ("bot.py", "config.py", "requirements.txt"):
            (self.root / name).write_text("")
        env = self.root / ".venv"
        env.mkdir()
        stamp = env / ".requirements.sha256"
        stamp.write_text("old signature")
        with patch.object(run, "ROOT", self.root), patch.object(sys, "prefix", str(env)), \
             patch.object(run, "has_module", return_value=True), patch.object(run.subprocess, "run") as installer:
            run.prepare_environment()
            self.assertEqual(installer.call_count, 2)
            self.assertEqual(installer.call_args.args[0][-1], "check")
            installer.reset_mock()
            run.prepare_environment()
            installer.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Actual Linux venv creation")
    def test_clean_venv_creation_without_installing_external_dependencies(self):
        for name in ("bot.py", "config.py", "requirements.txt"):
            (self.root / name).write_text("")
        class ReexecRequested(Exception):
            pass
        with patch.object(run, "ROOT", self.root), patch.object(run.os, "execv", side_effect=ReexecRequested):
            with self.assertRaises(ReexecRequested):
                run.prepare_environment()
        python = self.root / ".venv/bin/python"
        result = subprocess.run([str(python), "-m", "pip", "--version"], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0)

    def test_windows_locking_branch_simulated_without_windows_execution(self):
        lock = run.InstanceLock(self.root / "windows.lock")
        msvcrt = SimpleNamespace(LK_NBLCK=1, locking=Mock())
        with patch.object(run.os, "name", "nt"), patch.dict(sys.modules, {"msvcrt": msvcrt}):
            with lock:
                msvcrt.locking.assert_called_once_with(lock.file.fileno(), 1, 1)
                self.assertEqual(lock.file.tell(), 0)
            self.assertIsNone(lock.file)

    def test_windows_duplicate_lock_branch_simulated(self):
        lock = run.InstanceLock(self.root / "windows.lock")
        msvcrt = SimpleNamespace(LK_NBLCK=1, locking=Mock(side_effect=OSError("locked")))
        with patch.object(run.os, "name", "nt"), patch.dict(sys.modules, {"msvcrt": msvcrt}):
            with self.assertRaises(run.StartupError):
                lock.__enter__()
            self.assertIsNone(lock.file)

    def test_batch_file_is_crlf_and_paths_are_quoted_static_check(self):
        data = (ROOT / "start.bat").read_bytes()
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        text = data.decode("utf-8")
        self.assertIn('cd /d "%~dp0"', text)
        self.assertIn('".venv\\Scripts\\python.exe"', text)
        self.assertIn('%*', text)
