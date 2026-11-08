"""The conversation: routes what you say to the chosen session, and its reply to your ears.

Everything here runs on one event loop, so it needs no locks. Transcription and
synthesis run in their own single workers.

It's half duplex, like a walkie-talkie. While a reply plays, the microphone is
ignored, so the earbuds can't feed a reply back in as a prompt. Anyone still
talking as it opens hears the warn tone, and those words don't count, because
their start is gone. Tap the earbud to cut the reply off and stop the turn, then
talk. A reply waits while you're mid-message, pauses included.

A prompt spoken to a busy session is accepted and waits its turn. The mod tells
the gateway as soon as it has the prompt, so the voice can say so.

A session is chosen only by a fingerprint at Start, which the phone's hardware key
signs, and it stays chosen only for that run of the phone. The run ends at Stop,
when the phone has been gone PHONE_LOST_SECONDS, after IDLE_SECONDS with nothing
said or heard, when the session goes, or when its mod lets go. Then the next Start
takes a new fingerprint. The mod checks the signature itself, so a gateway alone
can't choose a session.

A front-desk session relays another session's replies here, to be spoken word for
word. Any of them can be a read-back that asks for a yes, and only this code hears
that yes, so no model decides what it means. It counts only as the next thing you
say, wholly after a reply with nothing left out has played through, within
READBACK_SECONDS. A spoken yes alone confirms nothing: the app then shows the
words you heard, and only a fingerprint there files the confirmation for the
session that asked. Anything else leaves nothing to say yes to: a tap, another
prompt, a newer reply, or the wait running out.

Of those replies, a tap cuts only the one playing. It means you're about to talk, so
every later reply waits in memory and plays after the response to your next message.
"""
import asyncio
import collections
import concurrent.futures
import json
import logging
import secrets
import time

from . import security
from .text import LEFT_OUT, SpeechFilter, kind, sanitize_prompt
from .turns import TurnDetector

log = logging.getLogger("walkie.conversation")

FRAME_SECONDS = 0.02
LEAD_SECONDS = 0.4          # speech sent ahead of the clock, as the phone's jitter buffer
PACE_SECONDS = 0.06         # how often speech is sent, so several frames share a packet
MIC_TAIL_SECONDS = 0.25     # the microphone stays ignored this long after a reply's last frame
MIC_STALE_SECONDS = 5.0     # no microphone audio for this long: a reply stops waiting for the end of a message
TYPING_OFFER_SECONDS = 30   # how long the offer to type a refused prompt stands
ORDER_WAIT_SECONDS = 10     # the mod hasn't picked up a prompt in this time: say so
READBACK_SECONDS = 60       # how long after a relayed reply has played a yes still counts
CONFIRM_SECONDS = 120       # how long the app's confirmation waits for a fingerprint after a spoken yes
METRICS_WAIT_SECONDS = 15
PRUNE_SECONDS = 5
PHONE_LOST_SECONDS = 120    # the phone has been gone this long: its run ends, so nothing stays connected
IDLE_SECONDS = 30 * 60      # nothing said or heard for this long: the run ends
ENDED_AFTER_SECONDS = 6     # time to speak why a run ended, before the phone stops

NO_SESSION = "No session is connected. Choose one in the app, and tap Start."
SESSION_GONE = "That session has gone. Choose another in the app, and tap Start."
CONNECTED = "Connected to %s."  # never logged: it holds a folder's name
SESSION_CLOSED = "That session ended the call. Tap Start in the app to connect again."
IDLE_ENDED = "Nothing was said for 30 minutes, so I ended the call."
CANCELLED = "Cancelled."
DESK_CANCELLED = "Stopped what was waiting. Anything already filed still stands."
NOT_SENT = "I couldn't send that to the session."
QUEUED = "It's in the queue."  # short, because the microphone is ignored while it plays
QUEUE_DROPPED = "Dropped your queued message."
NOT_REACHED = "I can't reach that session right now. Your message is waiting for it."
NOT_STARTED = "The session hasn't started that. A dialog may be open at the keyboard."
LOST_PART = "I lost part of that. Please say it again."
STOPPED_AT_KEYBOARD = ("This session was stopped at the keyboard, so it won't take a spoken message. "
                       "Say type it, and I'll type that message into its terminal.")
