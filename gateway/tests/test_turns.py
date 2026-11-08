import asyncio
import unittest

import numpy as np

from walkie_gateway.turns import (
    CARRIED_TAIL,
    CARRY_DB,
    END_HOLD,
    MAX_SECONDS,
    NOISE_HOLD,
    PREROLL,
    RATE,
    SECONDS,
    SKIP_HOLD,
    THINKING_HOLD,
    THINKING_PAUSE,
    TRANSCRIBE_LEAD,
    TRANSCRIBE_PER_SECOND,
    WINDOW,
    TurnDetector,
)

LOUD = np.array(([3000] * 8 + [-3000] * 8) * (WINDOW // 16), dtype=np.int16)
QUIET = np.zeros(WINDOW, dtype=np.int16)
SOFT = np.array(([40] * 8 + [-40] * 8) * (WINDOW // 16), dtype=np.int16)


def quieter(db):
    """LOUD, this many dB down."""
    return (LOUD * 10 ** (-db / 20)).astype(np.int16)


# Far enough into a one-second utterance's silence for its transcription to have started.
TRANSCRIBING = END_HOLD - TRANSCRIBE_LEAD - TRANSCRIBE_PER_SECOND + 0.1


class Harness:
    """A detector with a voice detector that goes by loudness, and a transcriber that answers at once."""

    def __init__(self, text="hello there"):
        self.text = text
        self.turns = []
        self.pauses = []
        self.transcribed = 0
        self.audio = None
        self.warned = 0
        self.detector = TurnDetector(lambda w: bool(w.any()), self._transcribe, self._turn,
                                     on_missed_start=self._warn)

    def _warn(self):
        self.warned += 1

    async def _transcribe(self, audio):
        self.transcribed += 1
        self.audio = audio
        return self.text

    def _turn(self, text, last_voice, longest_pause):
        self.turns.append(text)
        self.pauses.append(longest_pause)

    async def play(self, windows, seconds, ignored=False):
        for _ in range(int(seconds / SECONDS)):
            self.detector.feed(windows, ignored)
            await asyncio.sleep(0)


class TurnDetectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_turn_ends_after_the_hold(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        self.assertTrue(h.detector.talking)
        await h.play(QUIET, END_HOLD - 0.1)
        self.assertEqual(h.turns, [])
        await h.play(QUIET, 0.2)
        self.assertEqual(h.turns, ["hello there"])
        self.assertFalse(h.detector.talking)

    async def test_a_natural_pause_does_not_end_a_message(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        for pause in (0.5, 0.9, 1.3, 1.8):
            await h.play(LOUD, 1)
            await h.play(QUIET, pause)
            self.assertEqual(h.turns, [], "a pause of %g s ended the message" % pause)
        await h.play(LOUD, 1)
        await h.play(QUIET, THINKING_HOLD + 0.2)
        self.assertEqual(len(h.turns), 1)

    async def test_someone_thinking_aloud_gets_the_longer_hold(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        await h.play(QUIET, THINKING_PAUSE + 0.1)
        await h.play(LOUD, 1)
        await h.play(QUIET, END_HOLD + 0.2)
        self.assertEqual(h.turns, [])
        await h.play(QUIET, THINKING_HOLD - END_HOLD)
        self.assertEqual(len(h.turns), 1)
        self.assertGreaterEqual(h.pauses[0], THINKING_PAUSE)

    async def test_a_breath_does_not_count_as_thinking(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        await h.play(QUIET, THINKING_PAUSE - 0.3)
        await h.play(LOUD, 1)
        await h.play(QUIET, END_HOLD + 0.2)
        self.assertEqual(len(h.turns), 1)

    async def test_transcription_starts_before_the_silence_is_up(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        await h.play(QUIET, TRANSCRIBING)
        self.assertEqual(h.transcribed, 1)
        self.assertEqual(h.turns, [])

    async def test_a_short_pause_costs_no_transcription(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        await h.play(QUIET, 0.5)
        self.assertEqual(h.transcribed, 0)

    async def test_talking_again_cancels_the_early_transcript(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        await h.play(QUIET, TRANSCRIBING)
        await h.play(LOUD, 1)
        await h.play(QUIET, THINKING_HOLD + 0.1)
        self.assertEqual(h.transcribed, 2)
        self.assertEqual(len(h.turns), 1)

    async def test_a_trailing_word_stretches_the_hold(self):
        h = Harness("I want to fix that and")
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        await h.play(QUIET, END_HOLD + 0.2)
        self.assertEqual(h.turns, [])
        await h.play(QUIET, THINKING_HOLD - END_HOLD)
        self.assertEqual(h.turns, ["I want to fix that and"])

    async def test_a_full_stop_on_a_fragment_does_not_end_it_early(self):
        # The transcriber writes this for a fragment: the words count, not the punctuation.
        h = Harness("And maybe we should.")
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        await h.play(QUIET, END_HOLD + 0.2)
        self.assertEqual(h.turns, [])

    async def test_soft_speech_is_not_silence_once_someone_is_talking(self):
        h = Harness()
        await h.play(quieter(20), 4.5)  # wind: the floor rises with it
        await h.play(LOUD, 1)
        await h.play(quieter(11), 2.5)  # too near the wind for the loudness check, but it's the same person
        self.assertEqual(h.turns, [])
        self.assertLessEqual(h.detector.quiet, 1)

    async def test_the_same_person_speaking_softly_carries_a_message_on(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        await h.play(quieter(CARRY_DB - 3), END_HOLD + 0.5)
        self.assertEqual(h.turns, [])
        self.assertLessEqual(h.detector.quiet, 1)

    async def test_a_much_quieter_voice_does_not_carry_a_message_on(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        await h.play(quieter(CARRY_DB + 9), END_HOLD + 0.2)
        self.assertEqual(h.turns, ["hello there"])
        self.assertLess(h.pauses[0], THINKING_PAUSE)  # it never counted as the person carrying on

    async def test_the_loudest_moments_of_a_quieter_voice_do_not_carry_a_message_on(self):
        # A voice's syllables peak well above its own median, so one loud window proves nothing.
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        syllable = np.concatenate([quieter(CARRY_DB - 3)] * 2 + [quieter(CARRY_DB + 3)] * 4 + [quieter(CARRY_DB + 9)] * 4)
        await h.play(syllable, (END_HOLD + 0.6) / 10)
        self.assertEqual(h.turns, ["hello there"])

    async def test_a_much_quieter_voice_stays_out_of_the_words(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        await h.play(QUIET, 0.5)
        far = quieter(CARRY_DB + 9)
        await h.play(far, 5)
        self.assertEqual(len(h.turns), 1)
        self.assertLessEqual(len(h.audio) / RATE, PREROLL * SECONDS + 1 + CARRIED_TAIL * SECONDS + 2 * SECONDS)
        self.assertFalse(np.array_equal(h.audio[-WINDOW:], far))

    async def test_a_much_quieter_voice_does_not_lower_the_level_it_is_held_to(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 0.4)  # a short message, soon outnumbered if the quieter voice counted
        await h.play(quieter(CARRY_DB + 9), END_HOLD + 0.2)
        self.assertEqual(len(h.turns), 1)

    async def test_a_quieter_voice_does_not_hold_a_message_up_after_a_cough(self):
        h = Harness()
        far = quieter(CARRY_DB + 9)
        await h.play(far, 1.5)  # a conversation nearby: the floor rises with it, so it can't start a message itself
        h.detector.reset()
        await h.play(far, 4.5)
        await h.play(LOUD, 4 * SECONDS)
        await h.play(far, NOISE_HOLD + 0.3)
        self.assertEqual(h.transcribed, 0)
        self.assertFalse(h.detector.talking)

    async def test_speech_going_as_the_mic_opens_never_starts_a_message(self):
        # What the adversarial review heard: "Do not delete the old release branch" arrived as "the old release branch".
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1, ignored=True)
        await h.play(LOUD, 1)
        self.assertFalse(h.detector.talking)
        self.assertEqual(h.warned, 1)
        await h.play(QUIET, THINKING_HOLD + 0.2)
        self.assertEqual(h.turns, [])
        self.assertEqual(h.transcribed, 0)

    async def test_a_short_pause_does_not_end_speech_that_lost_its_start(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1, ignored=True)
        await h.play(LOUD, 0.5)
        await h.play(QUIET, SKIP_HOLD - 0.2)
        await h.play(LOUD, 1)
        self.assertFalse(h.detector.talking)
        self.assertEqual(h.warned, 1)

    async def test_the_next_message_after_speech_that_lost_its_start_is_whole(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1, ignored=True)
        await h.play(LOUD, 0.5)
        await h.play(QUIET, SKIP_HOLD + 0.1)
        await h.play(LOUD, 1)
        await h.play(QUIET, END_HOLD + 0.2)
        self.assertEqual(h.turns, ["hello there"])

    async def test_speech_that_starts_just_as_the_mic_opens_keeps_its_start(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(QUIET, 1, ignored=True)
        await h.play(LOUD, 2 * SECONDS, ignored=True)
        await h.play(LOUD, 1)
        self.assertTrue(h.detector.talking)
        self.assertEqual(h.warned, 0)
        self.assertGreaterEqual(sum(window.any() for window in h.detector.audio), 2 + int(1 / SECONDS))

    async def test_a_comma_as_the_mic_opens_does_not_let_the_rest_of_the_sentence_start_a_message(self):
        # What the end-to-end run heard: "Do not delete the old release branch, just rename it" arrived as "Just rename it."
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1, ignored=True)
        await h.play(QUIET, 0.2, ignored=True)
        await h.play(QUIET, 0.1)  # nobody's talking at the moment it opens
        await h.play(LOUD, 1)
        self.assertFalse(h.detector.talking)
        self.assertEqual(h.warned, 1)
        await h.play(QUIET, THINKING_HOLD + 0.2)
        self.assertEqual(h.turns, [])

    async def test_speech_that_ended_under_a_reply_is_forgotten(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1, ignored=True)
        await h.play(QUIET, SKIP_HOLD + 0.1, ignored=True)
        await h.play(LOUD, 1)
        self.assertTrue(h.detector.talking)
        self.assertEqual(h.warned, 0)

    async def test_nothing_starts_on_speech_after_audio_went_missing(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        h.detector.reset()
        h.detector.skip_speech()
        await h.play(LOUD, 1)
        self.assertFalse(h.detector.talking)
        self.assertEqual(h.warned, 1)
        await h.play(QUIET, THINKING_HOLD + 0.2)
        self.assertEqual(h.turns, [])

    async def test_silence_after_audio_went_missing_lets_the_next_message_start(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        h.detector.reset()
        h.detector.skip_speech()
        await h.play(QUIET, SKIP_HOLD + 0.1)
        await h.play(LOUD, 1)
        await h.play(QUIET, END_HOLD + 0.2)
        self.assertEqual(h.turns, ["hello there"])
        self.assertEqual(h.warned, 0)

    async def test_soft_speech_cannot_start_a_message(self):
        h = Harness()
        await h.play(LOUD, 1.5)  # the floor rises with a loud background
        h.detector.reset()
        await h.play(SOFT, 1)
        self.assertFalse(h.detector.talking)

    async def test_one_stray_window_does_not_restart_the_silence(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 1)
        for _ in range(int(END_HOLD / (10 * SECONDS)) + 1):
            await h.play(LOUD, SECONDS)
            await h.play(QUIET, 9 * SECONDS)
        await h.play(QUIET, 0.2)
        self.assertEqual(len(h.turns), 1)

    async def test_a_click_is_never_transcribed(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 4 * SECONDS)
        await h.play(QUIET, 1)
        self.assertEqual(h.transcribed, 0)
        self.assertEqual(h.turns, [])
        self.assertFalse(h.detector.talking)

    async def test_a_bump_before_speech_keeps_the_opening_words(self):
        h = Harness()
        soft = quieter(20)
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 4 * SECONDS)
        await h.play(soft, 2)
        await h.play(QUIET, END_HOLD + 0.2)
        self.assertEqual(h.turns, ["hello there"])
        windows = [h.audio[i:i + WINDOW] for i in range(0, len(h.audio), WINDOW)]
        self.assertEqual(sum(np.array_equal(window, soft) for window in windows), int(2 / SECONDS))

    async def test_noise_lets_go_quickly(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(LOUD, 4 * SECONDS)
        await h.play(QUIET, NOISE_HOLD + 0.1)
        self.assertFalse(h.detector.talking)

    async def test_a_voice_near_the_background_level_does_not_count(self):
        h = Harness()
        await h.play(LOUD, 1.5)  # a steady loud background: the floor rises with it
        self.assertLess(h.detector.voiced, 60)
        h.detector.reset()
        await h.play(LOUD, 1)
        self.assertFalse(h.detector.talking)

    async def test_a_long_turn_ends_at_the_limit(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        await h.play(np.concatenate([LOUD, LOUD, LOUD, LOUD, QUIET]), MAX_SECONDS / 5 + 1)
        self.assertEqual(len(h.turns), 1)
        self.assertLessEqual(max(len(h.detector.audio) * SECONDS, 0), MAX_SECONDS)

    async def test_words_said_while_a_message_at_the_limit_is_transcribed_start_the_next_one(self):
        await self.check_words_past_the_limit_are_kept(transcriber_fails=False)

    async def test_words_past_the_limit_survive_a_failed_transcription(self):
        await self.check_words_past_the_limit_are_kept(transcriber_fails=True)

    async def test_words_past_the_limit_survive_a_breath_before_the_transcript_lands(self):
        await self.check_words_past_the_limit_are_kept(transcriber_fails=False, breath=0.4)

    async def check_words_past_the_limit_are_kept(self, transcriber_fails, breath=0):
        gate = asyncio.Event()
        lengths = []

        async def slow_transcribe(audio):
            lengths.append(len(audio))
            if len(lengths) == 1:
                await gate.wait()
                if transcriber_fails:
                    raise RuntimeError("no transcript")
            return "words"

        turns = []
        detector = TurnDetector(lambda w: bool(w.any()), slow_transcribe, lambda text, *_: turns.append(text))

        async def play(windows, seconds):
            for _ in range(int(seconds / SECONDS)):
                detector.feed(windows)
                await asyncio.sleep(0)

        speech = np.concatenate([LOUD, LOUD, LOUD, LOUD, QUIET])
        await play(QUIET, 1.5)
        await play(speech, MAX_SECONDS / 5 + 1)
        self.assertEqual(len(lengths), 1)  # the message at the limit is being transcribed
        said_meanwhile = 5  # seconds
        await play(speech, said_meanwhile / 5)
        await play(QUIET, breath)
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await play(QUIET, THINKING_HOLD + 0.2)
        self.assertEqual(turns, ["words"] if transcriber_fails else ["words", "words"])
        self.assertGreaterEqual(lengths[1], said_meanwhile * RATE)

    async def test_any_buffer_size_works(self):
        h = Harness()
        audio = np.concatenate([np.zeros(RATE * 2, dtype=np.int16)] + [LOUD] * 40 + [np.zeros(RATE * 3, dtype=np.int16)])
        for start in range(0, len(audio), 320):
            h.detector.feed(audio[start:start + 320])
            await asyncio.sleep(0)
        self.assertEqual(h.turns, ["hello there"])

    async def test_it_marks_when_speech_was_last_skipped(self):
        h = Harness()
        await h.play(QUIET, 1.5)
        self.assertEqual(h.detector.skipped_at, 0.0)
        await h.play(LOUD, 0.5, ignored=True)
        talked_over = h.detector.skipped_at
        self.assertGreater(talked_over, 0.0)
        await h.play(QUIET, SKIP_HOLD + 0.1)
        self.assertEqual(h.detector.skipped_at, talked_over)
        self.assertFalse(h.detector.skipping)


if __name__ == "__main__":
    unittest.main()
