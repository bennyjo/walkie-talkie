"""The conversation: routes what you say to the chosen session, and its reply to your ears.

Everything here runs on one event loop, so it needs no locks. Transcription and
synthesis run in their own single workers.

You can talk over a reply, as in a phone call. While a reply sounds, no message
starts unless the phone says it heard a voice louder than the earbuds' leak. The
phone silences the reply itself at that moment, with no round trip, and it says
"hold". Sound already on its way to the earbuds can't be recalled, so for
OUTPUT_DELAY_SECONDS what the mic hears may still be the reply. Speech counts as the
owner only if it carries on past that, about as loud as their last message. Then the
reply is cut, its turn stops, and the words are the next message. Anything else, such
as a cough, an echo, a short word, or a quieter voice nearby, and the reply resumes.
Each of these fails toward not stopping a turn. Speech over a reply is also dropped
as echo if it's five or more words that repeat, in order, what had just played. The
phone learns to expect that leak from speech that ended inside the output delay, unless
it was about as loud as the owner, and from words that cut the reply but repeat it.
Until the owner has spoken, all speech that ended inside the delay teaches it. Other
speech teaches it nothing, because it may be the owner. A voice too soft for the phone
to hear over the reply stays ignored: anyone still talking as it ends hears the warn
tone, and those words don't count, because their start is gone. Once a reply has
played out, with nothing more on its way, speech in its output delay is an ordinary
message, even a single word. A tap still cuts
everything at once, and a reply waits while you're mid-message, pauses included.

A lone "pause" stops listening, and that survives a dropped connection, a new run of
the app, and a restart. While paused, what's heard is only checked for a lone "go",
"play", or "start": no prompt, no talk-over, and no lesson for the phone. A running
turn carries on, and its reply plays. Over a reply, either word is too short to count
as talking over it, so it's read when its hold settles, if it's as loud as the owner
and isn't a word that just played. The transcriber often misses so short a word over
a reply, and a miss just lets the reply resume, so the owner says it again.

A prompt spoken to a busy session is accepted and waits its turn. The mod tells
the gateway as soon as it has the prompt, so the voice can say so. A prompt an idle
session has taken gets one thinking word, so the owner knows it arrived before the
reply comes. A prompt that doesn't get through gets the warning tone ahead of its notice.
Both are on unless the gateway is started with --no-heard.

A session is chosen only by a fingerprint at Start, which the phone's hardware key
signs, and it stays chosen only for that run of the phone. The run ends at Stop,
when the phone has been gone PHONE_LOST_SECONDS, after IDLE_SECONDS with nothing
said or heard, when the session goes, or when its mod lets go. Then the next Start
takes a new fingerprint. The mod checks the signature itself, so a gateway alone
can't choose a session.

A front-desk session relays another session's replies here, to be spoken word for
word. Any of them can be a read-back that asks for a yes, and only this code hears
that yes, so no model decides what it means. A read-back never says the word yes,
so its own echo can't be one. A yes counts whenever it comes, if no other message
comes first, after a reply with nothing left out has played through, and within
READBACK_SECONDS. A spoken yes alone confirms nothing: the app then shows the
words you heard, and only a fingerprint there files the confirmation for the
session that asked. A no in that wait ends it and sends nothing. Anything else also
leaves nothing to say yes to: a tap, another prompt, a pause, a newer reply, or the wait
running out.

Of those replies, a tap cuts only the one playing. It means you're about to talk, so
every later reply waits in memory and plays after the response to your next message.

Saying "switch session", or "switch to" and a session's name, asks the app for a
fingerprint over that session, exactly as Start does, within the same run. Only once
the signature checks out does the other session take the grant, and the one before is
let go as at Stop. A spoken switch alone moves nothing, and a name that matches no
session goes through as an ordinary prompt.
"""
import asyncio
import collections
import concurrent.futures
import json
import logging
import math
import os
import random
import secrets
import time

import numpy as np

from . import security
from .speech import DEFAULT_VOICE, VOICES
from .text import LEFT_OUT, SpeechFilter, compact, kind, sanitize_prompt, spoken_words, switch_target, words_of
from .turns import SECONDS, TurnDetector, speaking_level

log = logging.getLogger("walkie.conversation")

