"""Starts the front desk: a small session the phone talks to, so that no spoken prompt waits on the session doing the real work.

The session runs headless, and this process holds the other end of its stdin, so the session
ends when this process does, however it ends. A lock in the state folder allows one at a time.
What to run and which login to use are local configuration: see docs/front-desk.md.
"""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys

from . import security

CONFIG_FILE = "front-desk.json"
LOCK_FILE = "front-desk.lock"
SESSION_DIR = "front-desk"  # the phone lists a session by its folder's name
DEFAULT_MODEL = "haiku"
COMMANDS = ("status", "handoff", "replies")
STOP_WAIT_SECONDS = 5
MOD_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "walk-voice"))
NOT_INHERITED = ("FM_TASK_ID",)  # the mod stays out of an orchestrated worker, and this isn't one


class ConfigError(Exception):
    pass


class Stop(Exception):
    pass


def load_config(path):
    try:
        with open(path) as f:
            config = json.load(f)
    except FileNotFoundError:
        raise ConfigError("No front desk is set up: write %s. See docs/front-desk.md." % path) from None
    except (OSError, ValueError) as e:
        raise ConfigError("Can't read %s: %s" % (path, e)) from None
    if not isinstance(config, dict):
        raise ConfigError("%s must hold a JSON object." % path)
    for name in COMMANDS:
        command = config.get(name)
        if not (isinstance(command, list) and command and all(isinstance(part, str) and part for part in command)):
            raise ConfigError('%s needs "%s": a command as a list of strings.' % (path, name))
    env = config.setdefault("env", {})
    if not (isinstance(env, dict) and all(isinstance(value, str) for value in env.values())):
        raise ConfigError('In %s, "env" must map names to strings.' % path)
    model = config.setdefault("model", DEFAULT_MODEL)
    if not (isinstance(model, str) and model):
        raise ConfigError('In %s, "model" must be a model name.' % path)
    return config


def claude_command(config):
    """No tools, no MCP servers, no settings, and no saved transcript: it can't act, and it stays small."""
    return ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
            "--disable-slash-commands", "--tools", "", "--plugin-dir", MOD_DIR, "--model", config["model"]]


def session_env(config, state_dir, base):
    env = {name: value for name, value in base.items() if name not in NOT_INHERITED}
    env.update(config["env"])
    env["WALKIE_TALKIE_STATE"] = state_dir
    env["WALKIE_FRONT_DESK"] = json.dumps({
        "model": config["model"],
        **{name: config[name] for name in COMMANDS},
    })
    return env


def take_lock(state_dir):
    """Held until this process ends, however it ends, so a crash leaves nothing to clean up."""
    lock = open(os.path.join(state_dir, LOCK_FILE), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise ConfigError("A front desk is already running for %s. Stop it first." % state_dir) from None
    return lock


def _raise_stop(_signal, _frame):
    raise Stop


def hold(child):
    """Stay until the session ends or this process is told to stop."""
    signal.signal(signal.SIGINT, _raise_stop)
    signal.signal(signal.SIGTERM, _raise_stop)
    try:
        return child.wait()
    except Stop:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child.stdin.close()  # the session ends itself, and tells the gateway it has gone
        try:
            return child.wait(STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            return child.wait()


def run(state_dir):
    state_dir = security.private_dir(os.path.abspath(state_dir))  # the session runs from another folder
    config = load_config(os.path.join(state_dir, CONFIG_FILE))
    lock = take_lock(state_dir)
    session_dir = security.private_dir(os.path.join(state_dir, SESSION_DIR))
    child = subprocess.Popen(claude_command(config), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             cwd=session_dir, env=session_env(config, state_dir, os.environ))
    print("The front desk is running on %s, as session process %d under launcher process %d. Stop the launcher to stop it."
          % (config["model"], child.pid, os.getpid()), flush=True)
    code = hold(child)
    lock.close()
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(prog="walkie-front-desk", description=__doc__)
    parser.add_argument("--state", default=security.default_state_dir(), help="the gateway's private folder")
    args = parser.parse_args(argv)
    try:
        return run(args.state)
    except (ConfigError, security.SecurityError, FileNotFoundError) as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
