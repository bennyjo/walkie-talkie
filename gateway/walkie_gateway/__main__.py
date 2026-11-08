"""Runs the gateway. See README.md, or run: walkie-gateway serve --help"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys

from . import security
from .conversation import Conversation
from .front_desk import MOD_DIR
from .modlink import ModLink
from .phone import PhoneServer
from .speech import OpusDecoder, OpusEncoder, Speech, VoiceDetector, default_models_dir

DEFAULT_PORT = 8791
MOD_NAME = "walk-voice"
MOD_FILES = (".claude-plugin/plugin.json", "hooks/hooks.json")  # and every hooks/*.js
UNREADABLE = "Can't read %s: %s. Nothing was changed. Fix the file, or move it away to start over."


def warn_if_vpn_blocks_tailscale():
    """Some VPNs block Tailscale to the phone. Only Mullvad is detected, and only its status is read."""
    if not shutil.which("mullvad"):
        return
    try:
        out = subprocess.run(["mullvad", "status"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    if out.split()[:1] in (["Connected"], ["Connecting"]):
        print("Warning: a VPN that blocks Tailscale is connected. Turn it off.", flush=True)


def keep_mac_awake():
    """The Mac stays awake while this process lives. caffeinate exits by itself when it ends."""
    if shutil.which("caffeinate"):
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return None


async def serve(args):
    state_dir = security.private_dir(args.state)
    host = security.check_bind(args.bind) if args.bind else security.tailscale_address()
    warn_if_vpn_blocks_tailscale()

    print("Loading the speech models. This takes a few seconds.", flush=True)
    loop = asyncio.get_running_loop()
    speech = await loop.run_in_executor(None, Speech, args.models, args.threads)

    conversation = Conversation(speech, VoiceDetector(args.models), OpusDecoder, OpusEncoder(),
                                voice_file=os.path.join(state_dir, "voice"))
    store = security.PhoneStore(state_dir)
    pairing = security.PairingWindow() if args.pair else None
    phone = PhoneServer(store, conversation, pairing)
    modlink = ModLink(os.path.join(state_dir, "mod.sock"), conversation)
    conversation.phone, conversation.modlink = phone, modlink

    await modlink.start()
    port = await phone.start(host, args.port)
    conversation.start()
    awake = keep_mac_awake()

    print("Listening for the phone on %s, port %d." % (host, port), flush=True)
    if pairing:
        print("Pairing code: %s. In the app, enter this Mac's address and the code within %d seconds."
              % (pairing.code, security.PAIR_SECONDS), flush=True)
    elif not store.paired:
        print("No phone is paired yet. Restart with --pair to pair one.", flush=True)

    stop = asyncio.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)
    await stop.wait()

    print("Stopping.", flush=True)
    conversation.stop()
    await phone.stop()
    await modlink.stop()
    if awake:
        awake.terminate()


def install(logins, deny, deny_on, remove_logins=(), allow=(), allow_on=()):
    """A read-only copy of the mod in each login that may connect, and the list of what may connect.

    A copy, not a link: an edit or a branch switch in this checkout must not change what
    every session runs. Read-only, so a stray save doesn't either. The logins and deny lists
    stay in the owner's own file, never in this repository. An install only adds to them:
    --remove-login takes a login away but keeps its deny list for when it comes back, and
    only --allow and --allow-on take a denied name away.
    """
    config_path = security.connect_config_path()
    old = _read_config(config_path)
    lists = _deny_lists(old.get("logins"))
    kept = _deny_lists(old.get("removed"))
    named = [_login_dir(login) for login in logins]
    removed = [_path(login) for login in remove_logins]
    for login in removed:
        if login in named:
            raise SystemExit("%s is named by both --login and --remove-login." % login)
        if login not in lists:
            raise SystemExit("--remove-login names %s, which isn't an installed login." % login)
        kept[login] = _names(lists.pop(login), kept.get(login, []))
    restored = {}
    for login in named:
        if login not in lists:
            lists[login] = restored[login] = _names(kept.pop(login, []))
    if not lists:
        raise SystemExit("No login may connect. Name one with --login.")
    for login in lists:
        if login not in named:
            _login_dir(login, "Name it with --remove-login to stop installing into it.")
    added, taken = _per_login(deny_on, lists, "--deny-on"), _per_login(allow_on, lists, "--allow-on")
    config = {
        "logins": {login: {"deny": _without(_names(names, added[login]), taken[login], "--allow-on")}
                   for login, names in lists.items()},
        "deny": _without(_names(old.get("deny", []), deny), allow, "--allow"),
    }
    kept = {login: {"deny": names} for login, names in kept.items() if names}
    if kept:
        config["removed"] = kept
    for login in removed:
        _remove(os.path.join(login, "skills", MOD_NAME))
        print("Removed the mod from the login at %s." % login, flush=True)
    for login, names in restored.items():
        if names:
            print("The login at %s gets back its own deny list, kept from when it was removed: %s." % (login, _count(names)), flush=True)
    for login in config["logins"]:
        target = _copy_mod(os.path.join(login, "skills"))
        print("Installed the mod into %s, for the login at %s." % (target, login), flush=True)
    security.private_dir(os.path.dirname(config_path))
    security.write_private(config_path, json.dumps(config, indent=2) + "\n")
    print("Sessions on these logins may connect, after a fingerprint on the phone:", flush=True)
    for login, entry in config["logins"].items():
        print("  %s, with %s of its own denied." % (login, _count(entry["deny"])), flush=True)
    print("Denied on every login: %s." % _count(config["deny"]), flush=True)
    print("Settings saved in %s. Start new sessions, or run /reload-plugins in running ones." % config_path, flush=True)


def _read_config(path):
    """The owner's settings, or none yet. A file that's there but can't be read stops the install, so no denied name is lost."""
    try:
        with open(path) as f:
            config = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SystemExit(UNREADABLE % (path, e))
    problem = _config_problem(config)
    if problem:
        raise SystemExit(UNREADABLE % (path, problem))
    return config


