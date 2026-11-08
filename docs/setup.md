# Set up Walkie Talkie

The [README](../README.md#quick-start) has the short version. This page has every step and option.

## What you need

- A Mac on Apple silicon with [uv](https://docs.astral.sh/uv/). The speech models take about 1 GB, and downloading them needs about 2 GB free. uv provides Python if the Mac has no suitable version.
- [Tailscale](https://tailscale.com/) connected on the Mac and the phone. A VPN that blocks Tailscale, such as Mullvad, has to be off.
- Claude Code 2.1.318 or later. The mods API can still change between Claude Code releases.
- An Android phone on Android 13 or later, with a fingerprint set up, a USB audio input, and Bluetooth earbuds. This was designed for the DJI Mic Mini 2 receiver.

## On the Mac

Run the commands on this page from the root of your clone of this repository.

```sh
gateway/fetch-models.sh
uv run --project gateway walkie-gateway serve --pair
```

The first command downloads the models and checks their checksums. The second listens on the Mac's Tailscale address and prints that address and an 8-digit pairing code. The code works once and lasts two minutes.

The gateway keeps the Mac awake while it runs, and it stops with Ctrl+C. After pairing, start it without `--pair`.

## On the phone

1. Install the APK. Build it with `android/build.sh`, which puts it at `android/out/walkie-talkie.apk`.
1. In **Settings > Apps > Walkie Talkie > App battery usage**, choose **Unrestricted**.
1. Open **Walkie Talkie**, enter the Mac's Tailscale address and the pairing code, and tap **Pair**. The phone makes a key in its secure hardware and gives the Mac the public half. The key signs only after a fingerprint.

A pairing lasts 30 days. Then the app asks you to pair again, with a new code from `walkie-gateway serve --pair`. Adding a fingerprint to the phone also ends the pairing, because it destroys the key.

## Install the mod

Name each Claude Code login whose sessions may connect. A login is the folder that `CLAUDE_CONFIG_DIR` points at, or `~/.claude` when it isn't set. Nothing is installed into a login you don't name.

```sh
uv run --project gateway walkie-gateway install --login ~/.claude --login ~/.claude-personal
```

It prints each folder it installed into. Each gets a read-only copy of the mod, not a link to this checkout, so editing or switching branches here doesn't change what your sessions run. Sessions started after that load the mod by themselves. A session that was already running needs `/reload-plugins` typed once.

An install only adds. Logins and deny lists from earlier installs stay, and every install refreshes the copy in each login that may connect. So after you update the checkout, run the command again with no `--login`. To stop a login's sessions from connecting, and remove the mod from it:

```sh
uv run --project gateway walkie-gateway install --remove-login ~/.claude
```

The removed login's own deny list stays in `connect.json`. If you name that login again, the install restores the list and says how many names it restored.

Each install prints the logins that may connect, how many names each one denies of its own, and how many are denied on every login.

The command also writes `~/.config/walkie-talkie/connect.json`, which the mod reads at every session start. A session on a login that file doesn't name never connects and never shows on the phone, and neither does a session with no file at all. If you edit the file by hand and leave it unreadable, such as with a trailing comma, no session connects, and the next install stops and says what's wrong rather than write over your deny lists.

### Keep folders off the phone

The deny list lives only in your own `connect.json`, never in this repository, and it starts empty. A session whose folder is denied is never listed, and its name never reaches the gateway or the phone. A name matches any folder in the session's path, and the owner or repository name of any of the folder's git remotes. So one name can cover every repository of an owner, and a copy under another folder name is still known.

```sh
# Denied on every login:
uv run --project gateway walkie-gateway install --login ~/.claude --login ~/.claude-personal --deny some-private-repo
# Denied on one login only:
uv run --project gateway walkie-gateway install --login ~/.claude --login ~/.claude-personal --deny-on ~/.claude-personal another-org
```

A name stays denied until you take it off the list it's on:

```sh
# Off the list for every login:
uv run --project gateway walkie-gateway install --allow some-private-repo
# Off one login's own list:
uv run --project gateway walkie-gateway install --allow-on ~/.claude-personal another-org
```

The mod reads the file when a session starts, so start new sessions, or type `/reload-plugins` in running ones.

## What a session does

The mod does nothing visible until a fingerprint chooses its session, which can already have taken turns. Then the session shows a short notice, the status line shows **Walkie-talkie on**, and from the next prompt on, the session follows the voice rules and reads replies aloud. That includes replies to prompts typed at the keyboard or sent through Remote Control. The rules go beside each of those prompts, hidden from the screen, so they stop when the call ends, and the system prompt and its cache stay as they were. It stays out of orchestrated workers, which have `FM_TASK_ID` set, and out of headless runs such as `claude -p`, where nobody is there.
