# Security

A live session can do anything you can do at its keyboard, so getting into one should be as hard as getting into your bank account. Banking apps are the baseline here: when in doubt, this does what a banking app does.

To talk to a session, someone needs all of these at once: a device on your tailnet, at the paired phone's address, holding the paired app's token, and your fingerprint on that phone's hardware key, for that session, at that Start.

- **A fingerprint at every Start.** Tapping **Start** asks for a strong fingerprint, with no PIN instead. It unlocks a key in the phone's secure hardware for one signature, over a fresh challenge from the gateway, this call, the session's ID, and a challenge that session made itself. The session's name in the prompt comes from the gateway's list, and isn't signed. The key never leaves the phone, and it works only while the phone is unlocked.
- **The session checks it too.** The gateway checks the signature, and so does the mod in the chosen session, against the phone's public key. A session takes no spoken prompt without that check, so an older gateway can't choose it. The session keeps the call only while each answer from the gateway names the call the fingerprint signed, and lets go the moment the gateway stops answering. So something that answers on the socket without having asked the gateway, such as a listener after the gateway stops, can't ride the call. A process running as you can still get in: see [What this can't stop](#what-this-cant-stop).
- **A call ends, and the next one takes a new fingerprint.** At Stop, after 30 minutes with nothing said, after 2 minutes without the phone, at `/clear`, when the gateway restarts or stops answering, when the mod reloads, and after four hours at most. The session also lets go after 31 minutes with nothing said, in case a gateway forgets to.
- **Pairing lasts 30 days.** Then the phone pairs again at the Mac. Adding a fingerprint to the phone destroys its key, so that ends the pairing too.
- **Only the logins and folders you allow.** The mod connects only on the logins `connect.json` names. A session in a denied folder never shows on the phone, and its name never leaves the session. A login whose path holds a denied name shows without a name.
- **The list shows only names.** Before a fingerprint, the app can see each session's folder name and the folders above it, a short ID, a short name for its login, and when it last took a prompt. It can't talk, hear, or choose. The screen can't be captured, and it stays out of the recent-apps preview.
- **A confirmation needs the app.** A spoken yes alone confirms nothing at a front desk. Your fingerprint signs the exact words you heard.
- **Private network only.** The gateway binds to the Mac's Tailscale address, and it refuses any address but a Tailscale or loopback one. It has no fallback to all interfaces, and nothing in this project runs `tailscale funnel`, `serve`, `up`, or `down`.
- **One paired phone.** After pairing, only that phone's Tailscale address may try to log in.
- **Mutual login.** The phone and the gateway each prove they hold a 256-bit token, with HMAC-SHA256 over fresh nonces. The token crosses the connection once, at pairing, inside Tailscale's encryption. The phone sends no audio until the gateway has proved itself. On the phone, the token is encrypted under another key in the secure hardware.
- **Limits on guessing.** A login has 5 seconds and 2 KB. Five failures in a minute, wrong signatures included, close the door for a minute. A pairing code allows three tries.
- **The session link is private.** The mod talks over a Unix socket in a folder only you can enter: `~/.local/state/walkie-talkie/`, mode 0700. The token and key files there are mode 0600.
- **Nothing leaves your devices but what Claude Code already sends.** Speech is recognized and synthesized on the Mac. Audio and words go only between the Mac and the paired phone, inside the tailnet, and between the gateway and each session, on that socket. There's no cloud speech service, crash reporter, or analytics, and the app has no libraries. Claude Code sends your prompts and replies to Anthropic, as it does when you type.
- **No logs of what you say.** The gateway logs timings, connection events, the levels and counts of each pause in a reply, and the names of spoken notices, and never audio or words. The app logs neither. This also holds while [paused](usage.md#pause-listening).
- **The phone's own mic and speaker stay out of it.** The app sends sound only while the input route is USB, and plays sound only while the output route is Bluetooth. Otherwise it warns you. It stays out of call mode.
- **The installed mod is a copy.** It's read-only, and it changes only when you run the install again.
- **The front desk can't act.** It has no tools. It runs only the three commands you configured, with no shell, and spoken words reach only the `handoff` command, as one argument. It sends the status and the talk to Anthropic for each question, as any session sends what it reads, so choose a `status` command whose output you're happy to send.
- **Downloads are pinned.** Models and build tools are checked against checksums, and `gateway/uv.lock` pins Python packages by hash.
- **The app keeps its secrets to itself.** Its storage is private and excluded from backups and device transfers.

## What spoken words can do

Within a call, your spoken words have the same authority as your keyboard: whatever the session would do for a typed prompt, it does for a spoken one, tools included. A prompt never starts with `/`, `!`, `#`, or `@`, and the mod approves nothing and can't type into an open dialog, but the model acts on what it hears.

This is a live microphone with no wake word, so anything it hears can become a prompt, including you talking to a shopkeeper, or someone near you. It can't tell who's speaking. A close voice can start a message, or join yours. The gateway requires speech to be 12 dB above the background in the speech band, and louder than -55 dBFS, to start a message, and within about 15 dB of the message's own level to carry it on. That has to let a clipped-on mic through, so in a quiet place a clear voice nearby can pass. While a reply plays, only a voice louder than the earbuds' leak and about as loud as your last message can start anything, or stop a turn. That keeps out a clearly quieter voice nearby, but it's a simple level check, so a voice about 5 dB quieter than yours or closer can still do both. Say "pause", or stop the app, when you're talking to someone else. A confirmation the voice rules ask for is a rule the model is asked to follow, not a lock. The app's fingerprint confirmation at a front desk is a lock at the gateway, which files a note that starts "Confirmed in the app" only after the signature checks out. The primary still decides by reading the note, and a voice note that says those words differs only in how it starts.

The reply's own sound can also become a prompt, though that should be rare. Sound already on its way to the earbuds can't be recalled, so speech counts as you only if it carries on past about half a second after the reply goes quiet. If your earbuds are slower than that, or leak about as loud as your voice, an echo can still cut the reply and reach the session as a stray prompt. Speech over a reply is dropped as echo if it has five or more words that all repeat, in order, words that played in the last four seconds, but the check judges only what it has heard so far, so a longer repeat can also count as you. The gateway logs one `hold` line for each pause, with levels and counts and no words, so a walk can show whether this happens.

## If you lose your phone

Do both steps, from the Mac or from any browser and a terminal on the Mac:

1. In the [Tailscale admin console](https://login.tailscale.com/admin/machines), remove the phone. It can no longer reach the Mac.
1. On the Mac, run:

   ```sh
   uv run --project gateway walkie-gateway revoke
   ```

   This works whether or not the gateway is running. A running gateway ends the call and closes the phone's connection at once. The pairing is gone, so the phone is refused from then on, even if it gets back on the tailnet. If you started the gateway with `--state` or `WALKIE_TALKIE_STATE`, give `revoke` the same folder. When no gateway answers in the folder it used, `revoke` names that folder, so you can check it's the right one.

To use a phone again, pair it with `walkie-gateway serve --pair`.

## What this can't stop

- **A thief who takes the phone during a call, locked or not,** can talk to that one session until the call ends: once nothing has been said for 30 minutes, after four hours at most, or at once when you revoke. They can't start another call without your fingerprint.
- **Processes running as you on the Mac.** Any of them can read the socket's folder, so it can hear spoken words as text and speak in Claude's voice to your earbuds. It can also replace the phone's public key and grant itself a session, capture a grant at Start or ride a live call by polling the gateway in the session's name, or relay the gateway's traffic. Such a process can already read your sessions' transcripts and type into your terminals, so this adds little.
- **A compromised Tailscale account** could reach the gateway's port. It would still need the token and your fingerprint to do anything. Use two-factor sign-in for Tailscale.
