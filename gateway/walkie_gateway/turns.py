"""Finds where a message ends, and has its words ready by then.

A pause doesn't end a message: silence does, and it has to last.

- Two seconds of silence end a message.
- Three seconds do, if the person has already paused and carried on, or stopped on
  a word that usually has more after it, like "and" or "to". They're thinking aloud.
- Punctuation plays no part, because the transcriber adds a full stop to a fragment.
- Once someone is talking, speech carries the message on, unless it's far quieter
  than the message so far. That leaves out a quieter voice nearby. A voice about as
  close as the wearer's still joins, because nothing here can tell who's speaking.
- A message never starts on speech that has lost its start: audio that went missing,
  or someone who was talking under a reply and hasn't stopped by the time it ends.

A start that turns out to be noise lets go of what followed it, so a cough doesn't cost
the words after it, and the same goes for what's said past the two-minute limit.

Transcription starts before the silence is up, so the wait pays for only part of it.
A message ends only with a transcript in hand, and noise never reaches the conversation.
"""
import asyncio
import collections
import math
import statistics
import time

import numpy as np

from .text import kind, trails_off

RATE = 16000
WINDOW = 512            # samples Silero takes at a time: 32 ms
SECONDS = WINDOW / RATE
START_WINDOWS = 3       # this many voiced windows in a row start an utterance
PREROLL = 10            # windows kept from before it started
MIN_VOICED = 8          # an utterance with fewer voiced windows is noise, never transcribed
CONTINUE_WINDOWS = 2    # this many windows of speech in a row means the person is talking again
NOISE_HOLD = 0.5        # seconds of quiet that end a noise, which never gets as far as a message
END_HOLD = 1.9          # seconds of quiet that end a message; Silero's own 0.1 s makes it 2
THINKING_HOLD = 2.9     # the same, for someone thinking aloud; 3 with Silero's
THINKING_PAUSE = 0.7    # a gap this long, with talking after it, means they're thinking aloud
TRANSCRIBE_LEAD = 0.5   # transcription starts this long before the silence is up
TRANSCRIBE_PER_SECOND = 0.1  # and this much sooner for each second of audio to get through
TRANSCRIBE_AFTER = 0.25 # but no sooner than this into the silence
MAX_SECONDS = 120       # a message this long ends here: past about this, Parakeet starts dropping words
CARRY_DB = 15           # speech this far below the message's own median level doesn't carry it on
CARRY_WINDOWS = 6       # how much of it is compared: 0.2 s, because a quieter voice's loudest moments can pass alone
CARRIED_TAIL = 10       # windows of audio kept after the last words that carried the message on
SKIP_HOLD = 0.7         # seconds with no voice that mean speech which lost its start has ended
# Carried over from walk mode. A voiced window must also be this much louder
# than the quietest of the last four seconds, which keeps a passer-by from starting a message.
MARGIN_DB = 12
MIN_DB = -60
BACKGROUND_WINDOWS = 125
FLOOR_READY_WINDOWS = 31  # for the first second, the floor can't be louder than a quiet room


def loudness(samples):
    """A window's loudness in dBFS, after a first difference that takes out
    most of the rumble of wind and footsteps."""
    diff = np.diff(samples.astype(np.float64), prepend=float(samples[0]))
    total = float(np.dot(diff, diff))
    return 10 * math.log10(total / len(samples)) - 90.3 if total else -120.0


