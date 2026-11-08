# Talk to a front desk

A session in the middle of a long turn can't answer you. A front desk is a small session you talk to instead. It answers questions from a cached status, and it files everything else in the inbox of the session doing the work, which this section calls the primary. It never waits on the primary, so a spoken prompt is never refused as busy.

The front desk can't act on anything. It has no tools, no MCP servers, and no settings, and it takes no turns. Each question is one model call that sees the status and the last few lines of the talk, and that call either answers or says to file. A question opens with what, where, which, who, when, why, how, is, are, did, does, or has. Anything else, including "Can you post the update?", is filed with no model call. Filing runs a command with your words as recognized, so the model can't reword them.

## Set up the front desk

An agent can follow these steps as written. The front desk needs three commands from whatever runs your primary. Each command is a list of strings, and each runs with no shell.

| Setting | What the command does | What it must do |
| --- | --- | --- |
| `status` | Reads what's going on | Change nothing, and print text. It runs every 30 seconds, and the model sees the first 6,000 characters |
| `handoff` | Files a note for the primary | Take the note as its last argument, and exit 0 only if the note is saved |
| `replies` | Lists the primary's answers to notes | Print `{"replies": [{"id": "the note", "body": "the answer", "cursor": "..."}]}`, oldest first. Any argument that's exactly `{cursor}` becomes the last cursor seen, which starts empty, and the command lists only what came after it |

1. Write the commands to `front-desk.json` in the gateway's state folder, `~/.local/state/walkie-talkie/`:

   ```json
   {
     "model": "haiku",
     "status": ["/path/to/your/tool", "status"],
     "handoff": ["/path/to/your/tool", "note"],
     "replies": ["/path/to/your/tool", "replies", "--after", "{cursor}"],
     "env": {"CLAUDE_CONFIG_DIR": "/path/to/the/login"}
   }
   ```

   `model` answers each question, and it defaults to `haiku`. `env` sets variables for the session and its commands, such as `CLAUDE_CONFIG_DIR` to choose the login. That login must be one you installed the mod for, or the front desk never shows on the phone.

1. Start the front desk in a terminal you leave open:

   ```sh
   uv sync --project gateway
   gateway/.venv/bin/walkie-front-desk
   ```

1. In the app, choose **front-desk** from the list of sessions, and tap **Start**. Like any session, it takes your fingerprint.

To stop the front desk, press Ctrl+C, or stop the launcher process it names when it starts. The session reads its input from the launcher, so it ends with the launcher, even if the launcher is killed. A second launcher for the same state folder refuses to start. The session saves no transcript, and replies from before its first start are never read out.

An app restart, a gateway restart, or choosing another session can lose an answer you haven't heard yet, because the front desk moves on from an answer as soon as it passes the answer to the gateway. The answer stays in the written record of whatever runs your primary. A lost read-back is safe, because nothing happens without a yes.

## What you hear

| You | What happens |
| --- | --- |
| Ask a question the status answers | You hear the answer. The status is usually under 30 seconds old. If it's over 90 seconds old, because the `status` command is failing, the front desk files your question rather than answer from it. It also files your words if the model's answer runs over 40 words, contains a question mark, speaks in the first person, such as "I'll pass that on," claims to act without naming anyone, such as "Merging now," or holds anything that isn't read out, such as code or a path. You then hear "Filed." The model is told to file a question it can only partly answer, but it can still speak the part it knows. These checks go by wording, so a model that breaks its rule on a question can still be spoken, such as "The update is posted," with nothing filed |
| Ask for anything else | You hear "Filed." Your words go to the primary as recognized, with the end of the talk so far, and no model is asked. A question gets the same if the model fails or takes over 6 seconds. If the `handoff` command fails, you hear "I can't tell whether that was filed," even if you tapped while it ran, because a command can save a note and still fail. Saying it again may file it twice |
| Wait | Within about 5 seconds of the primary answering a note, you hear its answer word for word, with no model in between. An answer waits while you talk, while the front desk answers you, and while no phone has chosen the front desk. If an answer can't be turned into speech, you hear "I couldn't read a reply aloud," and the next answer plays |
| Tap an earbud | Speech stops, and a prompt that hasn't been answered or sent to the `handoff` command is dropped, and you hear that it was. A `handoff` command already running can still file its prompt, and you don't hear "Filed." if it does. Only the answer playing stops. Later answers wait in memory, and they play after the response to your next message |