def _config_problem(config):
    if not isinstance(config, dict):
        return "it isn't a JSON object"
    if not _name_list(config.get("deny", [])):
        return '"deny" isn\'t a list of names'
    for key in ("logins", "removed"):
        logins = config.get(key, {})
        if not isinstance(logins, dict):
            return '"%s" isn\'t an object' % key
        for login, entry in logins.items():
            if not isinstance(entry, dict) or not _name_list(entry.get("deny", [])):
                return "the deny list for %s isn't a list of names" % login
    return None


def _name_list(names):
    return isinstance(names, list) and all(isinstance(name, str) for name in names)


def _deny_lists(logins):
    logins = logins if isinstance(logins, dict) else {}
    return {login: entry.get("deny", []) if isinstance(entry, dict) else [] for login, entry in logins.items()}


def _path(login):
    return os.path.realpath(os.path.expanduser(login))


def _login_dir(login, hint="Name the folder that CLAUDE_CONFIG_DIR points at, or ~/.claude."):
    login = _path(login)
    if not os.path.isfile(os.path.join(login, ".claude.json")) and not os.path.isdir(os.path.join(login, "projects")):
        raise SystemExit("%s doesn't look like a Claude Code login folder. %s" % (login, hint))
    return login


def _per_login(pairs, lists, flag):
    names = {login: [] for login in lists}
    for login, name in pairs:
        login = _path(login)
        if login not in names:
            raise SystemExit("%s names %s, which isn't an installed login or one of the --login folders." % (flag, login))
        names[login].append(name)
    return names


def _without(names, allowed, flag):
    names = list(names)
    for name in allowed:
        name = name.strip().lower()
        if name not in names:
            raise SystemExit("%s names %s, which isn't denied there." % (flag, name))
        names.remove(name)
    return names


def _count(names):
    return "1 name" if len(names) == 1 else "%d names" % len(names)


def _names(*lists):
    names = sorted({name.strip().lower() for names in lists if isinstance(names, list)
                    for name in names if isinstance(name, str) and name.strip()})
    if any("/" in name for name in names):
        raise SystemExit("A denied name is one folder's, an owner's, or a repository's name, with no slash.")
    return names


