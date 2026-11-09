# Walkie Talkie

Talk with your live Claude Code session, such as [Firstmate](https://github.com/kunchenguid/firstmate), from your phone and earbuds while you're away from the desk. No keyboard, no screen.

<!-- DEMO GIF PLACEHOLDER: a short clip of a walk, talking to a Firstmate session, goes here. -->

## What it feels like

You leave the desk with Firstmate halfway through a batch of work. Earbuds in, a small mic clipped to your collar, phone in your pocket.

On the way out, you open Walkie Talkie, choose your Firstmate session, tap **Start**, and touch the fingerprint sensor. You hear "Connected to" and the session's name. You lock the phone and walk.

Then you just talk. "What's everyone working on?" When you stop, your words go through, a quick "Pondering." tells you they arrived, and Firstmate's answer plays in your earbuds. "Tell the README task to lead with the use case." If Firstmate is still busy, you hear "It's in the queue," and your message runs after the current turn. Talk over a reply to cut it short, as in a phone call, or tap an earbud. Either one stops the turn. If you stop to chat with someone, say "pause", and "go" when you're done.

If you've set up a front desk, you can talk to it while Firstmate is deep in a long turn. It answers from Firstmate's status and files everything else for Firstmate to pick up. When Firstmate wants to merge, it reads the exact action back, and you hear it word for word. You say "yes", take out the phone, and confirm with your fingerprint. A voice alone confirms nothing there.

You never type. Apart from Start and a confirmation, the phone stays in your pocket.

## Who it's for

- You run Firstmate, or another long-lived Claude Code session, on a Mac, and you want to keep steering it when you step away.
- You've tried to drive it from a phone over SSH, and the phone keyboard is the slow part.
- You want your voice to stay on your own machines: speech is recognized and spoken on your Mac, with no cloud speech service.
- You have an Android phone. There's no iPhone app yet.

## How it works

Three pieces, all in this repository:

| Piece | Where it runs | What it does |
| --- | --- | --- |
| Phone app, `android/` | Your Android phone | Streams your voice to the Mac and plays the replies in your earbuds, with the phone locked. Holds the fingerprint-protected key |
| Gateway, `gateway/` | Your Mac | Hears when you've finished talking, turns speech into text and replies into speech, and routes each message to the session you chose |
| Mod, `mod/walk-voice/` | Inside each Claude Code session | Lists the session on the phone, checks your fingerprint, sends your words in as your own prompt, and passes the reply to the gateway to be spoken |

```text
collar mic --> USB receiver --> phone app --(Tailscale)--> Mac gateway --(Unix socket)--> mod in a Claude Code session
earbuds <----------------------- phone app <---------------- Mac gateway <---------------- reply
```

Your words reach the session as a normal prompt, so the session works as it does when you type. The mod stays out of orchestrated workers (sessions with `FM_TASK_ID` set), so the phone lists your Firstmate session and not every crewmate.

## What you need

- A Mac on Apple silicon with [uv](https://docs.astral.sh/uv/). The speech models take about 1 GB, and downloading them needs about 2 GB free.
- [Tailscale](https://tailscale.com/) connected on the Mac and the phone. A VPN that blocks Tailscale has to be off.
- Claude Code 2.1.318 or later.
- An Android phone on Android 13 or later, with a fingerprint set up.
- A USB audio input for the phone, such as a wireless collar mic's USB receiver.
- Bluetooth earbuds.

## Quick start

Clone the repository, and run every command from its root:

```sh
git clone https://github.com/bennyjo/walkie-talkie.git
cd walkie-talkie
```

1. On the Mac, download the speech models, which checks their checksums, and start the gateway in pairing mode:

   ```sh
   gateway/fetch-models.sh
   uv run --project gateway walkie-gateway serve --pair
   ```

   The gateway prints the Mac's Tailscale address and an 8-digit pairing code that works once, for two minutes. Leave it running. Next time, start it without `--pair`.

1. Build the phone app. The build needs no Gradle: it downloads a JDK and Android's build tools into `android/.toolchain/` and checks their checksums.

   ```sh
   android/build.sh
   ```

   Install `android/out/walkie-talkie.apk` on the phone, for example with `adb install android/out/walkie-talkie.apk`.

1. On the phone, go to **Settings > Apps > Walkie Talkie > App battery usage**, and choose **Unrestricted**.

1. Open **Walkie Talkie**, enter the Mac's Tailscale address and the pairing code, and tap **Pair**.

1. Install the mod into each Claude Code login whose sessions may connect. A login is the folder that `CLAUDE_CONFIG_DIR` points at, or `~/.claude` when it isn't set. If Firstmate runs on another login, name that one.

   ```sh
   uv run --project gateway walkie-gateway install --login ~/.claude
   ```

   Sessions started after this load the mod by themselves. In a session that was already running, type `/reload-plugins` once.

1. Plug the mic's receiver into the phone, connect the earbuds, and open the app. Choose a session, tap **Start**, and touch the fingerprint sensor. Then lock the phone and talk.

To keep some folders off the phone, to set up a front desk, or for everything else, see [Documentation](#documentation).

## Security

A live session can do anything you can do at its keyboard, so getting into one should be as hard as getting into your bank account. When in doubt, Walkie Talkie does what a banking app does.

To talk to a session, someone needs all of these at once: a device on your tailnet, at the paired phone's address, holding the paired app's secret token, and your fingerprint on that phone's hardware key, for that session, at that Start.

- **A fingerprint at every Start.** The fingerprint unlocks a key in the phone's secure hardware for one signature. Both the gateway and the session check it before the session takes a single spoken prompt.
- **Calls end on their own.** At **Stop**, after 30 minutes with nothing said, after 2 minutes without the phone, and after four hours at most. The next call takes a new fingerprint.
- **Private network only.** The gateway listens only on the Mac's Tailscale address, accepts only the one paired phone, and both sides prove they hold the token before any audio flows.
- **Only the sessions you allow.** The mod connects only on the logins you name, and you can deny folders and repositories so they never show on the phone.
- **Speech stays on your Mac.** No cloud speech service, crash reporter, or analytics. The gateway never logs audio or words. Claude Code sends your prompts and replies to Anthropic, as it does when you type.

What this can't stop:

- **Spoken words have your keyboard's authority.** Within a call, the session does whatever it would do for a typed prompt, tools included.
- **The mic is always live, with no wake word.** It can't tell who's speaking, so anyone close enough can become a prompt. Say "pause", or stop the app, when you talk to someone else.
- **A spoken yes to a session is not a lock.** A session asked to confirm follows a rule, not a check. The fingerprint confirmation at a front desk is a check.
- **A thief who takes the phone during a call** can talk to that one session until the call ends. They can't start another call without your fingerprint.
- **Processes running as you on the Mac** can reach the gateway's private folder and talk to a session. Such a process can already type into your terminals.
- **A compromised Tailscale account** could reach the gateway's port. It would still need the token and your fingerprint. Use two-factor sign-in for Tailscale.

If you lose your phone, remove it in the [Tailscale admin console](https://login.tailscale.com/admin/machines), then run this on the Mac:

```sh
uv run --project gateway walkie-gateway revoke
```

The full model, including pairing, rate limits, and file permissions, is in [docs/security.md](docs/security.md).

## Status

Early: version 0.1.0.

- **Android only.** There's no iPhone app.
- **Mac on Apple silicon only** for the gateway.
- **The mod needs Claude Code 2.1.318 or later.** The mods API can still change between Claude Code releases.
- **One paired phone per Mac.** A pairing lasts 30 days, then you pair again.

## Documentation

| Page | What's in it |
| --- | --- |
| [Set up](docs/setup.md) | Every setup step, updating the mod, and keeping folders off the phone |
| [Use it](docs/usage.md) | What happens when you talk, pause, tap, or go quiet, how a message ends, timings, and troubleshooting |
| [Front desk](docs/front-desk.md) | A small session that answers while your main session is busy, and the fingerprint confirmation for irreversible actions |
| [Security](docs/security.md) | The full security model and its limits |
| [Develop](docs/develop.md) | Tests, builds, the code map, and testing without a phone |

## License

Walkie Talkie is licensed under the [Apache License 2.0](LICENSE).