Known limit, until talking over a reply reaches the front desk: a prompt that you start over a short answer, such as "Filed.", loses its first words, and the front desk files the rest. Tap first, or wait for the answer to end.

## How a read-back and a yes work

Merges, posts, and other irreversible actions need a spoken read-back, a spoken yes, and then your fingerprint in the app. No model at the front desk takes part in any of them, and a spoken yes alone confirms nothing, because any voice near the microphone can say it.

1. **The primary does the read-back.** Each note tells it what to do before anything irreversible: don't act, and reply with one short sentence that names the exact action and ends with "Confirm?" The read-back never says the word "yes", so its own echo can't count as one. You hear that reply word for word.
1. **The gateway hears the yes.** A bare "yes" counts whenever you say it while the read-back waits, as long as nothing was left out of that reply. You hear "To confirm, open the app and use your fingerprint." A bare "no" ends the wait, sends nothing to the session, and you hear "Not confirmed, so I sent nothing." "Yes?" and "No?" don't count.
1. **You confirm in the app.** A notification says something is waiting, without saying what. Open the app: it shows the read-back you heard, word for word. Tap **Confirm** and touch the sensor, or tap **Not now**. The fingerprint signs those exact words, and the gateway checks the signature. Only then does it file a note that starts "Confirmed in the app", which names the note it answers, when you confirmed, and the words you heard. You hear "Sent your confirmation." If the `handoff` command fails, you hear "I can't tell whether your confirmation was sent. Check before you rely on it," because a command can save a note and still fail.
1. **The primary acts.** Each note tells it to act only on a "Confirmed in the app" note that names the read-back it's still waiting on, and to read back again if anything it named has changed. Any other words, such as "yes please", or "yes, but after lunch", go through as an ordinary note, which isn't a confirmation.

A late yes does nothing. A yes counts for 60 seconds after the reply has played, and only if no other message comes first. A no, a tap, another prompt, or a newer reply also ends the wait. After that you hear "Nothing is waiting for a yes, so I sent nothing," and nothing is filed. The app's confirmation waits 2 minutes for your fingerprint. A tap, a no, or another prompt withdraws it, and **Not now** or a wrong signature files nothing: you hear "Not confirmed, so I sent nothing." A confirmation that was sent can still sit in the inbox while the primary finishes a turn, so the note says when you confirmed, and the primary reads back again if it's no longer waiting.

A message you spoke before hearing a reply reaches the primary marked as unable to confirm that reply, and a note never quotes the primary's own replies back to it, because you may not have heard them yet.

The rules travel inside each note, so the primary needs no setup beyond reading its notes and answering them. The voice rules the mod adds in a chosen session don't reach a primary the phone hasn't chosen.

## What the front desk costs

These figures come from a busy 16 GB Mac, on Haiku, with the primary mid-turn.

- **Time:** over three runs of 30 prompts, a reply's first word came a median of 3.5 to 4.8 seconds after you stopped talking. Almost 2 seconds of that is the silence that ends a message. The rest is 0.8 to 1.6 seconds for the model and 0.65 to 1 second for the voice, and the slower figures came when the Mac was short of memory.
- **After a quiet spell:** a Mac short of memory can push the speech models out while the gateway sits idle, and the first prompts after that took up to 10.5 seconds. Closing sessions you don't need helps.
- **Memory:** by physical footprint, the session holds 156 to 157 MB while idle and 161 MB after about 30 prompts, and the launcher holds 11 to 12 MB. It keeps no conversation beyond the last 12 lines, so its context can't grow.
