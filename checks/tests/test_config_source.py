"""Конфигурация только через реальный .env: шаблоны и зависимости убраны.

env.example удалён из проекта (требование): примерные файлы конфигурации
нечаянно попадали в прод с placeholder-значениями. Развёртывание требует
настоящий .env (deploy/setup-vps.sh: «Положите $APP_DIR/.env и повторите»).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class NoExampleEnvFilesTests(unittest.TestCase):
    def test_example_env_files_are_absent(self):
        for name in ("env.example", ".env.example", "env.example.local", ".env.sample"):
            self.assertFalse((ROOT / name).exists(), f"{name} не должен существовать")

    def test_env_example_is_not_tracked_by_git(self):
        import subprocess
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout
        self.assertNotIn("env.example", tracked)
        self.assertNotIn(".env.example", tracked)

    def test_env_is_gitignored(self):
        import subprocess
        ignored = subprocess.run(["git", "check-ignore", "-v", ".env"], cwd=ROOT,
                                 capture_output=True, text=True).stdout
        self.assertIn(".gitignore", ignored)  # .env исключён из git именно через .gitignore

    def test_no_source_file_references_the_template(self):
        offenders = []
        for path in list(ROOT.glob("*.py")) + list((ROOT / "checks").glob("*.py")) + \
                [ROOT / "deploy" / "setup-vps.sh", ROOT / "start.sh", ROOT / "start.bat"]:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "env.example" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_deploy_script_requires_real_env_file(self):
        script = (ROOT / "deploy" / "setup-vps.sh").read_text(encoding="utf-8")
        self.assertIn('APP_DIR/.env', script)
        self.assertIn("Положите", script)
        self.assertNotIn("env.example", script)


class LocalEnvLoadingTests(unittest.TestCase):
    def test_config_reads_real_env_file_and_reports_missing_keys(self):
        from config import _required

        with self.assertRaises(RuntimeError) as caught:
            _required("BOT_TOKEN")
        self.assertIn("BOT_TOKEN", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