class TurnDetector:
    """Feed it 16 kHz mono samples. It calls on_turn(text, last_voice_time, longest_pause) once per message."""

    def __init__(self, is_speech, transcribe, on_turn, clock=time.monotonic, on_missed_start=lambda: None):
        self.is_speech = is_speech      # one window (int16) -> bool
        self.transcribe = transcribe    # audio (int16) -> awaitable text
        self.on_turn = on_turn
        self.clock = clock
        self.on_missed_start = on_missed_start  # someone's talking, and their words won't count
        self.levels = collections.deque(maxlen=BACKGROUND_WINDOWS)
        self.rest = np.zeros(0, dtype=np.int16)
        self.skipping = False   # speech that lost its start is still going
        self.skipped_at = 0.0   # when speech was last skipped: anything said around then may be missing its start
        self._idle()

    def _idle(self):
        self.talking = False
        self.before = collections.deque(maxlen=PREROLL)
        self.run_levels = []    # how loud each voiced window in a row was
        self.audio = []
        self.voice_levels = []  # how loud this message's voiced windows were
        self.spoken = 0         # how much of the audio runs up to the last words that carried it on
        self.held = []          # what's heard that may belong to the next message: after a start that may be noise, or past the limit
        self.voiced = 0
        self.quiet = 0
        self.speech_run = 0
        self.longest_pause = 0.0
        self.last_voice = 0.0
        self.attempt = None

    def reset(self):
        """Forget anything in progress: nobody hears the end of it now."""
        if self.attempt:
            self.attempt.cancel()
        self.rest = np.zeros(0, dtype=np.int16)
        self.skipping = False
        self._idle()

    def skip_speech(self):
        """What's being said has lost its start, so nothing starts until the person stops. One warning is enough."""
        if not self.skipping:
            self.skip_warned = False
        self.skipping = True
        self.skip_quiet = 0
        self.skipped_at = self.clock()

    def feed(self, samples, ignored=False):
        self.rest = np.concatenate((self.rest, samples))
        start = 0
        while len(self.rest) - start >= WINDOW:
            self._window(self.rest[start:start + WINDOW], ignored)
            start += WINDOW
        self.rest = self.rest[start:].copy()

    def _hears(self, window):
        """Whether this window is speech, whether it's also loud enough to count as the wearer's, and its level."""
        level = loudness(window)
        self.levels.append(level)
        floor = min(self.levels) if len(self.levels) > FLOOR_READY_WINDOWS else MIN_DB - MARGIN_DB
        speech = self.is_speech(window)  # always: Silero keeps state across windows
        return speech, speech and level > max(floor + MARGIN_DB, MIN_DB), level

    def _hold(self):
        return THINKING_HOLD if self.longest_pause >= THINKING_PAUSE else END_HOLD

    def _window(self, window, ignored):
        speech, voiced, level = self._hears(window)
        if not ignored:
            recent = statistics.median(list(self.levels)[-CARRY_WINDOWS:])
            self._step(window, speech, voiced, level, recent)
            return
        self.before.append(window)
        self._count_run(voiced, level)
        if len(self.run_levels) >= START_WINDOWS:
            self.skip_speech()
        if self.skipping:
            self._skip(voiced)

    def _count_run(self, voiced, level):
        if voiced:
            self.run_levels.append(level)
        else:
            self.run_levels.clear()

    def _skip(self, voiced):
        self.skip_quiet = 0 if voiced else self.skip_quiet + 1
        if voiced:
            self.skipped_at = self.clock()
        if self.skip_quiet * SECONDS >= SKIP_HOLD:
            self.skipping = False

    def _step(self, window, speech, voiced, level, recent):
        if not self.talking:
            self.before.append(window)
            self._count_run(voiced, level)
            if self.skipping:
                if len(self.run_levels) >= START_WINDOWS and not self.skip_warned:
                    self.skip_warned = True
                    self.on_missed_start()
                self._skip(voiced)
            elif len(self.run_levels) >= START_WINDOWS:
                self.talking = True
                self.audio = list(self.before)
                self.spoken = len(self.audio)
                self.voice_levels = list(self.run_levels)
                self.voiced = len(self.run_levels)
                self.last_voice = self.clock()
            return
        too_long = len(self.audio) * SECONDS >= MAX_SECONDS
        if too_long or self.voiced < MIN_VOICED:
            self.held.append((window, speech, voiced, level, recent))
        if not too_long:
            self.audio.append(window)
        close = recent >= statistics.median(self.voice_levels) - CARRY_DB
        if voiced and close and not too_long:
            self.voiced += 1
            self.voice_levels.append(level)
            if self.voiced >= MIN_VOICED:
                self.held.clear()
        self.speech_run = self.speech_run + 1 if speech and close else 0
        if self.speech_run >= CONTINUE_WINDOWS and not too_long:
            self._carry_on()
            return
        self.quiet += 1
        quiet = self.quiet * SECONDS
        if self.voiced < MIN_VOICED:
            if quiet >= NOISE_HOLD:
                self._next_message()
            return
        if (too_long or quiet >= self._transcribe_at()) and self.attempt is None:
            self.attempt = asyncio.ensure_future(self.transcribe(np.concatenate(self.audio[:self.spoken + CARRIED_TAIL])))
        self._maybe_finish(quiet, too_long)

    def _carry_on(self):
        """Talking again, so whatever silence came before was only a pause."""
        pause = self.quiet * SECONDS
        self.longest_pause = max(self.longest_pause, pause)
        self.quiet = 0
        self.spoken = len(self.audio)
        self.last_voice = self.clock()
        if self.attempt:
            self.attempt.cancel()  # that transcript is out of date
            self.attempt = None

    def _transcribe_at(self):
        """Soon enough to be done as the silence ends, late enough not to redo it at every pause."""
        audio_seconds = len(self.audio) * SECONDS
        return max(TRANSCRIBE_AFTER, self._hold() - TRANSCRIBE_LEAD - TRANSCRIBE_PER_SECOND * audio_seconds)

    def _maybe_finish(self, quiet, too_long):
        if not self.attempt or not self.attempt.done() or (quiet < self._hold() and not too_long):
            return
        if self.attempt.cancelled() or self.attempt.exception():
            self._next_message()
            return
        text = self.attempt.result()
        if trails_off(text) and kind(text) == "prompt" and quiet < THINKING_HOLD and not too_long:
            return
        last_voice, longest_pause = self.last_voice, self.longest_pause
        self._next_message()
        self.on_turn(text, last_voice, longest_pause)

    def _next_message(self):
        held = self.held
        self._idle()
        for heard in held:
            self._step(*heard)
