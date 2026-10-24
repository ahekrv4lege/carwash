import importlib.util
import io
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("check_commands", Path(__file__).resolve().parents[1] / "run.py")
commands = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(commands)


class CheckCommands(unittest.TestCase):
    def diagnostic(self, body):
        case = type("DiagnosticCase", (unittest.TestCase,), {"runTest": body})
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, resultclass=commands.Results).run(case())
        self.assertFalse(result.wasSuccessful())
        text = output.getvalue()
        self.assertIn("DiagnosticCase.runTest", text)
        self.assertIn(str(Path(__file__).resolve()) + ":", text)
        self.assertIn("diagnostic-sentinel", text)
        return text

    def test_failure_has_test_name_filename_line_and_reason(self):
        def body(case):
            case.fail("diagnostic-sentinel")
        self.assertIn("FAIL ", self.diagnostic(body))

    def test_error_has_test_name_filename_line_and_reason(self):
        def body(case):
            raise ValueError("diagnostic-sentinel")
        self.assertIn("ERROR ", self.diagnostic(body))

    def test_subtest_failure_reports_parameters(self):
        def body(case):
            with case.subTest(provider="synthetic"):
                case.fail("diagnostic-sentinel")
        self.assertIn("provider='synthetic'", self.diagnostic(body))

    def test_default_command_blocks_network_keys_and_live_checks(self):
        live = Mock()
        def isolated():
            self.assertNotIn("LLM_API_KEY", os.environ)
            self.assertEqual(os.environ["PYTHON_DOTENV_DISABLED"], "1")
            with self.assertRaises(AssertionError):
                socket.getaddrinfo("example.invalid", 443)
            with socket.socket() as connection:
                with self.assertRaises(AssertionError):
                    connection.connect(("127.0.0.1", 1))
        suite = unittest.TestSuite([unittest.FunctionTestCase(isolated)])
        with patch.object(sys, "argv", [str(commands.FOLDER / "run.py")]), \
             patch.dict(sys.modules, {"live": live}), patch.dict(os.environ, {"LLM_API_KEY": "diagnostic-sentinel"}), \
             patch.object(unittest.defaultTestLoader, "discover", return_value=suite), \
             patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()):
            self.assertEqual(commands.main(), 0)
        live.suite.assert_not_called()
