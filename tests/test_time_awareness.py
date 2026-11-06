from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1] / "plugins/time-awareness"
sys.path.insert(0, str(ROOT))
from hook import file_lock, handle
from install import configure, install


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.start = datetime(2026, 11, 2, 14, tzinfo=timezone.utc)

    def event(self, name, now, session="chat-a", turn="turn-1", **extra):
        return handle(dict(hook_event_name=name, session_id=session, turn_id=turn, **extra),
                      self.state, "America/New_York", now)

    def context(self, result):
        return result["hookSpecificOutput"]["additionalContext"]

    def test_first_message_has_clock_without_inventing_gap(self):
        text = self.context(self.event("UserPromptSubmit", self.start))
        self.assertIn("2026-11-02T09:00:00-05:00", text)
        self.assertIn("prior gap is unknown", text)

    def test_two_day_break_is_measured_from_turn_end(self):
        self.event("UserPromptSubmit", self.start - timedelta(hours=1))
        self.assertEqual({}, self.event("Stop", self.start))
        text = self.context(self.event("UserPromptSubmit", self.start + timedelta(days=2), turn="turn-2"))
        self.assertIn("2 days (172800 seconds)", text)
        self.assertIn("assistant turn ending", text)
        self.assertIn("message submission: 2 days, 1 hour", text)

    def test_resume_and_compaction_do_not_erase_gap(self):
        self.event("UserPromptSubmit", self.start)
        for source in ("resume", "compact"):
            self.event("SessionStart", self.start + timedelta(days=2), source=source)
        text = self.context(self.event("UserPromptSubmit", self.start + timedelta(days=2, hours=1)))
        self.assertIn("2 days, 1 hour (176400 seconds)", text)

    def test_other_chat_does_not_change_this_chat(self):
        self.event("UserPromptSubmit", self.start)
        other = self.context(self.event("UserPromptSubmit", self.start + timedelta(days=1), session="chat-b"))
        self.assertIn("prior gap is unknown", other)
        text = self.context(self.event("UserPromptSubmit", self.start + timedelta(days=2)))
        self.assertIn("2 days (172800 seconds)", text)

    def test_fallback_distinguishes_work_time_from_idle_time(self):
        self.event("UserPromptSubmit", self.start)
        text = self.context(self.event("UserPromptSubmit", self.start + timedelta(hours=3)))
        self.assertIn("3 hours (10800 seconds)", text)
        self.assertIn("not necessarily idle time", text)

    def test_interrupt_records_pause_and_late_stop_is_ignored(self):
        self.event("UserPromptSubmit", self.start)
        self.event("Interrupt", self.start + timedelta(minutes=10))
        text = self.context(self.event("UserPromptSubmit", self.start + timedelta(hours=1), turn="turn-2"))
        self.assertIn("50 minutes (3000 seconds)", text)
        self.assertIn("turn interruption", text)
        self.event("Stop", self.start + timedelta(hours=2), turn="turn-1")
        text = self.context(self.event("UserPromptSubmit", self.start + timedelta(hours=3), turn="turn-3"))
        self.assertIn("2 hours (7200 seconds)", text)

    def test_daylight_saving_uses_utc_elapsed_and_historical_offsets(self):
        start = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
        self.event("UserPromptSubmit", start)
        text = self.context(self.event("UserPromptSubmit", start + timedelta(hours=1)))
        self.assertIn("1 hour (3600 seconds)", text)
        self.assertIn("2026-11-01T01:30:00-04:00", text)
        self.assertIn("2026-11-01T01:30:00-05:00", text)

    def test_clock_going_backward_is_not_a_negative_break(self):
        self.event("UserPromptSubmit", self.start)
        text = self.context(self.event("UserPromptSubmit", self.start - timedelta(hours=1)))
        self.assertIn("elapsed time is unknown (clock changed)", text)

    def test_corrupt_state_is_recoverable(self):
        self.event("UserPromptSubmit", self.start)
        next(self.state.glob("*.json")).write_text("broken", encoding="utf-8")
        text = self.context(self.event("UserPromptSubmit", self.start + timedelta(days=1)))
        self.assertIn("prior gap is unknown", text)
        json.loads(next(self.state.glob("*.json")).read_text())

    def test_storage_failure_still_provides_clock(self):
        self.state.write_text("file instead of directory", encoding="utf-8")
        text = self.context(self.event("UserPromptSubmit", self.start))
        self.assertIn("2026-11-02T09:00:00-05:00", text)
        self.assertIn("Timestamp storage is unavailable", text)

    def test_payload_text_is_neither_persisted_nor_echoed(self):
        result = self.event("UserPromptSubmit", self.start, session="../../outside", prompt="PRIVATE-PROMPT")
        self.event("Stop", self.start, session="../../outside", last_assistant_message="PRIVATE-REPLY")
        stored = "".join(p.read_text() for p in self.state.glob("*.json"))
        self.assertNotIn("PRIVATE", stored + json.dumps(result))
        self.assertEqual(len(list(self.state.glob("*.json"))), 1)


