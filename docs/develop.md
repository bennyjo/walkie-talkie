# Develop Walkie Talkie

## How the pieces connect

```text
Mic transmitter --radio--> USB receiver --> AudioRecord (VOICE_RECOGNITION, preferred USB input)
                                               |
Earbuds <--A2DP-- AudioTrack (USAGE_MEDIA, preferred Bluetooth output)
                                               |
                  android/: foreground service, talk-over gate, earbud tap
                                               |  Opus over TCP, inside the tailnet
                                               v
                  gateway/: turn detection, Parakeet, Kokoro, session router
                                               |  Unix socket: long poll and POST
                                               v
                  mod/walk-voice/: runs inside each live Claude Code session
```

## Build and test

Run these from the repository root.

| Task | Command |
| --- | --- |
| Gateway tests, which need no models. Set `WALKIE_TALKIE_MODELS` to also run quiet speech through the real voice detector | `uv run --project gateway python -m unittest discover -s gateway/tests -t gateway` |
| Mod tests | `claude plugin test mod/walk-voice` |
| Mod checks | `claude plugin validate mod/walk-voice` |
| Phone APK | `android/build.sh` |
| Emulator test APK | `android/build.sh --debug` |
| Talk-over gate tests, with no device | `android/build.sh --test` |

The build needs no Gradle. It downloads a JDK and Android's build tools into `android/.toolchain/`, about 570 MB, and checks their checksums. Nothing is installed system-wide. To keep one signing key across builds, so an update installs over the last version, set `WALKIE_KEYSTORE` to a path outside the repository.

## Where things are

| Path | What it holds |
| --- | --- |
| `gateway/walkie_gateway/phone.py` | The phone's protocol, described at the top of the file, and login |
| `gateway/walkie_gateway/modlink.py` | The mod's long poll and events |
| `gateway/walkie_gateway/conversation.py` | Routing, the tap, talking over a reply, pausing, the voice, spoken notices, relayed replies, the spoken yes, and when a call ends |
| `gateway/walkie_gateway/turns.py` | Speech detection and end-of-speech detection |
| `gateway/walkie_gateway/text.py` | What of a reply gets read aloud |
| `gateway/walkie_gateway/speech.py` | Silero, Parakeet, Kokoro, and Opus |
| `gateway/walkie_gateway/security.py` | Bind rules, the token, pairing, and the phone's signatures |
| `gateway/walkie_gateway/__main__.py` | The `serve`, `install`, and `revoke` commands |
| `gateway/walkie_gateway/front_desk.py` | The front desk's launcher and settings |
| `gateway/tools/fake_phone.py` | Stands in for the phone, for testing on the Mac |
| `mod/walk-voice/hooks/register.js` | The mod, including which logins and folders may connect, and its check of the fingerprint |
| `mod/walk-voice/hooks/p256.js` | The mod's check of a P-256 signature |
| `mod/walk-voice/hooks/front-desk.js` | The mod's front-desk role and its rules for the model and the primary |
| `android/src/main/` | The app. `TalkService` runs the conversation, `TalkGate` hears a voice over a reply, and `PhoneKey` holds the hardware key |
| `android/src/debug/` | The test allowances, which the phone's APK doesn't contain |

## Test without a phone

`gateway/tools/fake_phone.py` speaks a 16 kHz mono WAV file to the gateway in real time and reports when the reply arrives. It can also talk over a reply, leak the reply back into the mic, and send a hold with no voice: see the top of the file. You can make a clip with `say -o q.wav --data-format=LEI16@16000 "What is two plus two?"`. Run the gateway with `--bind 127.0.0.1 --pair`, pair with `--pair CODE --key-file phone.key`, and save the token that prints to a private file. The key file stands in for the phone's hardware key. Then pass `--token-file` and `--key-file`, with `--session` and `--say q.wav`. It starts as the app does, signing the session it chose, and `--confirm` also confirms read-backs as a fingerprint in the app would.

To exercise the app itself, install the test APK on an emulator with a fingerprint set up. It accepts `10.0.2.2` as the Mac's address, or `127.0.0.1` with `adb reverse tcp:8791 tcp:PORT` for a gateway on another port, and it plays a raw 16 kHz mono file named `test-mic.pcm` from its external files folder as the microphone, on the left channel only. It accepts any audio route while a file named `any-route` sits in that folder. Without that file, the privacy guards behave as on a phone. It also accepts a key outside secure hardware, which an emulator may not have, and its screen can be captured. Its package name is `dev.walkietalkie.test`, so it can't install over the phone's APK. Never install the test APK on a phone.

Use your own scratch session for testing, started with `--model`, rather than a live session.