FRAME_SECONDS = 0.02
LEAD_SECONDS = 0.4          # speech sent ahead of the clock, as the phone's jitter buffer
PACE_SECONDS = 0.06         # how often speech is sent, so several frames share a packet
# How long sound due now can still be reaching the earbuds, and so the mic. The real delay is unknown
# until a walk, so this leans long: too long only makes a talk-over count later, and too short stops
# turns for nothing. The phone's SOUNDING_TAIL_MS is this same delay.
OUTPUT_DELAY_SECONDS = 0.5
HOLD_WAIT_SECONDS = 0.5     # this much quiet settles a hold, with no need for the silence that ends a message
CONFIRM_WINDOWS = 6         # voiced windows past the output delay, about 0.2 s, before the words are checked
OWNER_MARGIN_DB = 4.5       # a talk-over this much quieter than the owner's last message is someone else
ECHO_MIN_WORDS = 5          # anything shorter is the owner, because a short answer often repeats the question's words
ECHO_SECONDS = 4.0          # how far back before a hold a word counts as just played
PLAYED_WORDS = 80           # how many of the last played words are kept for that check
LONE_WORD_SECONDS = 1.5     # speech longer than this over a reply can't be a lone word, so it isn't transcribed
MIC_STALE_SECONDS = 5.0     # no microphone audio for this long: a reply stops waiting for the end of a message
TYPING_OFFER_SECONDS = 30   # how long the offer to type a refused prompt stands
ORDER_WAIT_SECONDS = 10     # the mod hasn't picked up a prompt in this time: say so
READBACK_SECONDS = 60       # how long after a relayed reply has played a yes still counts
CONFIRM_SECONDS = 120       # how long the app's confirmation waits for a fingerprint after a spoken yes or switch
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
QUEUED = "It's in the queue."
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
# These name sessions, so the log names the notice and never its words.
SWITCH_IN_APP = "To switch to %s, open the app and use your fingerprint."
SWITCH_WHICH = "Which session? Say switch to, and one of these: %s."
ALREADY_ON = "You're already talking to %s."
NOT_SWITCHED = "Not switched. You're still talking to %s."
NO_OTHER_SESSION = "There's no other session to switch to."


class Switch:
    """A spoken switch, waiting for a fingerprint in the app over the session it names, as at Start."""

    def __init__(self, session):
        self.instance, self.mod_nonce, self.name = session.instance, session.nonce, session.name

# One of these, at random, says an idle session has the prompt. A busy one says QUEUED instead.
HEARD_WORDS = ("Working.", "Pondering.", "Thinking.", "Mulling.", "Musing.", "Noodling.", "Cogitating.", "Confabulating.")

PAUSED = "Paused."
LISTENING = "Listening."
STILL_PAUSED = "Still paused. Say go to start listening."
# These go ahead of a reply, because a whole reply can be waiting, and the owner is waiting to know.
# Only the latest is true, so it replaces any that hasn't gone out, and the last one heard is the current state.
STATE_NOTICES = {PAUSED, LISTENING, STILL_PAUSED}


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
    REPLY_NOT_SPOKEN: "reply-not-spoken", NOT_FILED: "not-filed", NO_OTHER_SESSION: "no-other-session",
    PAUSED: "paused", LISTENING: "listening", STILL_PAUSED: "still-paused",
}


class Metrics:
    """One spoken message's timings, in milliseconds after the last voiced audio, so marks made
    before that are negative. Never any words.

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
            log.info("turn-timing %s", json.dumps(dict(self.times, last_voice_at=round(self.start, 3))))


class Hold:
    """The phone silenced the sound because someone may be talking over it."""

    def __init__(self, hold_id, at):
        self.id, self.at = hold_id, at
        self.mark = None        # voiced windows counted when speech carried on past the output delay
        self.level_mark = 0     # and windows of speech
        self.checking = False
        self.voiced_in_delay = 0
        self.levels = []        # the speech heard, which outlasts the detector forgetting it


def is_state_notice(unit):
    """A reply's sentence can have the same words, but it comes with a voice."""
    text, _, reply_voice, _ = unit
    return reply_voice is None and text in STATE_NOTICES


