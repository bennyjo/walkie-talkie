# Use Walkie Talkie

This page covers a call from Start to Stop, pausing, how the gateway decides a message has ended, the timing log, and troubleshooting.

## Start a call

1. Plug in the receiver, and connect the earbuds.
1. Open the app. It lists the live sessions, the one with the latest prompt first. Choose one.
1. Tap **Start**, allow the microphone and notifications the first time, and touch the fingerprint sensor. The prompt names the session you chose. You hear "Connected to" and its name.
1. Lock the phone, and talk.

Every Start takes a fingerprint, and the fingerprint lets the phone talk to that one session for that one call. To talk to another session, tap **Stop**, choose it, and tap **Start**.

| You | What happens |
| --- | --- |
| Stop talking | After two seconds of silence, your words go to the session, with no tone. The reply follows. See [How a message ends](#how-a-message-ends) |
| Pause while you think | Nothing. A pause doesn't end a message, so one thought arrives as one prompt |
| Talk while the session is busy | You hear "It's in the queue." Your message runs once, in the order you said it, after the current turn. It can't confirm anything in a reply you hadn't heard yet, and the session is told so. That includes a turn that's working in silence, or whose reply hasn't started. Tap first to stop it |
| Talk while a reply plays | The reply goes quiet at once, on the phone. If you keep talking for about a second, as loud as your last message, it's cut, a running spoken turn stops as a tap stops it, messages still in the queue are dropped, and your words are the next message. The session is told you didn't hear all of that reply. Once a reply has played out, with nothing more on its way, what you say straight after it is an ordinary message, even a single word. Talking over a reply also applies in its first moments, so talking just as it starts may stop its turn before you hear any of it. A short word like "no", a lone "cancel" or "type it", a cough, a knock, or a quieter voice nearby doesn't count, and the reply carries on after about a second, so say a little more, or tap. Until you've spoken once since the gateway started, talking over a reply only pauses it, and the app can take a short word for earbud leak, which makes your voice harder to hear over later replies. If your voice is too soft for the phone to hear over the reply, it plays on, and if you're still talking when it ends, you hear the warning tone, and those words don't count, because their start is gone |
| Tap an earbud | The reply stops at once, and a running spoken turn is stopped too, along with any shell commands it's running or has left in the background. A command still running after its turn has ended carries on. Messages still in the queue are dropped, and you hear that they were |
| Say only "cancel" or "never mind, cancel" | The same as a tap, and you hear "Cancelled." At a [front desk](front-desk.md), you hear "Stopped what was waiting. Anything already filed still stands," because a note already filed stays with the primary. Over a reply or a notice, a lone "cancel" is too short to count, so tap, say "never mind, cancel", or wait for the sound to end. A prompt that merely ends with the word still goes |
| Say only "pause", then nothing else | Listening stops after the usual two seconds of silence, and you hear "Paused." The word never reaches the session. Until you resume, nothing anyone says reaches the session: no prompts, no yes or no to a read-back, "cancel" and "type it" do nothing, and talking over a reply stops nothing and gives the app no lesson about earbud leak. A running turn keeps going, and you hear its reply. A reply waits while someone's talking, and it can be silenced while they do. A tap still stops the reply and the turn, and it doesn't resume. See [Pause listening](#pause-listening) |
| Say only "go", "play", or "start" while paused | Listening starts again, and you hear "Listening." The word never reaches the session. Unless you're paused, these are ordinary words |
| Hear the warning tone with no words | The connection to the Mac dropped, the mic stopped coming from the receiver, you talked too softly over the end of a reply, or the call ended. The app says which |
| Hear "I lost part of that" | The connection was down for more than the 10 seconds the phone keeps, or the mic stopped coming from the receiver mid-message, so that message was thrown away rather than sent with a hole in it. Say it again |
| Hear "something on screen" | The reply had code, a path, or something similar there, which isn't read out. Look, or ask what it is. Don't confirm something you haven't heard |
| Hear "hasn't started that" | The session has your message, but hasn't started it after 10 seconds. A dialog may be open at the keyboard. It runs when the dialog closes, unless you tap or stop the app |
| Hear "stopped at the keyboard" | Someone pressed Esc in that session, so it refuses spoken prompts. Say **type it** within 30 seconds to send that message through the session's own prompt box. Over the notice, a lone "type it" is too short to count, so say "yes, type that", or wait for the notice to end. Messages queued after it are dropped, and you hear that they were. This works only in herdr or tmux. In a plain terminal, you hear that it can't reach the terminal |
| Hear "asking for a permission" | The session needs an answer at the keyboard. Tap to stop the turn |

## Pause listening

Pausing keeps a conversation with someone else out of the session. The mic stays on, so what's heard still reaches the Mac and is transcribed there to listen for the words that resume. None of it goes to the session or the log. Anyone's voice can resume: a lone "go" from someone nearby counts, so listen for "Listening."

If you don't hear "Paused." or "Listening.", the word was missed, so say it again. The transcriber can miss a word this short, or hear something else, and more often over a reply. If you say a second word before the first confirmation goes out, you hear only the second confirmation, so the last thing you hear is always the current state.

"Pause it" and "paused" count as "pause", because the transcriber can hear a lone "pause" either way. As a result, a lone "pause it" or "paused" meant for the session pauses listening instead of reaching it.

A read-back waiting for a yes ends when you pause, as it does for any other message, so after you resume there's nothing to say yes to. Ask again.

**Over a reply,** "pause" and the resume words are too short to count as talking over it. The reply goes quiet for about a second. If the word is caught, about as loud as your last message, and not a word the reply has just said, you hear the confirmation ahead of the rest of the reply. Then the reply carries on, and its turn keeps running. If the word is missed, the reply carries on and nothing else changes, so say it again, or wait for the reply to end. Speech that carries on past a missed "pause" can still count as talking over the reply, as it does for any other speech: the reply is cut, its turn stops, and your words are the next message.

**A pause lasts until you resume,** because the gateway saves it in its state folder:

- **A short drop in the connection:** you stay paused, and you hear nothing more about it.
- **A new call, after Stop, a gateway restart, the four-hour limit, or any other end of a call:** you stay paused, and when the phone connects you hear "Still paused. Say go to start listening." After a gateway restart, it doesn't know how loud you are until you've sent a message, so a resume word over a reply doesn't count. Wait for the reply to end.
- **30 minutes paused:** speech heard while paused doesn't count as something said, so the call ends as it does after 30 minutes of silence.

The app's screen doesn't show the pause: go by what you hear. No app update is needed.

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

The transcriber's punctuation plays no part, because it puts a full stop on a fragment. Once you're talking, speech carries the message on unless it's far quieter than the message so far: the last 0.2 seconds must be within about 15 dB of the message's own median level. This leaves out a quieter voice nearby. In tests with synthetic voices, a voice 24 dB below yours stayed out, one 15 dB below or closer still joined, and one 18 dB below could go either way: it became its own prompt, or its first words joined your message. A voice that close can also hold a message open and delay the reply, up to the two-minute limit. Your own speech can be left out the same way if it's far quieter than the rest of your message, especially in wind. On real wind recordings, soft speech kept in the same message went from 73 to 67 of 120 runs, with no cost at 0 or -6 dB. A pause longer than the rule allows still splits a message.

This costs time. Your words reach the session about 1.4 seconds later after a fluent sentence, and about 2.4 seconds later after a halting one, compared with the 0.6-second hold this replaced. A reply's first word is delayed by at least as much. The transcription starts before the silence is up, so the wait pays for only part of it.

## Timings

After each spoken message, the gateway logs one `turn-timing` line, in milliseconds after your last voiced audio, so marks before that are negative. Every message gets its own line, with `queued`, `refused`, or `dropped` set if that happened. `longest_pause` is the longest pause you carried on from. `speech_detected` is when the gateway decided you were talking, and `transcribe_start` is when the final transcription began. `submitted` should be about 2,000, or 3,000 if you paused. For a session that answers straight away, `first_audio_played` should be about 4,000 to 5,000, or a second more if you paused. `last_voice_at` is that moment on the Mac's monotonic clock, in seconds. The gateway also logs each spoken notice by name, such as `notice queued` or `notice paused`, and never its words.

The app's screen shows the routes, which mic channels carry sound, the round trip to the Mac, and underruns. It also shows the mic's loudest level over the last second, its background, and how loud the earbuds leak, all in dB under full scale in the speech band, and how many times you talked over a reply and how many of those resumed.

## Voices

Pick a voice under **Voice** in the app, once you've tapped **Start**. All 11 come with the Kokoro model, the gateway remembers your choice in its state folder, and it takes effect from the next reply.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `Can't listen on 100.x.y.z` | Tailscale is stopped on the Mac. Connect it |
| `Missing ... Run gateway/fetch-models.sh` | The speech models aren't downloaded yet |
| The app says "Connecting to the Mac" | Check Tailscale on both ends, and that the gateway is running |
| The app says "Can't reach your Mac" | Turn off any VPN that blocks Tailscale, check Tailscale on both ends, and check the Mac is awake |
| The app says "the gateway isn't running" | Start it on the Mac |
| The app says "Your Mac refused this phone" | The pairing was revoked, or the phone's address changed. Wait a minute, then tap **Pair again** if it still fails |
| No sessions in the app | No session has the mod running. Type `/reload-plugins` in a session that started before the install. If it says `0 plugins`, run the install for that session's login. A session in a denied folder, on a login the install didn't name, or in an orchestrated worker never shows. `0 hooks` in that line is normal |
| The fingerprint prompt says the key has gone | A fingerprint was added to the phone, or the pairing is over 30 days old. Tap **Pair again** |
| The app says "That session changed a moment ago" | Its list was a moment out of date. Tap **Start** again |
| **front-desk** isn't in the app's list | The launcher isn't running, it ran in a stripped environment, such as `env -i`, where Claude Code doesn't load mods, or its `CLAUDE_CONFIG_DIR` isn't a login you installed for. Start it from your usual shell |
| `A front desk is already running` | Another launcher holds this state folder. Stop that one first |
| "Connect your USB mic" | The input route isn't USB. With Advanced Protection on, connect the mic before you lock the phone |
| Your words aren't picked up | Read **Mic level** in the app while you talk. Speech needs to reach about -45 dB, and 12 dB over the background. If it's lower, raise the mic's gain, or turn off its noise cancelling |
| A reply doesn't stop when you talk over it | Keep talking for about a second, as loud as your last message. Your voice also has to beat **earbud leak** in the app by about 8 dB. If you've just spoken much closer to the mic, say something first in your normal voice. Otherwise, lower the volume, or tap |
| A reply keeps pausing for a moment or cuts off on its own, or a stray message reaches the session | Loud leak or a noisy place. The app learns the leak as replies play, but it can take a dozen pauses in the first reply after it starts. An echo that lasts past about half a second usually teaches it nothing, and neither does one about as loud as your last message. The exception is a talk-over of four words or fewer that cuts the reply and repeats what just played: it teaches the leak even at that level, and even if it was you. Until you've spoken once since the gateway started, any shorter speech counts as leak, even your own voice. If it goes on, lower the volume |
| The earbud tap pauses music instead | Another media app took the tap. Close it, and tap again |