LATER_DROPPED = "I dropped the messages queued after it."
NOTHING_TO_TYPE = "There's nothing waiting to be typed."
DIALOG_OPEN = "A dialog is open in that session, so I didn't type anything."
DRAFT_OPEN = "That session has a half-typed draft, so I didn't type anything."
NO_PANE = "I can't reach that session's terminal, so I didn't type anything."
PERMISSION = "The session is asking for a permission at the keyboard. Tap to stop the turn."
TURN_FAILED = "The turn stopped with an error. It's on screen."
NOTHING_TO_CONFIRM = "Nothing is waiting for a yes, so I sent nothing."
CONFIRM_IN_APP = "To confirm, open the app and use your fingerprint."
NOT_CONFIRMED = "Not confirmed, so I sent nothing."
YES_SENT = "Sent your confirmation."
YES_NOT_SENT = "I can't tell whether your confirmation was sent. Check before you rely on it."  # the command may have saved it, then failed
REPLY_NOT_SPOKEN = "I couldn't read a reply aloud."
NOT_FILED = "I can't tell whether that was filed."  # the command may have saved it, then failed


class Relayed:
    """A relayed reply, in the words it was spoken in. It rides the speech queue behind its words, and they've played when it comes up."""

    def __init__(self, note, units, takes_yes):
        self.note, self.units, self.takes_yes, self.expires = note, units, takes_yes, 0.0
        self.spoken = " ".join(units)


# What the log may say about a notice: its name, never any words of a prompt or a reply.
NOTICE_NAMES = {
    NO_SESSION: "no-session", SESSION_GONE: "session-gone", CANCELLED: "cancelled", DESK_CANCELLED: "desk-cancelled", NOT_SENT: "not-sent",
    QUEUED: "queued", QUEUE_DROPPED: "queue-dropped", NOT_REACHED: "not-reached", NOT_STARTED: "not-started",
    LOST_PART: "lost-part", STOPPED_AT_KEYBOARD: "stopped-at-keyboard", LATER_DROPPED: "later-dropped", NOTHING_TO_TYPE: "nothing-to-type", DIALOG_OPEN: "dialog-open",
    DRAFT_OPEN: "draft-open", NO_PANE: "no-pane", PERMISSION: "permission", TURN_FAILED: "turn-failed",
    NOTHING_TO_CONFIRM: "nothing-to-confirm", CONFIRM_IN_APP: "confirm-in-app", NOT_CONFIRMED: "not-confirmed",
    YES_SENT: "yes-sent", YES_NOT_SENT: "yes-not-sent", SESSION_CLOSED: "session-closed", IDLE_ENDED: "idle-ended",
    REPLY_NOT_SPOKEN: "reply-not-spoken", NOT_FILED: "not-filed",
}


class Metrics:
    """One spoken message's timings, in milliseconds after the last voiced audio. Never any words.

    It follows that message's own reply, so a later message or a notice can't take its marks.
    """

    def __init__(self, last_voice, clock):
        self.start, self.clock, self.times, self.done = last_voice, clock, {}, False

    def mark(self, name, at=None):
        if name not in self.times:
            self.times[name] = round(((self.clock() if at is None else at) - self.start) * 1000)

    def report(self):
        if not self.done:
            self.done = True
            log.info("turn-timing %s", json.dumps(self.times))