class Conversation:
    def __init__(self, speech, is_speech, new_decoder, encoder, clock=time.monotonic, voice_file=None, pause_file=None,
                 heard=True):
        self.speech = speech
        self.new_decoder = new_decoder
        self.encoder = encoder
        self.clock = clock
        self.phone = None           # set once the phone server exists
        self.modlink = None         # set once the mod link exists
        self.decoder = new_decoder()
        self.detector = TurnDetector(is_speech, self._transcribe, self._turn_finished, clock, self._missed_start)
        self.stt_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="stt")
        self.tts_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="tts")
        self.chosen = None          # the instance of the chosen session
        self.gen = 1                # speech of an earlier generation was cut off
        self.say_queue = collections.deque()
        self.say_ready = asyncio.Event()
        self.synthesizing = None    # the unit now being made, until something cuts it
        self.frames = collections.deque()
        self.frames_ready = asyncio.Event()
        self.played_until = 0.0
        self.last_mic = 0.0
        self.reply_turn = None      # the spoken turn now running
        self.reply_filter = None
        self.hold = None            # a Hold while someone may be talking over the reply
        self.state_notice = None    # the last state notice said, until something cuts it
        self.state_notice_ends = 0.0  # when it's done playing: never, until all of it has gone out
        self.reply_heard = False    # the last sound due was a reply's, not a notice's
        self.cut_unheard = False    # the next message cut a reply off, so its speaker hasn't heard all of it
        self.over_a_reply = False   # the next message started under a hold
        self.owner_level = None     # how loud the owner's last message was: None until they've spoken
        self.played_words = collections.deque(maxlen=PLAYED_WORDS)  # (when it plays, word)
        self.voice_file = voice_file
        self.voice = self._load_voice()
        self.reply_voice = self.voice  # a choice takes effect from the next reply
        self.pause_file = pause_file
        self.heard = heard          # say when a prompt has reached the session, and warn when it can't
        self.paused = bool(pause_file) and os.path.exists(pause_file)  # a restart mustn't start listening to a stranger
        self.order_prefix = secrets.token_hex(4)  # so a mod can't mistake an order from an earlier gateway
        self.next_order = 1
        self.orders = {}            # order id -> (what, text)
        self.accepted = set()       # orders the mod has told us it holds
        self.typing_offer = None    # (text, expiry)
        self.readback = None        # the relayed reply a yes would answer
        self.confirming = None      # (nonce, read-back or Switch, expiry): waiting for a fingerprint in the app
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
        if self.paused:
            self.say(STILL_PAUSED)

    def phone_connected(self):
        self.phone.send_control({"t": "cut", "gen": self.gen})
        self._send_voices()
        self.sessions_changed()

    def mic_packet(self, packet):
        samples = self.decoder.decode(packet)
        self.last_mic = self.clock()
        self.detector.feed(samples, ignored=self.hold is None and self._sounding() and not self.detector.talking)
        self._watch_hold()

    def mic_gap(self):
        """Audio went missing. What was being said isn't whole, and what comes next may have lost its start."""
        if self.detector.talking and not self.paused:
            self.say(LOST_PART)
        self.detector.reset()
        self.detector.skip_speech()

    def _sounding(self):
        return bool(self.frames) or self.clock() < self.played_until + OUTPUT_DELAY_SECONDS

    def _reply_pending(self):
        return self._sounding() or bool(self.say_queue) or self.synthesizing

    def phone_message(self, message):
        """Only from a run a fingerprint started: the phone server drops the rest."""
        what = message.get("t")
        if what == "tap":
            self._interrupt(message.get("gen", 0))
        elif what == "hold":
            self._phone_held(message.get("id"))
        elif what == "voice":
            self._choose_voice(message.get("id"))
        elif what == "release":
            self._end_run(None)
        elif what == "confirmed":
            self._confirmed(message.get("nonce"), message.get("sig"))
        elif what == "declined":
            if self.confirming and message.get("nonce") == self.confirming[0]:
                waiting = self.confirming[1]
                self._drop_confirm()
                if isinstance(waiting, Switch):
                    self._not_switched()
                else:
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
        self.say(CONNECTED % (session.name or "the session"))

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
        unfinished = self._unfinished_state_notice()
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
        if unfinished:  # it's about listening, not the session, and it may be the only sign of a pause
            self.say(unfinished)
        self.sessions_changed()

    def _unfinished_state_notice(self):
        return self.state_notice if self.clock() < self.state_notice_ends + OUTPUT_DELAY_SECONDS else None

    def _cut_speech(self, at_least=0):
        self._cut_sound(at_least)
        self.frames.clear()
        self.say_queue.clear()
        self.synthesizing = None
        self.reply_heard = False
        self.cut_unheard = False
        self.over_a_reply = False
        self.readback = None
        self.sounding_relay = None
        self.held_relays = None

    def _cut_notices(self):
        """Replies waiting behind a notice stay, and they go out under the new generation."""
        self._cut_sound()
        self.frames = collections.deque(frame for frame in self.frames if frame[2])
        self.say_queue = collections.deque(unit for unit in self.say_queue if unit[2])
        if self.synthesizing and not self.synthesizing[2]:
            self.synthesizing = None

    def _cut_sound(self, at_least=0):
        self.gen = max(self.gen + 1, at_least)
        self.played_words.clear()
        self.played_until = 0.0
        self.state_notice = None
        self.hold = None
        self.phone.forget_speech()
        self.phone.send_control({"t": "cut", "gen": self.gen})

    def _cut_reply(self, at_least=0):
        """Relayed replies that hadn't started wait for the response to your next message."""
        later = [relayed for relayed in self._queued_relays() if relayed is not self.sounding_relay]
        held = (self.held_relays or []) + later
        self._cut_speech(at_least)
        self.held_relays = held

    def _interrupt(self, phone_gen):
        """The earbud tap. The phone has already silenced itself, up to the generation it names."""
        self.active_at = self.clock()
        self._drop_confirm()
        self._cut_reply(phone_gen)
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

    # --- talking over a reply ----------------------------------------------------

    def _phone_held(self, hold_id):
        if self.hold:
            self.hold.id = hold_id
        elif not self._sounding():
            self.phone.send_control({"t": "resume", "id": hold_id, "leak": False})
        elif self.frames or self.say_queue or self.synthesizing or self.clock() < self.played_until:
            self.hold = Hold(hold_id, self.clock())
            self.detector.allow_start()
        else:  # the reply has played out, so this is an answer to it, not talk over it
            self.played_until = 0.0
            self.detector.allow_start()
            self.phone.send_control({"t": "resume", "id": hold_id, "leak": False})

    def _watch_hold(self):
        hold = self.hold
        if hold is None:
            return
        elapsed = self.clock() - hold.at
        if not self.detector.talking:
            if elapsed > HOLD_WAIT_SECONDS:
                self._release_hold()
            return
        hold.levels = self.detector.speech_levels
        if hold.mark is None:
            hold.voiced_in_delay = self.detector.voiced
        if elapsed < OUTPUT_DELAY_SECONDS:
            return
        if hold.mark is None and not self.detector.quiet:
            hold.mark = self.detector.voiced
            hold.level_mark = len(self.detector.speech_levels)
        if hold.mark is not None and self.detector.voiced - hold.mark >= CONFIRM_WINDOWS and not self.paused \
                and self._as_loud_as_owner(self.detector.speech_levels[hold.level_mark:]) and not hold.checking:
            hold.checking = True
            asyncio.ensure_future(self._confirm(hold))
        elif self._hold_settled() and not hold.checking:
            self._settle(hold)  # it ended inside the output delay, or was someone quieter: not a message

    def _as_loud_as_owner(self, heard):
        """Someone nearby is quieter than the wearer. Until the owner has spoken, nothing can be compared."""
        return bool(heard) and self.owner_level is not None and \
            speaking_level(heard) >= self.owner_level - OWNER_MARGIN_DB

    def _hold_settled(self):
        return self.detector.quiet * SECONDS >= HOLD_WAIT_SECONDS

    async def _confirm(self, hold):
        """Still speech as loud as the owner, past the output delay. The words rule out a cough and an echo."""
        try:
            text = await self._transcribe(np.concatenate(self.detector.audio))
        except (RuntimeError, ValueError):
            return  # the utterance ended first, or the gateway is stopping
        if hold is not self.hold:
            return
        if self._as_loud_as_owner(self.detector.speech_levels[hold.level_mark:]) and self._is_owner(text, hold):  # the level again: a peak can pass alone
            self._talked_over(text, hold)
        elif self._hold_settled():
            self._settle(hold)  # it wasn't a message, so the reply needn't wait for one to end
        else:
            hold.mark = self.detector.voiced
            hold.checking = False

    def _settle(self, hold):
        """The speech under this hold ended without being a message. Only a lone word can still count."""
        if self._as_loud_as_owner(hold.levels) and self.detector.voiced * SECONDS <= LONE_WORD_SECONDS:
            hold.checking = True
            asyncio.ensure_future(self._check_word(hold))
        else:
            self.detector.reset()
            self._release_hold()

    async def _check_word(self, hold):
        try:
            text = await self._transcribe(np.concatenate(self.detector.audio))
        except (RuntimeError, ValueError):
            return  # the gateway is stopping
        if hold is not self.hold:
            return
        if not self._hold_settled():
            hold.checking = False  # they carried on, so it wasn't a lone word
            return
        self.detector.reset()
        self._release_hold()
        wanted = "resume" if self.paused else "pause"
        if kind(text) == wanted and not self._repeats_played(spoken_words(text)[0], hold):
            self._set_paused(not self.paused)

    def _is_owner(self, text, hold):
        if kind(text) in ("noise", "pause"):  # a lone "pause" stops no turn, so it waits to see if more follows
            return False
        return len(words_of(text)) < ECHO_MIN_WORDS or not self._repeats_played(text, hold)

    def _repeats_played(self, text, hold):
        words = words_of(text)
        played = [word for at, word in self.played_words if hold.at - ECHO_SECONDS <= at <= hold.at]
        return any(played[i:i + len(words)] == words for i in range(len(played) - len(words) + 1))

    def _talked_over(self, text, hold):
        """As a tap does, if the sound was a reply. Talking over a notice cuts only the notices."""
        if self._repeats_played(text, hold):
            self.phone.send_control({"t": "leak"})  # it may have been the earbuds, so don't trip on them again
        self._log_hold("talked-over", hold)
        self.hold = None
        self.over_a_reply = True
        if not self.reply_heard:
            self._cut_notices()
            return
        stop_turn = self.reply_turn
        self._cut_reply()
        self.cut_unheard = self.over_a_reply = True
        session = self._chosen_session()
        if session and stop_turn:
            self._send_order(session, "abort", self.reply_turn)
            self._drop_reply()
            for metrics in self.prompt_metrics.values():  # the abort stops each of these before its turn
                metrics.times["dropped"] = 1
            self._report_waiting_prompts()

    def _release_hold(self):
        hold, self.hold = self.hold, None
        # Speech that ended inside the output delay may be the earbuds, so the phone should expect it that
        # loud. Speech that carried on past it, or was as loud as the owner, may be the owner, whose voice
        # mustn't deafen the phone's gate.
        leak = hold.mark is None and hold.voiced_in_delay > 0 and not self._as_loud_as_owner(hold.levels) \
            and not self.paused  # that may be whoever the owner is talking to
        self.phone.send_control({"t": "resume", "id": hold.id, "leak": leak})
        if self.played_until > hold.at:  # what the phone kept back still has to play
            self.played_until += self.clock() - hold.at
            self.state_notice_ends += self.clock() - hold.at
        self._log_hold("resumed", hold)

    def _log_hold(self, outcome, hold):
        """What a walk needs, to judge OUTPUT_DELAY_SECONDS and OWNER_MARGIN_DB. Levels and counts, never words."""
        heard = hold.levels[hold.level_mark:] if hold.mark is not None else []
        log.info("hold %s", json.dumps({
            "outcome": outcome,
            "ms": round((self.clock() - hold.at) * 1000),
            "voiced_in_delay": hold.voiced_in_delay,
            "voiced_after": len(heard),
            "level": round(speaking_level(heard), 1) if heard else None,
            "owner_level": round(self.owner_level, 1) if self.owner_level is not None else None,
        }))

    # --- turns of speech ---------------------------------------------------------

    async def _transcribe(self, audio):
        return await asyncio.get_running_loop().run_in_executor(self.stt_pool, self.speech.transcribe, audio)

    def _turn_finished(self, text, last_voice, longest_pause=0.0):
        text = sanitize_prompt(text)
        what = kind(text)
        if self.hold:  # it ended without ever counting as the owner
            self._release_hold()
            return
        if self.paused:
            if what == "resume":
                self._set_paused(False)
            return
        if what == "noise":
            return
        if self.detector.finished_level is not None:
            self.owner_level = self.detector.finished_level
        self.active_at = self.clock()
        readback, confirming = self.readback, self.confirming
        if what != "yes":
            self.readback = None
            self._drop_confirm()
        if what == "pause":
            self._set_paused(True)
            return
        if what == "cancel":
            desk = self._chosen_session()
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
        if what == "no" and session.front_desk and (confirming or readback and self.clock() <= readback.expires):
            self.say(NOT_CONFIRMED)
            self._release_relays()
            return
        if what == "yes" and session.front_desk:
            self._confirm_readback(session)
            self._release_relays()
            return
        target = switch_target(text)
        if target is not None and self._switch(session, target):
            self._release_relays()
            return
        metrics = Metrics(last_voice, self.clock)
        metrics.times["longest_pause"] = round(longest_pause * 1000)
        for name, at in self.detector.finished_marks.items():
            metrics.mark(name, at)
        metrics.mark("submitted")
        unheard = self._reply_unheard() or self.cut_unheard
        over_a_reply = self.over_a_reply
        self.cut_unheard = self.over_a_reply = False
        order = self._send_order(session, "submit", text, unheard, over_a_reply)
        self.prompt_metrics[order] = metrics
        asyncio.get_running_loop().call_later(ORDER_WAIT_SECONDS, self._check_order, order)

    def _set_paused(self, paused):
        """Pausing and resuming are messages, so they leave nothing to say yes to."""
        self.paused = paused
        self.active_at = self.clock()
        self.readback = None
        self._drop_confirm()
        self._save_paused()
        self.say(PAUSED if paused else LISTENING)

    def _save_paused(self):
        if not self.pause_file:
            return
        try:
            if self.paused:
                os.close(os.open(self.pause_file, os.O_WRONLY | os.O_CREAT, 0o600))
            elif os.path.exists(self.pause_file):
                os.remove(self.pause_file)
        except OSError:
            log.exception("couldn't save whether listening is paused")

    def _missed_start(self):
        if not self.paused:  # the words wouldn't have counted anyway
            self._tone("warn")

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
        self._send_order(session, "type", offer[0])

    def _reply_unheard(self):
        """Whether a reply is still on its way out: the session may be idle, but its words haven't been heard."""
        return self._reply_pending() or bool(self.held_relays)

    def _confirm_readback(self, session):
        readback, self.readback = self.readback, None
        if not readback or self.clock() > readback.expires:
            self.say(NOTHING_TO_CONFIRM)
            return
        nonce = security.new_nonce()
        self.confirming = (nonce, readback, self.clock() + CONFIRM_SECONDS)
        self.phone.send_control({"t": "confirm", "nonce": nonce, "text": readback.spoken})
        self.say(CONFIRM_IN_APP)

    # --- switching sessions by voice ------------------------------------------------

    def _switch(self, session, target):
        """A spoken switch. It asks the app for a fingerprint over the session it names, so a voice alone opens nothing.

        A name that matches no session isn't a switch, so "switch to the main branch" goes to the session as a prompt.
        """
        listed = [s for s in self.modlink.sessions.values() if s.nonce]
        if target:
            matches = [s for s in listed if compact(s.name) == target]
            if not matches:
                return False
            matches = [s for s in matches if s is not session]
            if not matches:
                self._notice("already-on", ALREADY_ON % session.name)
                return True
        else:
            matches = [s for s in listed if s is not session]
            if not matches:
                self.say(NO_OTHER_SESSION)
                return True
        if len(matches) > 1:
            self._notice("switch-which", SWITCH_WHICH % ", ".join(sorted(s.name for s in matches)))
            return True
        switch = Switch(matches[0])
        self._tone("ack")
        nonce = security.new_nonce()
        self.confirming = (nonce, switch, self.clock() + CONFIRM_SECONDS)
        self.phone.send_control({"t": "confirm", "nonce": nonce, "text": "Switch to %s?" % switch.name,
                                 "switch": {"id": switch.instance, "modNonce": switch.mod_nonce}})
        self._notice("switch-in-app", SWITCH_IN_APP % switch.name)
        return True

    def _switched(self, nonce, switch, expires, signature):
        """A fingerprint in the app signed the session the switch named, over this run, exactly as at Start."""
        message = security.start_message(nonce, self.phone.run_id, switch.instance, switch.mod_nonce)
        if (not self.phone.approved or self.clock() > expires or not self.phone.signed(message, signature)
                or self.start_problem(switch.instance, switch.mod_nonce)):
            self._not_switched()
            return
        log.info("a fingerprint switched sessions")
        self.phone_approved({"session": switch.instance, "modNonce": switch.mod_nonce, "nonce": nonce,
                             "run": self.phone.run_id, "sig": signature})

    def _not_switched(self):
        session = self._chosen_session()
        if session:
            self._notice("not-switched", NOT_SWITCHED % session.name)

    def _notice(self, name, text):
        log.info("notice %s", name)
        self.say(text)

    def _confirmed(self, nonce, signature):
        """A fingerprint in the app, over the words the owner heard. Only now does the yes go to the session."""
        confirming = self.confirming
        if not confirming or nonce != confirming[0]:
            return
        self._drop_confirm()
        if isinstance(confirming[1], Switch):
            self._switched(nonce, confirming[1], confirming[2], signature)
            return
        session = self._chosen_session()
        readback = confirming[1]
        if (session is None or not session.front_desk or self.clock() > confirming[2]
                or not self.phone.signed(security.confirm_message(nonce, readback.spoken), signature)):
            self.say(NOT_CONFIRMED)
            return
        self._send_order(session, "confirm", {"note": readback.note, "spoken": readback.spoken})

    def _drop_confirm(self):
        if self.confirming:
            self.confirming = None
            self.phone.send_control({"t": "confirm", "nonce": None})

    def _send_order(self, session, what, value, unheard=False, over_a_reply=False):
        order = "%s-%d" % (self.order_prefix, self.next_order)
        self.next_order += 1
        self.orders[order] = (what, value, over_a_reply)
        session.send({"id": order, what: value, "unheard": True} if unheard else {"id": order, what: value})
        return order

    def _check_order(self, order):
        """The mod never picked this up, or the engine hasn't started it. One in the queue is fine: it's waiting."""
        if order not in self.orders:
            return
        if order not in self.accepted:
            self._not_delivered()
            self.say(NOT_REACHED)
        elif order in self.prompt_metrics and "queued" not in self.prompt_metrics[order].times:
            self.say(NOT_STARTED)

    def _delivered(self):
        if self.heard:
            log.info("notice heard")
            self.say(random.choice(HEARD_WORDS))

    def _not_delivered(self):
        if self.heard:
            self._tone("warn")

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
            self.reply_voice = self.voice
            self.reply_metrics = self.prompt_metrics.pop(event.get("order"), None)
            if self.reply_metrics:
                self.reply_metrics.mark("turn_start")
        elif what == "text" and self.reply_filter and event.get("turnId") == self.reply_turn:
            if self.reply_metrics:
                self.reply_metrics.mark("first_text")
            for unit in self.reply_filter.feed(event.get("text") or ""):
                self.say(unit, self.reply_metrics, self.reply_voice)
        elif what == "turn-end" and event.get("turnId") == self.reply_turn:
            for unit in self.reply_filter.finish() if self.reply_filter else []:
                self.say(unit, self.reply_metrics, self.reply_voice)
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
            self.say(unit, reply_voice=self.voice, relayed=relayed)
        self.say_queue.append((relayed, None, self.voice, None))
        self.say_ready.set()

    def _release_relays(self):
        held, self.held_relays = self.held_relays or [], None
        for relayed in held:
            self._queue_relay(relayed)

    def _queued_relays(self):
        return [item[0] for item in (*self.frames, *self.say_queue) if isinstance(item[0], Relayed)]

    def _reply_not_spoken(self, relayed):
        """Its words go, and the notice takes their place. Its marker stays, so it can't take a yes or leave an older reply able to."""
        relayed.units, relayed.takes_yes = [], False
        self.say_queue = collections.deque(item for item in self.say_queue if item[3] is not relayed)
        log.info("notice %s", NOTICE_NAMES[REPLY_NOT_SPOKEN])
        self.say_queue.appendleft((REPLY_NOT_SPOKEN, None, None, None))

    def _order_result(self, event):
        order_id = event.get("id")
        if event.get("accepted"):
            if order_id in self.orders:
                self.accepted.add(order_id)
                if event.get("busy"):
                    self.say(QUEUED)
                    if order_id in self.prompt_metrics:
                        self.prompt_metrics[order_id].times["queued"] = 1
                elif self.orders[order_id][0] == "submit":
                    self._delivered()
            return
        order = self.orders.pop(order_id, None)
        self.accepted.discard(order_id)
        if not order:
            return
        what, value, over_a_reply = order
        if what == "abort" and event.get("dropped"):
            self.say(QUEUE_DROPPED)
        if what == "confirm":
            self.say(YES_SENT if event.get("ok") else YES_NOT_SENT)
            return
        if event.get("ok") or what == "abort":
            desk = self._chosen_session()
            if event.get("ok") and what == "submit" and desk and desk.front_desk:
                self._delivered()  # a front desk takes a prompt with no wait, so it says ok at once
            return
        metrics = self.prompt_metrics.pop(order_id, None)  # this prompt's turn won't come
        if metrics:
            metrics.times["dropped" if event.get("dropped") else "refused"] = 1
            metrics.report()
        if event.get("dropped"):
            return
        if what == "submit":
            if event.get("drop"):
                # What's said over a reply may be its echo, so it can't replace words waiting to be typed.
                if not (over_a_reply and self.typing_offer and self.clock() <= self.typing_offer[1]):
                    self.typing_offer = (value, self.clock() + TYPING_OFFER_SECONDS)
                self._not_delivered()
                self.say(STOPPED_AT_KEYBOARD)
                if event.get("later"):
                    self.say(LATER_DROPPED)
            else:
                self._not_delivered()
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

    def say(self, text, metrics=None, reply_voice=None, relayed=None):
        """A reply's words come with the voice its turn began with. A notice uses the current one."""
        if text in NOTICE_NAMES:
            log.info("notice %s", NOTICE_NAMES[text])
        unit = (text, metrics, reply_voice, relayed)
        if is_state_notice(unit):
            self._drop_unsent_state_notice()
            self.state_notice, self.state_notice_ends = text, math.inf
            self.say_queue.appendleft(unit)
        else:
            self.say_queue.append(unit)
        self.say_ready.set()

    def _drop_unsent_state_notice(self):
        self.frames = collections.deque(frame for frame in self.frames if not frame[5])
        self.say_queue = collections.deque(unit for unit in self.say_queue if not is_state_notice(unit))
        if self.synthesizing and is_state_notice(self.synthesizing):
            self.synthesizing = None

    async def _speak_loop(self):
        """Synthesize ahead: the next sentence is made while the current one plays."""
        loop = asyncio.get_running_loop()
        while True:
            await self.say_ready.wait()
            if not self.say_queue:
                self.say_ready.clear()
                continue
            unit = self.say_queue.popleft()
            text, metrics, reply_voice, relayed = unit
            if isinstance(text, Relayed):
                self.frames.append((text, None, True, [], None, False))
                self.frames_ready.set()
                continue
            if metrics:
                metrics.mark("first_unit")
            self.synthesizing = unit
            packets = []
            try:
                packets = await loop.run_in_executor(self.tts_pool, self._synthesize, text, reply_voice or self.voice)
            except Exception:
                log.exception("synthesis failed")
                if relayed and self.synthesizing is unit:
                    self._reply_not_spoken(relayed)
            if self.synthesizing is not unit:
                continue
            self.synthesizing = None
            if not packets:
                continue
            if metrics:
                metrics.mark("first_audio_ready")
            words = words_of(text)
            state_notice = is_state_notice(unit)
            made = [(packet, metrics, reply_voice is not None,
                     words[i * len(words) // len(packets):(i + 1) * len(words) // len(packets)], relayed, state_notice)
                    for i, packet in enumerate(packets)]
            if state_notice:
                self.frames.extendleft(reversed(made))
            else:
                self.frames.extend(made)
            self.frames_ready.set()

    def _synthesize(self, text, voice):
        return self.encoder.encode(self.speech.synthesize(text, voice))

    # --- the voice ---------------------------------------------------------------

    def _load_voice(self):
        try:
            with open(self.voice_file) as f:
                saved = f.read().strip()
        except (OSError, TypeError):
            return DEFAULT_VOICE
        return saved if any(saved == name for name, _ in VOICES) else DEFAULT_VOICE

    def _choose_voice(self, voice):
        if not any(voice == name for name, _ in VOICES):
            return
        self.voice = voice
        if self.voice_file:
            try:
                fd = os.open(self.voice_file + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(voice)
                os.replace(self.voice_file + ".new", self.voice_file)
            except OSError:
                log.exception("couldn't save the voice")
        self._send_voices()

    def _send_voices(self):
        self.phone.send_control({
            "t": "voices",
            "list": [{"id": name, "name": label} for name, label in VOICES],
            "chosen": self.voice,
        })

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
            if self.hold and now - self.last_mic > MIC_STALE_SECONDS:
                self.detector.reset()  # unconfirmed, so there's no message to keep
                self._release_hold()
            if not mid_message and not self.hold and self.phone and self.phone.connected:
                while self.frames and self.played_until - now < LEAD_SECONDS:
                    if isinstance(self.frames[0][0], Relayed):  # no sound: it marks where a relayed reply ends
                        packet = self.frames.popleft()[0]
                        self.sounding_relay = None
                        if packet.takes_yes and not self._queued_relays():
                            packet.expires = self.played_until + READBACK_SECONDS
                            self.readback = packet
                        else:
                            self.readback = None
                        continue
                    if self.frames[0][2] != self.reply_heard and now < self.played_until + OUTPUT_DELAY_SECONDS:
                        break  # a reply and a notice never sound together, so one cut can't take the other
                    self.played_until = max(self.played_until, now)
                    packet, metrics, is_reply, words, relayed, state_notice = self.frames.popleft()
                    self.phone.send_speech(self.gen, packet)
                    self.sounding_relay = relayed
                    self.played_until += FRAME_SECONDS
                    if state_notice and not (self.frames and self.frames[0][5]):
                        self.state_notice_ends = self.played_until
                    self.played_words.extend((self.played_until, word) for word in words)
                    self.reply_heard = is_reply
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
