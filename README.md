# Walkie Talkie

Talk with your live Claude Code session, such as [Firstmate](https://github.com/kunchenguid/firstmate), from your phone and earbuds while you're away from the desk. No keyboard, no screen.

<!-- DEMO GIF PLACEHOLDER: a short clip of a walk, talking to a Firstmate session, goes here. -->

## What it feels like

You leave the desk with Firstmate halfway through a batch of work. Earbuds in, a small mic clipped to your collar, phone in your pocket.

On the way out, you open Walkie Talkie, tap your Firstmate session, and touch the fingerprint sensor. You hear "You're talking to" and the session's name. You lock the phone and walk.

Then you just talk. "What's everyone working on?" When you stop, your words go through, a quick "Pondering." tells you they arrived, and Firstmate's answer plays in your earbuds. "Tell the README task to lead with the use case." If Firstmate is still busy, you hear "It's in the queue," and your message runs after the current turn. Talk over a reply to cut it short, as in a phone call, or tap an earbud. Either one stops the turn. If you stop to chat with someone, say "pause", and "go" when you're done.

If you've set up a front desk, you can talk to it while Firstmate is deep in a long turn. It answers from Firstmate's status and files everything else for Firstmate to pick up. When Firstmate wants to merge, it reads the exact action back, and you hear it word for word. You say "yes", take out the phone, and confirm with your fingerprint. A voice alone confirms nothing there.

To talk to another session, such as one on your personal login, tap it in the app, or say "switch session". The app groups sessions by login, and names a Firstmate secondmate by the secondmate it is. Either way, you confirm with your fingerprint, as when you started, and you hear the name of the session you're now talking to.

You never type. Apart from starting a call, a switch, and a confirmation, the phone stays in your pocket.

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
collar mic --> USB receiver --> phone app --(encrypted, over Tailscale)--> Mac gateway --(Unix socket)--> mod in a Claude Code session
earbuds <----------------------- phone app <---------------- Mac gateway <---------------- reply
```

Your words reach the session as a normal prompt, so the session works as it does when you type. The mod stays out of orchestrated workers (sessions with `FM_TASK_ID` set), so the phone lists your Firstmate session and not every crewmate.

## What you need

- A Mac on Apple silicon with [uv](https://docs.astral.sh/uv/). The speech models take about 1 GB, and downloading them needs about 2 GB free.
- [Tailscale](https://tailscale.com/) connected on the Mac and the phone. A VPN that blocks Tailscale has to be off.
- Claude Code 2.1.318 or later.
- An Android phone on Android 13 or later, with a fingerprint set up.
- A mic and a way to hear replies. The best is a USB audio input for the phone, such as a wireless collar mic's USB receiver, with Bluetooth earbuds. Bluetooth headphones with their own mic work too, at call quality, and so does the phone's own mic, with headphones or on the phone's speaker. See [Choose a mic](docs/usage.md#choose-a-mic).

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

1. Plug the mic's receiver into the phone, connect the earbuds, and open the app. To use another mic, choose it under **Mic**. Tap a session, and touch the fingerprint sensor. Then lock the phone and talk.

To keep some folders off the phone, to set up a front desk, or for everything else, see [Documentation](#documentation).

## Change how it talks

To change how fast the voice talks, how long it pauses between sentences and sections, or how the session words its replies, write `~/.config/walkie-talkie/talk.md` on the Mac. Its front matter sets the speed and pauses, and its text is the session's style rules, like an `AGENTS.md` for talking. You can also ask the session by voice, for example "talk slower from now on", and it updates the file for you. The setting holds for every reply, in every session. For the settings and the default rules, see [How it talks](docs/usage.md#how-it-talks).

## Security

A live session can do anything you can do at its keyboard, so getting into one should be as hard as getting into your bank account. When in doubt, Walkie Talkie does what a banking app does.

**Getting in.** To talk to a session, someone needs all of these at once: a device on your tailnet, at the paired phone's address, holding the paired app's secret token, and your fingerprint on that phone's hardware key, for that session, at the start of that call.

- **A fingerprint at every call and every switch.** It unlocks a key in the phone's secure hardware for one signature. The gateway and the session both check it before the session takes a spoken prompt.
- **Private network only.** The gateway listens only on the Mac's Tailscale address, and accepts only the one paired phone. No port faces the internet. The app dials the Mac only through the VPN network, so with Tailscale off on the phone, it sends nothing.
- **The call is encrypted by the app and the gateway themselves.** Every frame after the login is encrypted and authenticated with a key made for that one connection from the pairing token and both sides' login nonces. Tailscale is a second layer around it, not the only one.
- **Only the sessions you allow.** The mod connects only on the logins you name, and you can keep folders and repositories off the phone.
- **Speech stays on your devices.** It's recognized and spoken on your Mac. The phone speaks only a few fixed warnings of its own, in Android's offline voice. There's no cloud speech service, crash reporter, or analytics, and nothing you say is logged. Claude Code sends your prompts and replies to Anthropic, as it does when you type.
- **Calls end.** At Stop, when you say "hang up", the moment the Bluetooth headphones disconnect, after 30 minutes with nothing said, after 2 minutes without the phone, and after four hours at most. The next call takes a new fingerprint.
- **The gateway's own words start with a tone.** Notices such as "Paused." play after a short tone that only the phone makes, in the voice you chose for that session. A reply never gets that tone. The tone marks where a notice starts, not where it ends: a notice is a sentence or two, and anything after it may be a reply.

**Once you're in.** These are the limits. Read them before you rely on this.

- **Your voice has your keyboard's authority.** A spoken prompt does whatever a typed one would in that session, tools included. In a session that runs without permission prompts, that's everything. Connect the sessions you'd hand a keyboard to.
- **The mic can't tell who's speaking.** It's live, with no wake word. Anyone close enough can become a prompt. You can [enroll your voice](docs/usage.md#listen-only-to-your-voice) so the gateway drops messages that don't sound like you, but it only logs scores until you turn on enforcing, and even then it's a filter, not a lock: it doesn't stop a recording of your voice, or a voice close to yours. Say "pause", or stop the app, when you talk to someone else.
- **A phone taken during a call** can talk to that one session only while your Bluetooth headphones stay connected. The call ends at once when they disconnect, including when their battery runs out. A thief who stays within radio range of your headphones can keep talking until you say "hang up", for up to four hours. A call on the phone's own mic and speaker has no headphones to lose, so it carries on in a thief's hands until 30 minutes pass with nothing said. It can't start another call or switch sessions.
- **A spoken yes is not a lock.** Only the fingerprint confirmation at a front desk is, and the session that receives it still acts by reading a note.
- **A compromised Tailscale account** could put a device between the phone and the Mac. It could see that a call is on, and how much is said, and cut it, but it couldn't read a call, add to it, or start one without the token and your fingerprint. Use two-factor sign-in for Tailscale.
- **Programs running as you on the Mac** can reach the gateway's private folder. They can relabel a session, speak in Claude's voice, or grant themselves a session. Such a program can already type into your terminals.

**If you lose your phone,** remove it in the [Tailscale admin console](https://login.tailscale.com/admin/machines) from any browser. That cuts it off at once, wherever you are. Then run this on the Mac:

```sh
uv run --project gateway walkie-gateway revoke
```

**Compared with the Claude app's voice mode:** your audio never leaves your own devices, and every call takes a fingerprint, where the app takes a sign-in that lasts for weeks. In exchange, a voice here reaches a session that can run commands on your Mac, where the app's voice mode reaches the tools you've connected to your account. Anthropic is an audited vendor. This is version 0.1.0 by one author, and nobody has audited it.

The full model is in [docs/security.md](docs/security.md).

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
| [Use it](docs/usage.md) | What happens when you talk, pause, tap, or go quiet, switching sessions, the speaker check, how a message ends, timings, how it talks, and troubleshooting |
| [Front desk](docs/front-desk.md) | A small session that answers while your main session is busy, and the fingerprint confirmation for irreversible actions |
| [Security](docs/security.md) | The full security model and its limits |
| [Develop](docs/develop.md) | Tests, builds, the code map, and testing without a phone |

## License

Walkie Talkie is licensed under the [Apache License 2.0](LICENSE).