class InstallTests(unittest.TestCase):
    def test_packaged_hooks_execute_with_plugin_environment(self):
        config = json.loads((ROOT / "hooks/hooks.json").read_text())
        with tempfile.TemporaryDirectory(prefix="plugin path é ' $ & %PATH% ") as directory:
            plugin = Path(directory) / "plugin"
            plugin.mkdir()
            shutil.copy2(ROOT / "hook.py", plugin / "hook.py")
            data = Path(directory) / "data"
            data.mkdir()
            env = dict(os.environ, PLUGIN_ROOT=str(plugin), PLUGIN_DATA=str(data))
            (data / "settings.json").write_text('{"timezone": "America/New_York"}')
            for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionStart", "Interrupt"):
                handler = config["hooks"][event][0]["hooks"][0]
                command = handler["commandWindows"] if os.name == "nt" else handler["command"]
                result = subprocess.run(command, shell=True, env=env, cwd=data, text=True,
                    input=json.dumps({"hook_event_name": event, "session_id": "packaged-test", "turn_id": "1"}),
                    capture_output=True, timeout=handler["timeout"], check=True)
                output = json.loads(result.stdout)
                if event in ("SessionStart", "UserPromptSubmit"):
                    self.assertEqual(event, output["hookSpecificOutput"]["hookEventName"])
                    context = output["hookSpecificOutput"]["additionalContext"]
                    self.assertIn("America/New_York", context)
                    if event == "SessionStart" and list((data / "state").glob("*.json")):
                        self.assertIn("assistant turn ending", context)
                else:
                    self.assertEqual({}, output)
            record = json.loads(next((data / "state").glob("*.json")).read_text())
            self.assertEqual("Interrupt", record["last_activity_event"])

    def test_plugin_uses_writable_data_directory_and_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "settings.json").write_text('{"timezone": "Asia/Tokyo"}')
            result = subprocess.run([sys.executable, str(ROOT / "hook.py")], text=True,
                input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "plugin-test", "turn_id": "1"}),
                env=dict(os.environ, PLUGIN_DATA=str(data)), capture_output=True, timeout=3, check=True)
            self.assertIn("Asia/Tokyo", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])
            self.assertTrue(list((data / "state").glob("*.json")))
            self.assertFalse((ROOT / "state").exists())

    def test_preserves_other_hooks_and_removes_only_ours(self):
        target = Path("/some path/codex/time-awareness")
        original = {"description": "existing", "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "existing-command"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": []}],
        }}
        installed = configure(original, target)
        self.assertEqual(installed, configure(installed, target))
        self.assertEqual(original, configure(installed, target, remove=True))

    def test_installed_command_runs_with_quoted_paths_and_saves_state(self):
        with tempfile.TemporaryDirectory(prefix="clock path é ' $ & %PATH% ") as directory:
            codex_dir = Path(directory)
            summary = install(codex_dir, "America/New_York")
            config = json.loads((codex_dir / "hooks.json").read_text())
            for event in ("UserPromptSubmit", "Stop", "SessionStart", "Interrupt"):
                command = config["hooks"][event][0]["hooks"][0]["command"]
                result = subprocess.run(command, shell=True, cwd=codex_dir, text=True,
                    input=json.dumps({"hook_event_name": event, "session_id": "test", "turn_id": "turn"}),
                    capture_output=True, timeout=3, check=True)
                output = json.loads(result.stdout)
                if event in ("UserPromptSubmit", "SessionStart"):
                    self.assertEqual(event, output["hookSpecificOutput"]["hookEventName"])
                    self.assertIn("America/New_York", output["hookSpecificOutput"]["additionalContext"])
                else:
                    self.assertEqual({}, output)
            self.assertTrue(Path(summary["script"]).exists())
            install(codex_dir, remove=True)
            self.assertEqual({}, json.loads((codex_dir / "hooks.json").read_text())["hooks"])
            self.assertTrue(list((codex_dir / "time-awareness/state").glob("*.json")))

    def test_backup_preserves_exact_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_dir = Path(directory)
            original = b'{ "description": "keep", "hooks": {} }\n'
            (codex_dir / "hooks.json").write_bytes(original)
            summary = install(codex_dir)
            self.assertEqual(original, Path(summary["backup"]).read_bytes())
            self.assertNotIn("backup", install(codex_dir))

    def test_dry_run_writes_nothing_and_malformed_config_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_dir = Path(directory)
            install(codex_dir, dry_run=True)
            self.assertEqual([], list(codex_dir.iterdir()))
            (codex_dir / "hooks.json").write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                install(codex_dir)
            self.assertEqual("broken", (codex_dir / "hooks.json").read_text())

    def test_invalid_input_does_not_block_the_chat(self):
        result = subprocess.run([sys.executable, str(ROOT / "hook.py")], input="not json", text=True,
                                capture_output=True, timeout=3)
        self.assertEqual(0, result.returncode)
        self.assertEqual({}, json.loads(result.stdout))


