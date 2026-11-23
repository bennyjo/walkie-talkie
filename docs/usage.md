# Use Walkie Talkie

This page covers a call from Start to Stop, switching sessions, pausing, the speaker check, how the gateway decides a message has ended, the timing log, how it talks, and troubleshooting.

## Start a call

1. Plug in the receiver, and connect the earbuds.
1. Open the app. It lists the live sessions in groups, one for each Claude Code login, under a heading such as **Work account** or **Personal account**. Within a group, the session with the latest prompt comes first. Each row shows the session's name, and under it where the folder is, when the session last took a prompt, and a short ID. A session goes by its folder's name, except in a Firstmate secondmate home, which goes by the secondmate it declares, such as "secondmate walkie". The main Firstmate goes by its folder's name, "firstmate". The app, the fingerprint prompt, and what you hear all name the session the same way, with its account, such as "secondmate walkie on the personal account". To name a login, see [Name each account](setup.md#name-each-account). Choose a session.
1. Tap **Start**, allow the microphone and notifications the first time, and touch the fingerprint sensor. The prompt names the session you chose. You hear "Connected to" and its name.
1. Lock the phone, and talk.

Every Start takes a fingerprint, and the fingerprint lets the phone talk to that one session for that one call. To talk to another session, [switch](#switch-sessions): choose it in the app, or say **switch session**.

| You | What happens |
| --- | --- |
| Stop talking | After two seconds of silence, your words go to the session. As soon as the session has them, you hear one thinking word, such as "Working." or "Pondering.", so you know they arrived. The reply follows. If they can't reach the session, you hear the warning tone and why instead. See [How a message ends](#how-a-message-ends) and [Turn off the thinking word](#turn-off-the-thinking-word) |
| Pause while you think | Nothing. A pause doesn't end a message, so one thought arrives as one prompt |
| Talk while the session is busy | You hear "It's in the queue." Your message runs once, in the order you said it, after the current turn. It can't confirm anything in a reply you hadn't heard yet, and the session is told so. That includes a turn that's working in silence, or whose reply hasn't started. Tap first to stop it |
| Talk while a reply plays | The reply goes quiet at once, on the phone. If you keep talking for about a second, as loud as your last message, it's cut, a running spoken turn stops as a tap stops it, messages still in the queue are dropped, and your words are the next message. The session is told you didn't hear all of that reply. Once a reply has played out, with nothing more on its way, what you say straight after it is an ordinary message, even a single word. Talking over a reply also applies in its first moments, so talking just as it starts may stop its turn before you hear any of it. A short word like "no", a lone "cancel" or "type it", a cough, a knock, or a quieter voice nearby doesn't count, and the reply carries on after about a second, so say a little more, or tap. Until you've spoken once since the gateway started, talking over a reply only pauses it, and the app can take a short word for earbud leak, which makes your voice harder to hear over later replies. If your voice is too soft for the phone to hear over the reply, it plays on, and if you're still talking when it ends, you hear the warning tone, and those words don't count, because their start is gone |
| Tap an earbud | The reply stops at once, and a running spoken turn is stopped too, along with any shell commands it's running or has left in the background. A command still running after its turn has ended carries on. Messages still in the queue are dropped, and you hear that they were |
| Say only "cancel" or "never mind, cancel" | The same as a tap, and you hear "Cancelled." At a [front desk](front-desk.md), you hear "Stopped what was waiting. Anything already filed still stands," because a note already filed stays with the primary. Over a reply or a notice, a lone "cancel" is too short to count, so tap, say "never mind, cancel", or wait for the sound to end. A prompt that merely ends with the word still goes |
| Say only "pause", then nothing else | Listening stops after the usual two seconds of silence, and you hear "Paused." The word never reaches the session. Until you resume, nothing anyone says reaches the session: no prompts, no yes or no to a read-back, "cancel" and "type it" do nothing, and talking over a reply stops nothing and gives the app no lesson about earbud leak. A running turn keeps going, and you hear its reply. A reply waits while someone's talking, and it can be silenced while they do. A tap still stops the reply and the turn, and it doesn't resume. See [Pause listening](#pause-listening) |
| Say only "go", "play", or "start" while paused | Listening starts again, and you hear "Listening." The word never reaches the session. Unless you're paused, these are ordinary words |
| Hear the warning tone, then why a message didn't reach the session | It couldn't send it, the session didn't pick it up in 10 seconds, or the session was stopped at the keyboard. The words after the tone say which |
| Hear the warning tone with no words | The connection to the Mac dropped, the mic stopped coming from the receiver, you talked too softly over the end of a reply, or the call ended. The app says which |
| Hear a short rising tone, then words | That's the gateway speaking, not the session, in its own voice. A reply never comes with that tone |
| Hear "I lost part of that" | The connection was down for more than the 10 seconds the phone keeps, or the mic stopped coming from the receiver mid-message, so that message was thrown away rather than sent with a hole in it. Say it again |
| Hear "something on screen" | The reply had code, a path, or something similar there, which isn't read out. Look, or ask what it is. Don't confirm something you haven't heard |
| Hear "hasn't started that" | The session has your message, but hasn't started it after 10 seconds. A dialog may be open at the keyboard. It runs when the dialog closes, unless you tap or stop the app |
| Hear "stopped at the keyboard" | Someone pressed Esc in that session, so it refuses spoken prompts. Say **type it** within 30 seconds to send that message through the session's own prompt box. Over the notice, a lone "type it" is too short to count, so say "yes, type that", or wait for the notice to end. Messages queued after it are dropped, and you hear that they were. This works only in herdr or tmux. In a plain terminal, you hear that it can't reach the terminal |
| Hear "asking for a permission" | The session needs an answer at the keyboard. Tap to stop the turn |
| Hear the warning tone, then "Didn't catch your voice. Say it again." | The [speaker check](#listen-only-to-your-voice) refused the message, so it didn't reach the session. Say it again, with a little more if it was a single word |

While a reply plays or you speak, the phone lowers other audio, such as music, so you hear the voice over it. The audio comes back to full volume about two seconds after both stop.

## Switch sessions

You can move the call to another session without stopping it, in the app or by voice. The mod must run in that session too, so it's in the app's list. Sessions on different logins work the same way, so you can switch between a session on your work login and one on your personal login.

Like Start, a switch takes your fingerprint, and the fingerprint signs that one session. The session you leave is let go as at **Stop**: messages still in its queue are dropped, and you hear that they were. A turn already running there carries on, and nobody hears its reply.

### In the app

During a call, the app lists the sessions, with the one you're talking to selected.

1. Choose another session. The fingerprint prompt names it.
1. Touch the sensor.
1. You hear "Connected to" and its name. The list selects it, and the top of the screen says "Talking to" and its name.

If you cancel the fingerprint, you hear "Not switched" and the name of the session you're still talking to. A switch in the app needs the gateway from the same version as the app. With an older gateway, the screen says the call didn't move.

### By voice

1. Say **switch session** to go to the only other session, or **switch to** and the session's name, with or without its account, such as "switch to secondmate walkie" or "switch to firstmate personal".
1. You hear "To switch to" and the session's name, and a notification says something is waiting for your fingerprint. Open the app: it asks "Switch to" and the name. Tap **Confirm** and touch the sensor, or tap **Not now**.
1. You hear "Connected to" and the name of the session you're now talking to.

A spoken switch alone moves nothing.

| You say | What happens |
| --- | --- |
| **switch session** with more than one other session | You hear "Which session?" and their names. Say **switch to** and one of them |
| **switch session** with no other session | You hear that there's no other session to switch to |
| **switch to** and a name that matches no other session's whole name, such as "switch to the main branch", "switch to personal", or "switch to main" on a session in a folder called main | It's not a switch. Your words go to the session as an ordinary prompt |
| **yes** or **no** before the fingerprint | The switch is withdrawn, and you hear "Not switched". The word doesn't reach the session you're on |
| Anything else before the fingerprint, or tap an earbud | The switch is withdrawn, and nothing moves. Your words, even "yeah" or "okay", go to the session you're on |
| A front desk's reply that asks for a yes plays before the fingerprint | The switch is withdrawn, so your next **yes** answers the reply |

The transcriber often hears "switch session" as "swit session", so that counts too. It can also mishear a name, especially a made-up word such as "firstmate", and then your words go to the session you're on as an ordinary prompt. With two sessions, **switch session** is the dependable way to switch.

The request in the app waits 2 minutes, then goes. If you tap **Not now**, the signature fails, or the other session changed in the meantime, you hear "Not switched" and the name of the session you're still talking to.

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

## Turn off the thinking word

The thinking word, and the warning tone when a message can't reach the session, are on by default. To turn both off, start the gateway with `--no-heard`:

```sh
uv run --project gateway walkie-gateway serve --no-heard
```

You still hear "It's in the queue." for a busy session, and why a message didn't reach the session. No app update is needed.

## Listen only to your voice

The gateway can check that each message sounds like you, and drop one that doesn't. It's a filter, not a lock. It keeps out a bystander, a shop, a radio, a video, or someone who has your phone and talks in their own voice. It doesn't stop a recording of your voice, a software copy of it, or a voice close enough to yours.

It's off until you enroll your voice. Then it starts in shadow mode, which logs how each message scores and refuses nothing. After a week of walks, set the thresholds from your own scores, and turn on enforcing.

### Enroll your voice

Plug the collar mic's receiver into the Mac, clip the mic on as you wear it on a walk, and run:

```sh
gateway/fetch-models.sh
uv run --project gateway walkie-gateway enroll
```

The first command downloads the speaker model, if you set up before the gateway had one. The second lists the Mac's audio inputs, so you can choose the receiver, and then shows six short parts to read, one at a time: press Enter, read the part, and press Enter. It takes about two minutes. macOS asks once to let the terminal use the microphone. A part that's too short, silent, or unlike the others is read again.

The six parts make one voiceprint, which is saved in the gateway's state folder, readable only by you, with the speaker model's checksum. The recordings stay in memory, and they're gone when the command ends. The voiceprint never goes to the phone or a session, and revoking the phone leaves it alone. Restart the gateway to use it.

Enroll again to replace the voiceprint after a new collar mic, a long cold, or if your scores drift lower: a median below about 0.6 for messages of 3 seconds or more. To delete it, run `walkie-gateway enroll --remove`. With no voiceprint, the gateway checks nothing, and it logs `speaker check off` when it starts.

### What's checked

| What you say | Checked |
| --- | --- |
| A message to the session, a yes to a read-back, "type it", or "go" while listening | Yes |
| "cancel", "no", "pause", or "hang up" | No. They only stop or decline, so they work in any wind |
| "go", "play", or "start" while paused | No. Every message after it is checked |
| A switch to another session | No. The fingerprint in the app decides |
| Talking over a reply | No. It only stops the reply, and the words are checked when the message ends |

A message's score is how close its voice is to the voiceprint, from -1 to 1. Short speech scores lower for everyone, so the score a message needs depends on how much speech it holds: 0.35 under 1.5 seconds, 0.42 under 3 seconds, and 0.50 from 3 seconds. In lab tests with 40 recorded speakers through the phone's audio link, these thresholds let through about 1% of other people's messages, but more from someone whose voice is close to yours. A single word is hard to judge: expect a quarter to a third of a lone "yes" or "type it" to be refused, more in wind. A longer answer, such as "yes, go ahead and push it", is refused far less often.

A refused message gets the warning tone and "Didn't catch your voice. Say it again." Nothing reaches the session, and a yes or "type it" that was waiting still waits, so say it again. Nothing locks after many refusals.

### Read your scores

Each message logs one `speaker` line, with no words, except "hang up" and what you say while paused:

```text
speaker {"kind": "prompt", "score": 0.614, "seconds": 2.41, "threshold": 0.42, "outcome": "pass"}
```

`kind` is what the message was, such as `prompt`, `yes`, or `switch`. `seconds` is how much speech it held. `outcome` is `pass`, `would-refuse` in shadow mode, `refused` when enforcing, or `unchecked`. A `score` of `null` means the check failed, which counts as a fail.

The gateway logs to its terminal's standard error. To keep a week of lines, start it with `2>> ~/walkie-gateway.log`. Then summarize them:

```sh
uv run --project gateway python gateway/tools/speaker_scores.py ~/walkie-gateway.log
```

For each length of speech, it prints how many messages were scored, their median, and the share each threshold would have refused. For each threshold, pick the highest that refuses about 2% or less of your messages of 2 seconds or more. Going lower than the defaults lets more strangers through. For speech under 1.5 seconds, expect a higher share, and keep 0.35 unless your scores are much lower. Other people's speech the mic heard is in the log too, and it scores low, so a few low scores may not be yours.

### Enforce

```sh
uv run --project gateway walkie-gateway serve --enforce-speaker --speaker-thresholds 0.35,0.42,0.50
```

`--speaker-thresholds` takes the three thresholds, shortest speech first, and without it the defaults apply. The gateway won't start with `--enforce-speaker` and no voiceprint. No app update is needed.

## End a call

To stop, tap **Stop** in the app or in its notification. The session is released: it drops the voice rules, stops sending replies to the gateway, and drops any messages still in the queue. A message it has taken but hasn't started, such as one behind a dialog, stops as it starts. A turn already running carries on, and nobody hears its reply, so tap first to stop it.

A call also ends, and the next Start takes a new fingerprint, when:

| What happens | What you notice |
| --- | --- |
| You say only "hang up", "hangup", or "hang it up", paused or not | You hear "Hanging up.", then the warning tone. The words never reach the session. A sentence that merely contains them, such as "Please don't hang up yet", goes to the session as a prompt |
| The earbuds disconnect, including when their battery runs out | The call ends at once, with no tone, and the app says the earbuds disconnected. Losing the USB mic doesn't end the call |
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

After each spoken message, the gateway logs one `turn-timing` line, in milliseconds after your last voiced audio, so marks before that are negative. Every message gets its own line, with `queued`, `refused`, or `dropped` set if that happened. `longest_pause` is the longest pause you carried on from. `speech_detected` is when the gateway decided you were talking, and `transcribe_start` is when the final transcription began. `submitted` should be about 2,000, or 3,000 if you paused. For a session that answers straight away, `first_audio_played` should be about 4,000 to 5,000, or a second more if you paused. `last_voice_at` is that moment on the Mac's monotonic clock, in seconds. The gateway also logs each spoken notice by name, such as `notice queued`, `notice heard`, or `notice paused`, and never its words.

The app's screen shows the routes, which mic channels carry sound, the round trip to the Mac, and underruns. It also shows the mic's loudest level over the last second, its background, and how loud the earbuds leak, all in dB under full scale in the speech band, and how many times you talked over a reply and how many of those resumed.

## Voices

Pick a voice under **Voice** in the app, once you've tapped **Start**. All of them come with the Kokoro model, the gateway remembers your choice in its state folder, and it takes effect from the next reply.

The gateway's own notices, such as "Paused." or "It's in the queue.", always speak in Michael's American voice, after a short rising tone from the phone. Replies can't use that voice, so you can always tell a notice from a reply that says the same words. If you'd chosen Michael before, replies go back to the default voice.

## How it talks

How fast the voice talks, how long it pauses, and how the session words its replies are set in one Markdown file on the Mac, `~/.config/walkie-talkie/talk.md`. It works like an `AGENTS.md` for talking. Without the file, you get the defaults below. Edits take effect without a restart: the pace from the next sentence, and the rules from the next prompt.

The front matter, between two `---` lines, sets the pace. Each setting is optional, and goes on its own line as its exact name, a colon, and a plain number, such as `speed: 0.8`:

| Setting | What it sets | Default | Range |
| --- | --- | --- | --- |
| `speed` | How fast the voice talks. 1 is the Kokoro voice's natural speed | 0.9 | 0.5 to 2 |
| `sentence_pause` | Seconds of silence between sentences | 0.4 | 0 to 3 |
| `paragraph_pause` | Seconds of silence between paragraphs, list items, and lines | 0.9 | 0 to 5 |

A setting that's missing, not a plain number, or out of range keeps its default. The gateway logs each setting it ignores, and each line whose name isn't one of these three. The pause after a reply's last sentence stays short, so you can answer right away. Notices, such as "It's in the queue.", use the speed but not the pauses.

The text after the front matter replaces the session's style rules. If there's no text, the session follows these default rules:

```markdown
---
speed: 0.9
sentence_pause: 0.4
paragraph_pause: 0.9
---

Write for the ear:

- Start with the verdict or answer in one sentence: done, not done, blocked, or the answer itself.
- When they tell you to do something, don't repeat their request back to them. They know what they asked. Acknowledge it in a few words at most, and say only what they don't know yet.
- Then give at most three short points: what changed, whether you verified it, and what needs their decision.
- Keep the whole reply under about 100 words, in plain conversational sentences.
- Their prompts are recognised speech, so read past misheard words, and ask when the meaning is unclear.
- If you're blocked, say so and ask the one question that unblocks you.
```

Some rules always apply, whatever the file says. The rules cover only the reply to the prompt they arrive with. The session leaves out code, commands, file paths, URLs, credentials, and similar text, which the gateway drops anyway. Before anything destructive, it asks you to say one specific word back.

You can also ask by voice. Say something like "talk slower from now on" or "keep your confirmations shorter", and the session edits the file, creating it from the current rules if it's missing, and changes only what you asked, so the change holds for every later reply and session. Changing a file outside its folder may need your permission at the keyboard, and then you hear "asking for a permission".

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `Can't listen on 100.x.y.z` | Tailscale is stopped on the Mac. Connect it |
| `Missing ... Run gateway/fetch-models.sh` | The speech models aren't downloaded yet |
| The app says "Connecting to the Mac" | Check Tailscale on both ends, and that the gateway is running |
| The app says "Can't reach your Mac" | Turn off any VPN that blocks Tailscale, check Tailscale on both ends, and check the Mac is awake |
| The app says "Tailscale isn't connected on this phone" | The phone has no VPN network, so the app sends nothing. Connect Tailscale on the phone |
| The app says "Your Mac's answer didn't prove it's your Mac" | The gateway answered with another token, or the gateway is older than the app. Update both, then tap **Pair again** if it goes on. An app older than the gateway says "Can't reach your Mac" instead: update the app |
| The app says "the gateway isn't running" | Start it on the Mac |
| The app says "Your Mac refused this phone" | The pairing was revoked, or the phone's address changed. Wait a minute, then tap **Pair again** if it still fails |
| No sessions in the app | No session has the mod running. Type `/reload-plugins` in a session that started before the install. If it says `0 plugins`, run the install for that session's login. A session in a denied folder, on a login the install didn't name, or in an orchestrated worker never shows. `0 hooks` in that line is normal |
| The fingerprint prompt says the key has gone | A fingerprint was added to the phone, or the pairing is over 30 days old. Tap **Pair again** |
| The app says "The key won't sign" or "The fingerprint didn't unlock the key", then an error | The phone's key store refused the key. The error after the message names the exception and its causes. The full trace is in `adb logcat -s walkie` |
| The app says "That session changed a moment ago" | Its list was a moment out of date. Tap **Start** again |
| "Which session?" names the same session twice | Two sessions have the same name on one login, such as two copies of one repository, or on logins that both go unnamed because their paths or names hold a denied name. Switch with **switch session** if they're the only two, or choose the session in the app by where the folder is |
| **front-desk** isn't in the app's list | The launcher isn't running, it ran in a stripped environment, such as `env -i`, where Claude Code doesn't load mods, or its `CLAUDE_CONFIG_DIR` isn't a login you installed for. Start it from your usual shell |
| `A front desk is already running` | Another launcher holds this state folder. Stop that one first |
| "Connect your USB mic" | The input route isn't USB. With Advanced Protection on, connect the mic before you lock the phone |
| Your words aren't picked up | Read **Mic level** in the app while you talk. Speech needs to reach about -45 dB, and 12 dB over the background. If it's lower, raise the mic's gain, or turn off its noise cancelling |
| A reply doesn't stop when you talk over it | Keep talking for about a second, as loud as your last message. Your voice also has to beat **earbud leak** in the app by about 8 dB. If you've just spoken much closer to the mic, say something first in your normal voice. Otherwise, lower the volume, or tap |
| A reply keeps pausing for a moment or cuts off on its own, or a stray message reaches the session | Loud leak or a noisy place. The app learns the leak as replies play, but it can take a dozen pauses in the first reply after it starts. An echo that lasts past about half a second usually teaches it nothing, and neither does one about as loud as your last message. The exception is a talk-over of four words or fewer that cuts the reply and repeats what just played: it teaches the leak even at that level, and even if it was you. Until you've spoken once since the gateway started, any shorter speech counts as leak, even your own voice. If it goes on, lower the volume |
| The earbud tap pauses music instead | Another media app took the tap. Close it, and tap again |
