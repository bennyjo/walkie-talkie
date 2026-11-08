# Use Walkie Talkie

This page covers a call from Start to Stop, how the gateway decides a message has ended, the timing log, and troubleshooting.

## Start a call

1. Plug in the receiver, and connect the earbuds.
1. Open the app. It lists the live sessions, the one with the latest prompt first. Choose one.
1. Tap **Start**, allow the microphone and notifications the first time, and touch the fingerprint sensor. The prompt names the session you chose. You hear "Connected to" and its name.
1. Lock the phone, and talk.

Every Start takes a fingerprint, and the fingerprint lets the phone talk to that one session for that one call. To talk to another session, tap **Stop**, choose it, and tap **Start**.

| You | What happens |
| --- | --- |
| Stop talking | After two seconds of silence, a soft tone says your words went to the session. The reply follows. See [How a message ends](#how-a-message-ends) |
| Pause while you think | Nothing. A pause doesn't end a message, so one thought arrives as one prompt |
| Talk while the session is busy | You hear "It's in the queue." Your message runs once, in the order you said it, after the current turn. It can't confirm anything in a reply you hadn't heard yet, and the session is told so |
| Talk while a reply plays | Nothing. The microphone is ignored during a reply, so the earbuds can't feed it back in. If you're still talking when it opens, you hear the warning tone, and those words don't count, because their start is gone. Stop, then say it again. To interrupt, tap first |
| Tap an earbud | The reply stops at once, and a running spoken turn is stopped too, along with any shell commands it's running or has left in the background. A command still running after its turn has ended carries on. Messages still in the queue are dropped, and you hear that they were |
| Say only "cancel" or "never mind, cancel" | The same as a tap, and you hear "Cancelled." At a [front desk](front-desk.md), you hear "Stopped what was waiting. Anything already filed still stands," because a note already filed stays with the primary. A prompt that merely ends with the word still goes |
| Hear the warning tone with no words | The connection to the Mac dropped, the mic stopped coming from the receiver, you talked over the end of a reply, or the call ended. The app says which |
| Hear "I lost part of that" | The connection was down for more than the 10 seconds the phone keeps, or the mic stopped coming from the receiver mid-message, so that message was thrown away rather than sent with a hole in it. Say it again |
| Hear "something on screen" | The reply had code, a path, or something similar there, which isn't read out. Look, or ask what it is. Don't confirm something you haven't heard |
| Hear "hasn't started that" | The session has your message, but hasn't started it after 10 seconds. A dialog may be open at the keyboard. It runs when the dialog closes, unless you tap or stop the app |
| Hear "stopped at the keyboard" | Someone pressed Esc in that session, so it refuses spoken prompts. Say **type it** within 30 seconds to send that message through the session's own prompt box. Messages queued after it are dropped, and you hear that they were. This works only in herdr or tmux. In a plain terminal, you hear that it can't reach the terminal |
| Hear "asking for a permission" | The session needs an answer at the keyboard. Tap to stop the turn |

## End a call

To stop, tap **Stop** in the app or in its notification. The session is released: it drops the voice rules, stops sending replies to the gateway, and drops any messages still in the queue. A message it has taken but hasn't started, such as one behind a dialog, stops as it starts. A turn already running carries on, and nobody hears its reply, so tap first to stop it.

A call also ends, and the next Start takes a new fingerprint, when:

| What happens | What you notice |
| --- | --- |
| Nothing is said or heard for 30 minutes. A reply that's still coming counts as something | You hear "Nothing was said for 30 minutes, so I ended the call," then the warning tone |
| The phone has been out of reach for 2 minutes | When the phone is back, you hear the warning tone, and the app says the call ended while the phone was away |
| The session ends, or someone types `/clear` in it | You hear that the session has gone or ended the call |
| The gateway restarts or stops answering the session, or the mod reloads | The warning tone. The app says to tap **Start** again |
| You tap **Stop** with no signal | The phone can't tell the Mac, so the gateway ends the call once it has been without the phone for 2 minutes. Until then the session still shows **Walkie-talkie on** |
| Four hours pass | The app stops by itself, so a forgotten microphone can't run on |

A shorter drop in the connection keeps the call, and the phone carries on where it was.

## How a message ends

A pause doesn't end a message. Only a long enough silence does:

- **Two seconds of silence** end a message.
- **Three seconds** do if you've already paused for 0.7 seconds or more and carried on, or if you stopped on a word that usually has more after it, such as "and" or "to". You're thinking aloud.
- **A message can run for two minutes.** Past that, the transcriber starts dropping words, so the message ends there and the rest becomes the next one.

The transcriber's punctuation plays no part, because it puts a full stop on a fragment. Once you're talking, speech carries the message on unless it's far quieter than the message so far: the last 0.2 seconds must be within about 15 dB of the message's own median level. This leaves out a quieter voice nearby. In tests with synthetic voices, a voice 24 dB below yours stayed out, one 15 dB below or closer still joined, and one 18 dB below could go either way: it became its own prompt with its own tone, or its first words joined your message. A voice that close can also hold a message open and delay the reply, up to the two-minute limit. Your own speech can be left out the same way if it's far quieter than the rest of your message, especially in wind. On real wind recordings, soft speech kept in the same message went from 73 to 67 of 120 runs, with no cost at 0 or -6 dB. A pause longer than the rule allows still splits a message.

This costs time. The tone comes about 1.4 seconds later after a fluent sentence, and about 2.4 seconds later after a halting one, compared with the 0.6-second hold this replaced. A reply's first word is delayed by at least as much. The transcription starts before the silence is up, so the wait pays for only part of it.

## Timings

After each spoken message, the gateway logs one `turn-timing` line, in milliseconds after your last voiced audio. Every message gets its own line, with `queued`, `refused`, or `dropped` set if that happened. `longest_pause` is the longest pause you carried on from. `tone_sent` should be about 2,000, or 3,000 if you paused. For a session that answers straight away, `first_audio_played` should be about 4,000 to 5,000, or a second more if you paused. The gateway also logs each spoken notice by name, such as `notice queued`, and never its words. The app's screen shows the routes, the round trip to the Mac, and underruns.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `Can't listen on 100.x.y.z` | Tailscale is stopped on the Mac. Connect it |
| `Warning: Mullvad is connected` | Mullvad blocks Tailscale. Run `mullvad disconnect` |
| `Missing ... Run gateway/fetch-models.sh` | The speech models aren't downloaded yet |
| The app says "Connecting to the Mac" | Check Tailscale on both ends, and that the gateway is running |
| The app says "Can't reach your Mac" | Turn off Mullvad, check Tailscale on both ends, and check the Mac is awake |
| The app says "the gateway isn't running" | Start it on the Mac |
| The app says "Your Mac refused this phone" | The pairing was revoked, or the phone's address changed. Wait a minute, then tap **Pair again** if it still fails |
| No sessions in the app | No session has the mod running. Type `/reload-plugins` in a session that started before the install. If it says `0 plugins`, run the install for that session's login. A session in a denied folder, on a login the install didn't name, or in an orchestrated worker never shows. `0 hooks` in that line is normal |
| The fingerprint prompt says the key has gone | A fingerprint was added to the phone, or the pairing is over 30 days old. Tap **Pair again** |
| The app says "That session changed a moment ago" | Its list was a moment out of date. Tap **Start** again |
| **front-desk** isn't in the app's list | The launcher isn't running, it ran in a stripped environment, such as `env -i`, where Claude Code doesn't load mods, or its `CLAUDE_CONFIG_DIR` isn't a login you installed for. Start it from your usual shell |
| `A front desk is already running` | Another launcher holds this state folder. Stop that one first |
| "Plug in the DJI receiver" | The input route isn't USB. With Advanced Protection on, plug in the receiver before you lock the phone |
| The earbud tap pauses music instead | Another media app took the tap. Close it, and tap again |