class PortabilityTests(unittest.TestCase):
    def test_lock_excludes_another_process_then_releases(self):
        code = (
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); from hook import file_lock\n"
            "try:\n"
            "    with file_lock(Path(sys.argv[2]), timeout=0.1): print('acquired')\n"
            "except TimeoutError: print('busy')\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.lock"
            command = [sys.executable, "-c", code, str(ROOT), str(path)]
            with file_lock(path):
                result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=True)
                self.assertEqual("busy", result.stdout.strip())
            result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=True)
            self.assertEqual("acquired", result.stdout.strip())

    def test_default_clock_needs_no_timezone_package(self):
        with tempfile.TemporaryDirectory() as directory:
            # -S hides site packages; an empty PYTHONTZPATH hides any system IANA database.
            env = dict(os.environ, PLUGIN_DATA=directory, PYTHONTZPATH="")
            result = subprocess.run([sys.executable, "-S", str(ROOT / "hook.py")], env=env,
                input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "no-deps"}),
                capture_output=True, text=True, timeout=3, check=True)
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Current local date/time:", context)
            self.assertIn("Current UTC:", context)
            self.assertNotIn("timezone is unavailable", context)

    def test_missing_iana_data_falls_back_to_local_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PLUGIN_DATA=directory, PYTHONTZPATH="")
            result = subprocess.run([sys.executable, "-S", str(ROOT / "hook.py"),
                                     "--timezone", "America/New_York"], env=env,
                input=json.dumps({"hook_event_name": "SessionStart", "session_id": "no-tzdata"}),
                capture_output=True, text=True, timeout=3, check=True)
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Current local date/time:", context)
            self.assertIn("Configured IANA timezone is unavailable", context)

    def test_utf8_input_with_optional_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PLUGIN_DATA=directory)
            payload = {"hook_event_name": "UserPromptSubmit", "session_id": "chat-日本語"}
            result = subprocess.run([sys.executable, str(ROOT / "hook.py")], env=env,
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8-sig"),
                capture_output=True, timeout=3, check=True)
            self.assertIn("hookSpecificOutput", json.loads(result.stdout))
            self.assertTrue(list((Path(directory) / "state").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