class Conversation:
    def __init__(self, speech, is_speech, new_decoder, encoder, clock=time.monotonic):
        self.speech = speech
        self.new_decoder = new_decoder
        self.encoder = encoder
        self.clock = clock
        self.phone = None           # set once the phone server exists
        self.modlink = None         # set once the mod link exists
        self.decoder = new_decoder()
        self.detector = TurnDetector(is_speech, self._transcribe, self._turn_finished, clock, lambda: self._tone("warn"))
        self.stt_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="stt")
        self.tts_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="tts")
        self.chosen = None          # the instance of the chosen session
        self.gen = 1                # speech of an earlier generation was cut off
        self.say_queue = collections.deque()
        self.say_ready = asyncio.Event()
        self.synthesizing = False
        self.frames = collections.deque()
        self.frames_ready = asyncio.Event()
        self.played_until = 0.0
        self.last_mic = 0.0
        self.reply_turn = None      # the spoken turn now running
        self.reply_filter = None
        self.order_prefix = secrets.token_hex(4)  # so a mod can't mistake an order from an earlier gateway
        self.next_order = 1
        self.orders = {}            # order id -> (what, text)
        self.accepted = set()       # orders the mod has told us it holds
        self.typing_offer = None    # (text, expiry)
        self.readback = None        # the relayed reply a yes would answer
        self.confirming = None      # (nonce, read-back, expiry): a spoken yes waiting for a fingerprint in the app
        self.active_at = clock()    # the last time anything was said or heard
        self.lost_at = None         # when the phone went, while an approved run waits for it
        self.sounding_relay = None  # the relayed reply whose audio went out last, until it has played through
        self.held_relays = None     # after a tap, the replies waiting for the response to your next message
        self.prompt_metrics = {}    # order id -> Metrics, until that prompt's turn starts
        self.reply_metrics = None   # the spoken turn now running
        self.sounding_metrics = None  # the reply whose audio went out last, until the phone says it played
        self.tasks = []

    def start(self):
        self.tasks = [asyncio.ensure_future(t) for t in (self._speak_loop(), self._pace_loop(), self._prune_loop())]

    def stop(self):
        for task in self.tasks:
            task.cancel()
        self.stt_pool.shutdown(wait=False, cancel_futures=True)
        self.tts_pool.shutdown(wait=False, cancel_futures=True)

    # --- from the phone ----------------------------------------------------------

    def phone_new_run(self):
        self.decoder = self.new_decoder()
        self.detector.reset()
        self._cut_speech()
        self._set_chosen(None)

    def phone_connected(self):
        self.phone.send_control({"t": "cut", "gen": self.gen})
        self.sessions_changed()

    def mic_packet(self, packet):
        samples = self.decoder.decode(packet)
        self.last_mic = self.clock()
        self.detector.feed(samples, ignored=self._mic_ignored() and not self.detector.talking)

    def mic_gap(self):
        """Audio went missing. What was being said isn't whole, and what comes next may have lost its start."""
        if self.detector.talking:
            self.say(LOST_PART)
        self.detector.reset()
        self.detector.skip_speech()

    def _mic_ignored(self):
        return bool(self.frames) or self.clock() < self.played_until + MIC_TAIL_SECONDS

    def phone_message(self, message):
        """Only from a run a fingerprint started: the phone server drops the rest."""
        what = message.get("t")
        if what == "tap":
            self._interrupt(message.get("gen", 0))
        elif what == "release":
            self._end_run(None)
        elif what == "confirmed":
            self._confirmed(message.get("nonce"), message.get("sig"))
        elif what == "declined":
            if self.confirming and message.get("nonce") == self.confirming[0]:
                self._drop_confirm()
                self.say(NOT_CONFIRMED)
        elif what == "played" and self.sounding_metrics:
            rtt = message.get("rtt") or 0
            self.sounding_metrics.mark("first_audio_played", self.clock() - rtt / 2000)
            self.sounding_metrics.report()
            self.sounding_metrics = None

    # --- starting and ending a run ------------------------------------------------

    def start_problem(self, instance, mod_nonce):
        """Why a fingerprint's choice can't start, or None: the session has gone, or its challenge changed since the phone saw it."""
        session = self.modlink.sessions.get(instance)
        if session is None or not session.nonce:
            return "gone"
        return None if session.nonce == mod_nonce else "changed"

    def phone_approved(self, grant):
        """A fingerprint chose this session. The mod checks the signature itself before it takes part."""
        session = self.modlink.sessions[grant["session"]]
        session.send({"grant": grant})
        self._set_chosen(session.instance)
        self.active_at = self.clock()
        self.lost_at = None
        self.say(CONNECTED % (session.describe(0)["name"] or "the session"))

    def _end_run(self, why, notice=None):
        """No session stays chosen, and the phone's run is over. The phone hears why once the notice has had time to play."""
        run = self.phone.run_id
        self._set_chosen(None)
        self.phone.end_run()
        self.lost_at = None
        if why is None:
            return
        if notice:
            self.say(notice)
            asyncio.get_running_loop().call_later(ENDED_AFTER_SECONDS, self.phone.send_ended, run, why)
        else:
            self.phone.send_ended(run, why)

    def revoke_phone(self):
        self._end_run(None)
        self.phone.revoke()

    def _set_chosen(self, instance):
        if instance == self.chosen:
            return
        queued = any("queued" in metrics.times for metrics in self.prompt_metrics.values())
        self._cut_speech()
        self.orders.clear()
        self.accepted.clear()
        self._report_waiting_prompts()
        old = self._chosen_session()
        self.chosen = instance
        self._drop_reply()
        self._drop_confirm()
        self.typing_offer = None
        if old:
            old.forget_prompts()
            old.wake()
        if instance:
            self.modlink.sessions[instance].wake()
            if queued:  # the session that had them drops them when it hears it was let go
                self.say(QUEUE_DROPPED)
        self.sessions_changed()

    def _cut_speech(self, at_least=0):
        self.gen = max(self.gen + 1, at_least)
        self.frames.clear()
        self.say_queue.clear()
        self.played_until = 0.0
        self.readback = None
        self.sounding_relay = None
        self.held_relays = None
        self.phone.forget_speech()
        self.phone.send_control({"t": "cut", "gen": self.gen})

    def _interrupt(self, phone_gen):
        """The earbud tap. The phone has already silenced itself, up to the generation it names."""
        self.active_at = self.clock()
        self._drop_confirm()
        later = [relayed for relayed in self._queued_relays() if relayed is not self.sounding_relay]
        held = (self.held_relays or []) + later
        self._cut_speech(phone_gen)
        self.held_relays = held
        session = self._chosen_session()
        if session:
            # The turn it names, if the gateway has heard of one. The mod also stops turns yet to start.
            self._send_order(session, "abort", self.reply_turn)
        self._drop_reply()
        for metrics in self.prompt_metrics.values():  # the tap stops each of these before its turn
            metrics.times["dropped"] = 1
        self._report_waiting_prompts()

    def _drop_reply(self):
        """This reply won't reach its turn-end, so its line in the log goes out now."""
        if self.reply_metrics:
            self.reply_metrics.report()
        self.reply_turn = self.reply_filter = self.reply_metrics = None

    # --- turns of speech ---------------------------------------------------------

    async def _transcribe(self, audio):
        return await asyncio.get_running_loop().run_in_executor(self.stt_pool, self.speech.transcribe, audio)

    def _turn_finished(self, text, last_voice, longest_pause=0.0):
        text = sanitize_prompt(text)
        what = kind(text)
        if what == "noise":
            return
        self.active_at = self.clock()
        if what != "yes":
            self.readback = None
            self._drop_confirm()
        if what == "cancel":
            desk = self._chosen_session()
            self._tone("ack")
            self._interrupt(0)
            self.say(DESK_CANCELLED if desk and desk.front_desk else CANCELLED)  # a desk can't call back what it has filed
            self._release_relays()
            return
        session = self._chosen_session()
        if session is None:
            self._tone("warn")
            self.say(NO_SESSION)
            return
        if what == "confirm-typing":
            self._confirm_typing(session)
            self._release_relays()
            return
        if what == "yes" and session.front_desk:
            self._confirm_readback(session, last_voice)
            self._release_relays()
            return
        metrics = Metrics(last_voice, self.clock)
        metrics.times["longest_pause"] = round(longest_pause * 1000)
        self._tone("ack")
        metrics.mark("tone_sent")
        order = self._send_order(session, "submit", text, self._reply_unheard())
        self.prompt_metrics[order] = metrics
        asyncio.get_running_loop().call_later(ORDER_WAIT_SECONDS, self._check_order, order)

    def _report_waiting_prompts(self):
        """Every message gets its line in the log, even one whose turn never comes."""
        for metrics in self.prompt_metrics.values():
            metrics.report()
        self.prompt_metrics.clear()

    def _confirm_typing(self, session):
        offer, self.typing_offer = self.typing_offer, None
        if not offer or self.clock() > offer[1]:
            self.say(NOTHING_TO_TYPE)
            return
        self._tone("ack")
        self._send_order(session, "type", offer[0])

    def _reply_unheard(self):
        """Whether a reply is still on its way out: the session may be idle, but its words haven't been heard."""
        return self._mic_ignored() or bool(self.say_queue) or self.synthesizing or bool(self.held_relays)

    def _confirm_readback(self, session, last_voice):
        """Only a yes wholly after the read-back counts: not one said before it played, or after speech that lost its start."""
        readback, self.readback = self.readback, None
        played = readback.expires - READBACK_SECONDS if readback else 0.0
        if not readback or self.clock() > readback.expires or last_voice < played or self.detector.skipped_at >= played:
            self.say(NOTHING_TO_CONFIRM)
            return
        self._tone("ack")
        nonce = security.new_nonce()
        self.confirming = (nonce, readback, self.clock() + CONFIRM_SECONDS)
        self.phone.send_control({"t": "confirm", "nonce": nonce, "text": readback.spoken})
        self.say(CONFIRM_IN_APP)

    def _confirmed(self, nonce, signature):
        """A fingerprint in the app, over the words the owner heard. Only now does the yes go to the session."""
        confirming = self.confirming
        if not confirming or nonce != confirming[0]:
            return
        self._drop_confirm()
        session = self._chosen_session()
        readback = confirming[1]
        if (session is None or not session.front_desk or self.clock() > confirming[2]
                or not self.phone.signed(security.confirm_message(nonce, readback.spoken), signature)):
            self.say(NOT_CONFIRMED)
            return
        self._tone("ack")
        self._send_order(session, "confirm", {"note": readback.note, "spoken": readback.spoken})

    def _drop_confirm(self):
        if self.confirming:
            self.confirming = None
            self.phone.send_control({"t": "confirm", "nonce": None})

    def _send_order(self, session, what, value, unheard=False):
        order = "%s-%d" % (self.order_prefix, self.next_order)
        self.next_order += 1
        self.orders[order] = (what, value)
        session.send({"id": order, what: value, "unheard": True} if unheard else {"id": order, what: value})
        return order

    def _check_order(self, order):
        """The mod never picked this up, or the engine hasn't started it. One in the queue is fine: it's waiting."""
        if order not in self.orders:
            return
        if order not in self.accepted:
            self.say(NOT_REACHED)
        elif order in self.prompt_metrics and "queued" not in self.prompt_metrics[order].times:
            self.say(NOT_STARTED)

    def _tone(self, name):
        if self.phone:
            self.phone.send_control({"t": "tone", "kind": name})

    # --- from the mod ------------------------------------------------------------

    def mod_event(self, session, event):
        what = event.get("kind")
        if what == "bye":
            self._session_gone(session.instance)
            return
        if session.instance != self.chosen:
            return
        if what == "grant-ended":
            self._end_run("closed", SESSION_CLOSED)
            return
        if what == "turn-start":
            self._drop_reply()
            self.reply_turn = event.get("turnId")
            self.reply_filter = SpeechFilter()
            self.reply_metrics = self.prompt_metrics.pop(event.get("order"), None)
            if self.reply_metrics:
                self.reply_metrics.mark("turn_start")
        elif what == "text" and self.reply_filter and event.get("turnId") == self.reply_turn:
            if self.reply_metrics:
                self.reply_metrics.mark("first_text")
            for unit in self.reply_filter.feed(event.get("text") or ""):
                self.say(unit, self.reply_metrics)
        elif what == "turn-end" and event.get("turnId") == self.reply_turn:
            for unit in self.reply_filter.finish() if self.reply_filter else []:
                self.say(unit, self.reply_metrics)
            if event.get("reason") in ("error", "refusal"):
                self.say(TURN_FAILED)
            if self.reply_metrics:  # in case the phone never reports the first sound
                asyncio.get_running_loop().call_later(METRICS_WAIT_SECONDS, self.reply_metrics.report)
            self.reply_turn = self.reply_filter = self.reply_metrics = None
            self._release_relays()
        elif what == "permission" and self.reply_turn:
            self.say(PERMISSION)
        elif what == "not-filed":
            self.say(NOT_FILED, self.reply_metrics)
        elif what == "relay":
            self._relay(str(event.get("note")), str(event.get("text") or ""))
        elif what == "order":
            self._order_result(event)

    def _relay(self, note, text):
        """A reply from the session doing the work. No model stands between it and your ears."""
        speech = SpeechFilter()
        units = speech.feed(text + "\n") + speech.finish()
        whole = speech.spoke and not speech.truncated and LEFT_OUT not in " ".join(units).lower()
        relayed = Relayed(note, units, whole)
        if self.held_relays is None:
            self._queue_relay(relayed)
        else:
            self.held_relays.append(relayed)

    def _queue_relay(self, relayed):
        self.readback = None    # only the newest reply can take a yes, and only once it has played
        for unit in relayed.units:
            self.say(unit, relayed=relayed)
        self.say_queue.append((self.gen, relayed, None, None))
        self.say_ready.set()

    def _release_relays(self):
        held, self.held_relays = self.held_relays or [], None
        for relayed in held:
            self._queue_relay(relayed)

    def _queued_relays(self):
        return [item[1] for item in (*self.frames, *self.say_queue) if isinstance(item[1], Relayed)]

    def _reply_not_spoken(self, relayed):
        """Its words go, and the notice takes their place. Its marker stays, so it can't take a yes or leave an older reply able to."""
        relayed.units, relayed.takes_yes = [], False
        self.say_queue = collections.deque(item for item in self.say_queue if item[3] is not relayed)
        log.info("notice %s", NOTICE_NAMES[REPLY_NOT_SPOKEN])
        self.say_queue.appendleft((self.gen, REPLY_NOT_SPOKEN, None, None))

    def _order_result(self, event):
        order_id = event.get("id")
        if event.get("accepted"):
            if order_id in self.orders:
                self.accepted.add(order_id)
                if event.get("busy"):
                    self.say(QUEUED)
                    if order_id in self.prompt_metrics:
                        self.prompt_metrics[order_id].times["queued"] = 1
            return
        order = self.orders.pop(order_id, None)
        self.accepted.discard(order_id)
        if not order:
            return
        what, value = order
        if what == "abort" and event.get("dropped"):
            self.say(QUEUE_DROPPED)
        if what == "confirm":
            self.say(YES_SENT if event.get("ok") else YES_NOT_SENT)
            return
        if event.get("ok") or what == "abort":
            return
        metrics = self.prompt_metrics.pop(order_id, None)  # this prompt's turn won't come
        if metrics:
            metrics.times["dropped" if event.get("dropped") else "refused"] = 1
            metrics.report()
        if event.get("dropped"):
            return
        if what == "submit":
            if event.get("drop"):
                self.typing_offer = (value, self.clock() + TYPING_OFFER_SECONDS)
                self.say(STOPPED_AT_KEYBOARD)
                if event.get("later"):
                    self.say(LATER_DROPPED)
            else:
                self.say(NOT_SENT)
        elif what == "type":
            refused = event.get("refused")
            self.say({"dialog": DIALOG_OPEN, "draft": DRAFT_OPEN, "no-pane": NO_PANE}.get(refused, NOT_SENT))

    def _chosen_session(self):
        return self.modlink.sessions.get(self.chosen) if self.chosen else None

    def is_chosen(self, session):
        return session.instance == self.chosen

    def chosen_run(self, session):
        """The run a fingerprint approved, for the chosen session only, or None."""
        return self.phone.approved_run if self.phone and self.is_chosen(session) else None

    def sessions_changed(self):
        """The phone's list: the latest prompt first. Only sessions whose mod can check a fingerprint."""
        if self.phone and self.modlink:
            now_ms = time.time() * 1000
            listed = sorted((s for s in self.modlink.sessions.values() if s.nonce), key=lambda s: -s.prompted_at)
            self.phone.send_control({
                "t": "sessions",
                "list": [s.describe(now_ms) for s in listed],
                "chosen": self.chosen,
            })

    def _session_gone(self, instance):
        self.modlink.sessions.pop(instance, None)
        if instance == self.chosen:
            self.chosen = None
            self._drop_reply()
            self._drop_confirm()
            self.orders.clear()
            self.accepted.clear()
            self._report_waiting_prompts()
            self._end_run("gone", SESSION_GONE)
        self.sessions_changed()

    def _check_run(self):
        """A run nobody is using ends: the phone has gone, or nothing has been said or heard for a long time."""
        now = self.clock()
        if self.confirming and now > self.confirming[2]:
            self._drop_confirm()
        if not self.phone.approved:
            self.lost_at = None
            return
        if self.reply_turn or self.frames or self.say_queue or self.synthesizing:
            self.active_at = now  # a reply on its way counts: the owner is waiting for it
        if self.phone.connected:
            self.lost_at = None
        elif self.lost_at is None:
            self.lost_at = now
        if self.lost_at is not None and now - self.lost_at > PHONE_LOST_SECONDS:
            log.info("the phone has gone, so its run ended")
            self._end_run("lost")
        elif now - self.active_at > IDLE_SECONDS:
            log.info("nothing was said, so the run ended")
            self._end_run("idle", IDLE_ENDED)

    # --- speaking ----------------------------------------------------------------

    def say(self, text, metrics=None, relayed=None):
        if text in NOTICE_NAMES:
            log.info("notice %s", NOTICE_NAMES[text])
        self.say_queue.append((self.gen, text, metrics, relayed))
        self.say_ready.set()

    async def _speak_loop(self):
        """Synthesize ahead: the next sentence is made while the current one plays."""
        loop = asyncio.get_running_loop()
        while True:
            await self.say_ready.wait()
            if not self.say_queue:
                self.say_ready.clear()
                continue
            gen, text, metrics, relayed = self.say_queue.popleft()
            if gen != self.gen:
                continue
            if isinstance(text, Relayed):
                self.frames.append((gen, text, None, None))
                self.frames_ready.set()
                continue
            if metrics:
                metrics.mark("first_unit")
            self.synthesizing = True
            try:
                packets = await loop.run_in_executor(self.tts_pool, self._synthesize, text)
            except Exception:
                log.exception("synthesis failed")
                if relayed and gen == self.gen:
                    self._reply_not_spoken(relayed)
                continue
            finally:
                self.synthesizing = False
            if gen != self.gen:
                continue
            if metrics:
                metrics.mark("first_audio_ready")
            self.frames.extend((gen, packet, metrics, relayed) for packet in packets)
            self.frames_ready.set()

    def _synthesize(self, text):
        return self.encoder.encode(self.speech.synthesize(text))

    async def _pace_loop(self):
        """Send speech at about the speed it plays, so a tap has little to throw away."""
        while True:
            if not self.frames:
                self.frames_ready.clear()
                await self.frames_ready.wait()
            now = self.clock()
            # A stall in the microphone isn't the end of a message: the words so far are kept, and the
            # message carries on when the audio does. Only a reply stops waiting for it.
            mid_message = self.detector.talking and now - self.last_mic <= MIC_STALE_SECONDS
            if not mid_message and self.phone and self.phone.connected:
                self.played_until = max(self.played_until, now)
                while self.frames and self.played_until - now < LEAD_SECONDS:
                    gen, packet, metrics, relayed = self.frames.popleft()
                    if isinstance(packet, Relayed):
                        self.sounding_relay = None
                        if packet.takes_yes and not self._queued_relays():
                            packet.expires = self.played_until + READBACK_SECONDS
                            self.readback = packet
                        else:
                            self.readback = None
                        continue
                    self.phone.send_speech(gen, packet)
                    self.sounding_relay = relayed
                    self.played_until += FRAME_SECONDS
                    if metrics and "first_audio_sent" not in metrics.times:
                        metrics.mark("first_audio_sent")
                        self.sounding_metrics = metrics
            await asyncio.sleep(PACE_SECONDS)

    async def _prune_loop(self):
        while True:
            await asyncio.sleep(PRUNE_SECONDS)
            self._check_run()
            if self.modlink.prune():
                if self.chosen and self.chosen not in self.modlink.sessions:
                    self._session_gone(self.chosen)
                else:
                    self.sessions_changed()