def _copy_mod(skills):
    os.makedirs(skills, exist_ok=True)
    target = os.path.join(skills, MOD_NAME)
    staging = os.path.join(skills, "." + MOD_NAME + ".new")
    _remove(staging)
    files = list(MOD_FILES) + sorted("hooks/" + name for name in os.listdir(os.path.join(MOD_DIR, "hooks")) if name.endswith(".js"))
    for name in files:
        os.makedirs(os.path.dirname(os.path.join(staging, name)), exist_ok=True)
        shutil.copyfile(os.path.join(MOD_DIR, name), os.path.join(staging, name))
    for folder, _, names in os.walk(staging, topdown=False):
        for name in names:
            os.chmod(os.path.join(folder, name), 0o444)
        if folder != staging:
            os.chmod(folder, 0o555)
    _remove(target)
    os.rename(staging, target)  # macOS moves a folder only while it can write to it
    os.chmod(target, 0o555)
    return target


def _remove(path):
    """Removes an earlier copy, which is read-only, or a link to a checkout."""
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        for folder, _, _ in os.walk(path):
            os.chmod(folder, stat.S_IRWXU)
        shutil.rmtree(path)


def revoke(state):
    """The phone stops being trusted at once, whether or not the gateway is running."""
    told = _post(os.path.join(state, "mod.sock"), "/revoke")
    for name in (security.PHONE_FILE, security.PHONE_KEY_FILE):
        try:
            os.unlink(os.path.join(state, name))
        except FileNotFoundError:
            pass
    if told:
        print("Revoked the phone. The running gateway closed its connection and forgot it.", flush=True)
    else:
        print("Revoked the phone in %s. No gateway answered there, so a gateway using that folder refuses the phone"
              " when it next starts. If a gateway is running with another --state, run revoke with that --state too."
              % state, flush=True)
    print("To use a phone again, pair it: walkie-gateway serve --pair", flush=True)


def _post(path, route):
    """True if a running gateway answered."""
    probe = socket.socket(socket.AF_UNIX)
    probe.settimeout(5)
    try:
        probe.connect(path)
        probe.sendall(("POST %s HTTP/1.1\r\nHost: walkie\r\nContent-Length: 2\r\n\r\n{}" % route).encode())
        return probe.recv(64).startswith(b"HTTP/1.1 200")
    except OSError:
        return False
    finally:
        probe.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="walkie-gateway", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("serve", help="listen for the phone and for live sessions")
    run.add_argument("--pair", action="store_true", help="print a pairing code and accept a new phone")
    run.add_argument("--bind", help="listen here instead of on this Mac's Tailscale address; "
                                    "only a Tailscale or loopback address is allowed")
    run.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument("--state", default=security.default_state_dir(), help="private folder for the phone's token and the mod's socket")
    run.add_argument("--models", default=default_models_dir(), help="folder that holds the speech models")
    run.add_argument("--threads", type=int, default=4, help="threads for each speech model")
    lost = commands.add_parser("revoke", help="stop trusting the paired phone, at once: for a lost or stolen phone")
    lost.add_argument("--state", default=security.default_state_dir(), help="the gateway's private folder")
    setup = commands.add_parser("install", help="install the mod into the logins you name, and say which may connect")
    setup.add_argument("--login", action="append", default=[],
                       help="a Claude Code login folder, such as ~/.claude-personal; repeatable, and kept from "
                            "earlier installs, which this one refreshes too")
    setup.add_argument("--remove-login", action="append", default=[], metavar="LOGIN",
                       help="stop that login's sessions from connecting, and remove the mod from it; repeatable")
    setup.add_argument("--deny", action="append", default=[], metavar="NAME",
                       help="never list or connect a session whose folder path, or whose git remote's owner or "
                            "repository, has this name, on any login; repeatable, and kept from earlier installs")
    setup.add_argument("--deny-on", action="append", default=[], nargs=2, metavar=("LOGIN", "NAME"),
                       help="the same, on that login only; repeatable, and kept from earlier installs")
    setup.add_argument("--allow", action="append", default=[], metavar="NAME",
                       help="take a name off the list denied on every login; repeatable")
    setup.add_argument("--allow-on", action="append", default=[], nargs=2, metavar=("LOGIN", "NAME"),
                       help="take a name off that login's own deny list; repeatable")
    args = parser.parse_args(argv)
    if args.command == "revoke":
        revoke(args.state)
        return 0
    if args.command == "install":
        install(args.login, args.deny, args.deny_on, args.remove_login, args.allow, args.allow_on)
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
    try:
        asyncio.run(serve(args))
    except (security.SecurityError, FileNotFoundError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
