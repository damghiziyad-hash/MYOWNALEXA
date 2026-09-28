import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
 
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
 
import tools  # noqa: E402
 
 
class RegistryTests(unittest.TestCase):
    def test_every_tool_is_documented_and_unique(self):
        names = [t.__name__ for t in tools.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for func in tools.TOOLS:
            self.assertTrue(func.__doc__, f"{func.__name__} has no docstring")
 
    def test_confirmation_functions_are_hidden_from_gemini(self):
        names = {t.__name__ for t in tools.TOOLS}
        self.assertNotIn("confirm_pending_action", names)
        self.assertNotIn("cancel_pending_action", names)
 
 
class AudioTests(unittest.TestCase):
    def test_missing_wpctl_gives_clean_error(self):
        with mock.patch("tools.shutil.which", return_value=None):
            self.assertTrue(tools.get_volume().startswith("ERROR: wpctl is not available"))
 
    def test_get_volume_parses_wpctl_output(self):
        with mock.patch("tools._wpctl", return_value="Volume: 0.45"):
            self.assertEqual(tools.get_volume(), "SUCCESS: Volume is 45%.")
        with mock.patch("tools._wpctl", return_value="Volume: 0.30 [MUTED]"):
            self.assertIn("muted", tools.get_volume())
 
    def test_decrease_calls_wpctl_with_argument_list(self):
        with mock.patch("tools._wpctl", return_value="Volume: 0.40") as wpctl:
            result = tools.decrease_volume(10)
        wpctl.assert_any_call("set-volume", "-l", "1.0", tools.AUDIO_SINK, "10%-")
        self.assertEqual(result, "SUCCESS: Volume decreased to 40%.")
 
    def test_invalid_levels_are_rejected(self):
        for bad in (-1, 101, "abc", None):
            self.assertTrue(tools.set_volume(bad).startswith("ERROR:"), bad)
 
 
class ApplicationTests(unittest.TestCase):
    def test_unknown_app_is_refused(self):
        self.assertIn("Unknown application", tools.open_application("rm -rf /"))
 
    def test_terminal_cannot_be_closed(self):
        self.assertIn("cannot be closed", tools.close_application("terminal"))
 
    def test_aliases_and_missing_binary(self):
        with mock.patch("tools.shutil.which", return_value=None):
            self.assertEqual(
                tools.check_application_available("Visual Studio Code"),
                "ERROR: VS Code is not installed.",
            )
 
    def test_open_launches_detached_without_shell(self):
        with mock.patch("tools.shutil.which", return_value="/usr/bin/firefox"), mock.patch(
            "tools.subprocess.Popen"
        ) as popen:
            self.assertEqual(tools.open_application("Firefox"), "SUCCESS: Firefox is opening.")
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/firefox"])
        self.assertNotIn("shell", popen.call_args.kwargs)
 
 
class WebTests(unittest.TestCase):
    def test_url_normalization(self):
        self.assertEqual(tools._normalize_url("youtube"), "https://www.youtube.com")
        self.assertEqual(tools._normalize_url("github.com/x"), "https://github.com/x")
 
    def test_dangerous_urls_are_refused(self):
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://a.com", "not a url", ""):
            self.assertTrue(tools.open_website(bad).startswith("ERROR:"), bad)
 
    def test_search_encodes_query(self):
        with mock.patch("tools.webbrowser.open", return_value=True) as opened:
            tools.search_web("python 3.12 & more")
        opened.assert_called_once_with("https://www.google.com/search?q=python+3.12+%26+more")
 
 
class FileTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        patcher = mock.patch("tools.HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
 
    def test_create_folder_and_file(self):
        self.assertIn("SUCCESS", tools.create_folder("Projet IA", str(self.home)))
        self.assertTrue((self.home / "Projet IA").is_dir())
        self.assertIn("SUCCESS", tools.create_file("a.txt", str(self.home), "hi"))
        self.assertEqual((self.home / "a.txt").read_text(), "hi")
        self.assertIn("already exists", tools.create_file("a.txt", str(self.home)))
 
    def test_path_traversal_is_blocked(self):
        for bad in ("../../etc", "/etc/passwd", "~/../.."):
            self.assertIn("restricted", tools.open_file(bad), bad)
        self.assertIn("Invalid name", tools.create_folder("../evil", str(self.home)))
 
    def test_list_directory(self):
        (self.home / "b.txt").write_text("x")
        (self.home / "dossier").mkdir()
        self.assertIn("dossier/, b.txt", tools.list_directory(str(self.home)))
 
    def test_delete_needs_confirmation(self):
        target = self.home / "old.txt"
        target.write_text("x")
        self.assertTrue(tools.delete_file(str(target)).startswith("CONFIRMATION_REQUIRED"))
        self.assertTrue(target.exists(), "file must survive until the user confirms")
        with mock.patch("tools.shutil.which", return_value=None):  # force permanent delete
            self.assertIn("SUCCESS", tools.confirm_pending_action())
        self.assertFalse(target.exists())
 
    def test_delete_protections(self):
        (self.home / ".ssh").mkdir()
        secret = self.home / ".ssh" / "id_rsa"
        secret.write_text("x")
        self.assertIn("protected", tools.delete_file(str(secret)))
        self.assertIn("not an existing file", tools.delete_file(str(self.home)))
        self.assertFalse(tools.has_pending_action())
 
 
class ConfirmationTests(unittest.TestCase):
    def test_shutdown_only_runs_after_confirmation(self):
        with mock.patch("tools.shutil.which", return_value="/usr/bin/systemctl"), mock.patch(
            "tools._run"
        ) as run:
            self.assertTrue(tools.shutdown_pc().startswith("CONFIRMATION_REQUIRED"))
            run.assert_not_called()
            self.assertTrue(tools.has_pending_action())
            self.assertIn("SUCCESS", tools.confirm_pending_action())
            run.assert_called_once_with(["/usr/bin/systemctl", "poweroff"])
        self.assertFalse(tools.has_pending_action())
 
    def test_cancel_and_expiry(self):
        with mock.patch("tools.shutil.which", return_value="/usr/bin/systemctl"):
            tools.restart_pc()
            tools.cancel_pending_action()
            self.assertIn("No action", tools.confirm_pending_action())
        gate = tools._ConfirmationGate(timeout_s=-1)
        gate.request("x", lambda: "SUCCESS")
        self.assertFalse(gate.has_pending())
 
 
class MonitoringTests(unittest.TestCase):
    def test_monitoring_tools_return_success(self):
        for func in (
            tools.get_cpu_usage,
            tools.get_memory_usage,
            tools.get_disk_usage,
            tools.get_system_info,
            tools.get_running_processes,
            tools.get_time,
            tools.get_date,
            tools.get_datetime,
        ):
            self.assertTrue(func().startswith("SUCCESS:"), func.__name__)
 
    def test_battery_without_battery(self):
        with mock.patch("tools.psutil.sensors_battery", return_value=None):
            self.assertIn("No battery", tools.get_battery_status())
 
 
if __name__ == "__main__":
    unittest.main()
 