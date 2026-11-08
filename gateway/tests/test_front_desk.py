"""The front desk's launcher: its settings, its one-at-a-time lock, and that it leaves nothing behind."""
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from walkie_gateway import front_desk

FAKE_CLAUDE = """#!/bin/sh
# Stands in for a headless session: it lives until its stdin closes.
echo "$$" > "$WALKIE_TALKIE_STATE/fake-claude.pid"
pwd > "$WALKIE_TALKIE_STATE/fake-claude.cwd"
env > "$WALKIE_TALKIE_STATE/fake-claude.env"
printf '%s\\n' "$@" > "$WALKIE_TALKIE_STATE/fake-claude.args"
cat > /dev/null
"""
SETTINGS = {
    "status": ["/some/tool", "status"],
    "handoff": ["/some/tool", "note"],
    "replies": ["/some/tool", "replies", "--after", "{cursor}"],
    "env": {"CLAUDE_CONFIG_DIR": "/some/login"},
}


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_for(check, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "front-desk.json")

    def load(self, config):
        with open(self.path, "w") as f:
            f.write(config if isinstance(config, str) else json.dumps(config))
        return front_desk.load_config(self.path)

    def test_a_missing_file_names_the_file_to_write(self):
        with self.assertRaisesRegex(front_desk.ConfigError, "front-desk.json"):
            front_desk.load_config(self.path)

    def test_broken_json_is_refused(self):
        with self.assertRaises(front_desk.ConfigError):
            self.load("{not json")

    def test_every_command_is_required(self):
        for name in front_desk.COMMANDS:
            with self.assertRaisesRegex(front_desk.ConfigError, name):
                self.load({k: v for k, v in SETTINGS.items() if k != name})

    def test_a_command_must_be_a_list_so_nothing_goes_through_a_shell(self):
        with self.assertRaisesRegex(front_desk.ConfigError, "status"):
            self.load({**SETTINGS, "status": "/some/tool status"})

    def test_the_model_is_optional(self):
        self.assertEqual(self.load(SETTINGS)["model"], front_desk.DEFAULT_MODEL)
        self.assertEqual(self.load({**SETTINGS, "model": "other"})["model"], "other")

    def test_the_session_gets_no_tools_and_no_settings(self):
        command = front_desk.claude_command(self.load(SETTINGS))
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--no-session-persistence", command)

    def test_the_session_is_not_taken_for_a_worker_and_gets_its_local_settings(self):
        env = front_desk.session_env(self.load(SETTINGS), "/state", {"FM_TASK_ID": "x", "KEPT": "1"})
        self.assertNotIn("FM_TASK_ID", env)
        self.assertEqual(env["KEPT"], "1")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/some/login")
        self.assertEqual(env["WALKIE_TALKIE_STATE"], "/state")
        self.assertEqual(json.loads(env["WALKIE_FRONT_DESK"]), {
            "model": front_desk.DEFAULT_MODEL,
            "status": SETTINGS["status"],
            "handoff": SETTINGS["handoff"],
            "replies": SETTINGS["replies"],
        })


class LauncherTest(unittest.TestCase):
    """Runs the real launcher, with a stand-in for claude on the PATH."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = os.path.join(self.dir.name, "state")
        os.mkdir(self.state, 0o700)
        with open(os.path.join(self.state, front_desk.CONFIG_FILE), "w") as f:
            json.dump(SETTINGS, f)
        bin_dir = os.path.join(self.dir.name, "bin")
        os.mkdir(bin_dir)
        claude = os.path.join(bin_dir, "claude")
        with open(claude, "w") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(claude, os.stat(claude).st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "PATH": bin_dir + os.pathsep + os.environ["PATH"]}
        self.launchers = []

    def tearDown(self):
        for launcher in self.launchers:
            if launcher.poll() is None:
                launcher.kill()
            launcher.communicate()

    def start(self, state=None, cwd=None):
        launcher = subprocess.Popen([sys.executable, "-m", "walkie_gateway.front_desk", "--state", state or self.state],
                                    env=self.env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.launchers.append(launcher)
        return launcher

    def read(self, name):
        try:
            with open(os.path.join(self.state, name)) as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def session_pid(self):
        self.assertTrue(wait_for(lambda: self.read("fake-claude.pid")))
        return int(self.read("fake-claude.pid"))

    def test_the_session_runs_in_a_folder_the_phone_lists_as_the_front_desk(self):
        self.start()
        self.session_pid()
        self.assertEqual(os.path.basename(self.read("fake-claude.cwd")), "front-desk")

    def test_a_relative_state_folder_still_reaches_the_gateway_from_the_session(self):
        self.env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(front_desk.__file__)))
        self.start(state="state", cwd=self.dir.name)
        self.session_pid()
        self.assertTrue(wait_for(lambda: self.read("fake-claude.env")))
        self.assertIn("WALKIE_TALKIE_STATE=" + os.path.realpath(self.state), self.read("fake-claude.env"))

    def test_a_second_front_desk_is_refused(self):
        self.start()
        pid = self.session_pid()
        second = self.start()
        _, err = second.communicate(timeout=10)
        self.assertEqual(second.returncode, 1)
        self.assertIn("already running", err)
        self.assertTrue(alive(pid))

    def test_stopping_the_launcher_stops_the_session(self):
        for how in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(how=how):
                launcher = self.start()
                pid = self.session_pid()
                launcher.send_signal(how)
                launcher.communicate(timeout=10)
                self.assertTrue(wait_for(lambda: not alive(pid)))
                os.unlink(os.path.join(self.state, "fake-claude.pid"))

    def test_killing_the_launcher_outright_leaves_no_session(self):
        launcher = self.start()
        pid = self.session_pid()
        launcher.kill()
        launcher.communicate(timeout=10)
        self.assertTrue(wait_for(lambda: not alive(pid)))

    def test_a_new_front_desk_can_start_after_one_was_killed(self):
        first = self.start()
        pid = self.session_pid()
        first.kill()
        first.communicate(timeout=10)
        self.assertTrue(wait_for(lambda: not alive(pid)))
        os.unlink(os.path.join(self.state, "fake-claude.pid"))
        self.start()
        self.assertTrue(alive(self.session_pid()))


if __name__ == "__main__":
    unittest.main()
