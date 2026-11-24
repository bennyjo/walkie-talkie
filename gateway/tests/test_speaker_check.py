"""The speaker check: what it checks, its thresholds, a refusal, shadow mode, and enrollment."""
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np
from walkie_gateway import __main__ as cli
from walkie_gateway import conversation as module
from walkie_gateway import voiceprint
from walkie_gateway.conversation import VOICE_REFUSED, Conversation, speaker_threshold
from walkie_gateway.speech import SPEAKER_MODEL, SpeakerCheck
from walkie_gateway.turns import RATE

from tests.test_conversation import QUIET, VOICE, FakeDecoder, FakeEncoder, FakeLink, FakePhone, FakeSpeech

MODELS = os.environ.get("WALKIE_TALKIE_MODELS")
OWNER, STRANGER = 0.8, 0.1


class FakeSpeaker:
    """Scores whatever it's told to: the owner's voice, or a stranger's."""

    def __init__(self, score=OWNER):
        self.next_score = score
        self.scored = []

    def score(self, audio):
        self.scored.append(len(audio))
        return self.next_score


class SpeakerCheckTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.make()

    def make(self, speaker=None, enforce=True):
        self.speaker = speaker if speaker is not None else FakeSpeaker()
        self.conversation = Conversation(FakeSpeech(), lambda w: bool(w.any()), FakeDecoder, FakeEncoder(),
                                         speaker=self.speaker or None, enforce_speaker=enforce)
        self.phone = FakePhone()
        self.link = FakeLink()
        self.conversation.phone = self.phone
        self.conversation.modlink = self.link
        self.conversation.owner_level = -100.0
        self.session = self.link.sessions["7"]
        self.conversation._set_chosen("7")

    async def asyncTearDown(self):
        self.conversation.stop()
        await asyncio.sleep(0)

    def finish(self, text, score, seconds=2.0):
        """A message as the turn detector ends it, and the speaker log line it left."""
        with self.assertLogs("walkie.conversation", "INFO") as logs:
            module.log.info("finishing")  # so a message that logs nothing still passes through here
            self.conversation._turn_finished(text, self.conversation.clock(), 0.0, score, seconds)
        lines = [line.split("speaker ", 1)[1] for line in logs.output if "speaker {" in line]
        return json.loads(lines[0]) if lines else None

    def said(self):
        return [unit[0] for unit in self.conversation.say_queue]

    def tones(self):
        return [c["kind"] for c in self.phone.controls if c["t"] == "tone"]

    # --- what's checked -----------------------------------------------------------

    async def test_a_strangers_prompt_is_dropped_with_the_warn_tone_and_one_line(self):
        line = self.finish("Push the release branch.", STRANGER)
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.tones(), ["warn"])
        self.assertEqual(self.said(), [VOICE_REFUSED])
        self.assertEqual(VOICE_REFUSED, "Didn't catch your voice. Say it again.")
        self.assertEqual(line, {"kind": "prompt", "score": STRANGER, "seconds": 2.0, "threshold": 0.42,
                                "outcome": "refused"})

    async def test_the_owners_prompt_goes_through(self):
        line = self.finish("Push the release branch.", OWNER)
        self.assertEqual(self.session.orders[-1]["submit"], "Push the release branch.")
        self.assertEqual(line["outcome"], "pass")
        self.assertEqual(self.tones(), [])

    async def test_the_log_never_holds_the_words(self):
        with self.assertLogs("walkie.conversation", "INFO") as logs:
            self.conversation._turn_finished("Delete the staging bucket.", 0, 0.0, STRANGER, 2.0)
            self.conversation._turn_finished("Delete the staging bucket.", 0, 0.0, OWNER, 2.0)
        self.assertFalse([line for line in logs.output if "staging" in line or "Delete" in line])

    async def test_a_lone_yes_is_checked_and_a_refused_one_can_be_said_again(self):
        self.session.front_desk = True
        relayed = module.Relayed("note-1", ["Say yes to merge."], True)
        relayed.expires = self.conversation.clock() + module.READBACK_SECONDS
        self.conversation.readback = relayed
        line = self.finish("Yes.", STRANGER, seconds=0.5)
        self.assertEqual(line["kind"], "yes")
        self.assertEqual(line["threshold"], 0.35)
        self.assertEqual(line["outcome"], "refused")
        self.assertIs(self.conversation.readback, relayed, "the owner can still say yes")
        self.assertFalse([c for c in self.phone.controls if c["t"] == "confirm"])
        self.finish("Yes.", OWNER, seconds=0.5)
        self.assertTrue([c for c in self.phone.controls if c["t"] == "confirm" and c["nonce"]])

    async def test_type_it_is_checked_and_the_offer_stands_after_a_refusal(self):
        self.conversation.typing_offer = ("Run the tests.", self.conversation.clock() + 30)
        self.assertEqual(self.finish("Type it.", STRANGER, seconds=0.6)["outcome"], "refused")
        self.assertEqual(len(self.session.orders), 0)
        self.finish("Type it.", OWNER, seconds=0.6)
        self.assertEqual(self.session.orders[-1]["type"], "Run the tests.")

    async def test_go_while_listening_is_a_prompt_so_it_is_checked(self):
        line = self.finish("Go.", STRANGER, seconds=0.4)
        self.assertEqual((line["kind"], line["outcome"]), ("resume", "refused"))
        self.assertEqual(len(self.session.orders), 0)

    async def test_the_words_that_only_stop_or_decline_are_not_checked(self):
        for text, what in (("Cancel.", "cancel"), ("No.", "no"), ("Pause.", "pause")):
            with self.subTest(text=text):
                self.make()
                line = self.finish(text, STRANGER, seconds=0.5)
                self.assertEqual(line, {"kind": what, "score": STRANGER, "seconds": 0.5, "threshold": None,
                                        "outcome": "unchecked"})
                self.assertNotIn(VOICE_REFUSED, self.said())
        self.assertTrue(self.conversation.paused)

    async def test_go_while_paused_is_not_checked(self):
        self.conversation.paused = True
        self.assertIsNone(self.finish("Go.", STRANGER, seconds=0.4))
        self.assertFalse(self.conversation.paused)

    async def test_a_switch_is_left_to_the_fingerprint(self):
        self.link.sessions["8"].cwd, self.link.sessions["8"].login = "/work/docs", "default"
        line = self.finish("Switch to docs.", STRANGER)
        self.assertEqual((line["kind"], line["outcome"]), ("switch", "unchecked"))
        self.assertTrue([c for c in self.phone.controls if c["t"] == "confirm" and c.get("switch")])

    async def test_switch_to_a_name_no_session_has_is_a_prompt_and_is_checked(self):
        self.link.sessions["8"].cwd, self.link.sessions["8"].login = "/work/docs", "default"
        line = self.finish("Switch to the main branch.", STRANGER)
        self.assertEqual((line["kind"], line["outcome"]), ("prompt", "refused"))
        self.assertEqual(len(self.session.orders), 0)

    async def test_a_refused_message_leaves_the_owners_level_and_a_waiting_confirmation(self):
        self.conversation.owner_level = -30.0
        self.conversation.detector.finished_level = -10.0
        self.conversation.confirming = ("nonce", object(), self.conversation.clock() + 60)
        self.finish("Do it now.", STRANGER)
        self.assertEqual(self.conversation.owner_level, -30.0)
        self.assertIsNotNone(self.conversation.confirming)

    async def test_no_lockout_after_many_refusals(self):
        for _ in range(10):
            self.finish("Push it.", STRANGER)
        self.finish("Push it.", OWNER)
        self.assertEqual(self.session.orders[-1]["submit"], "Push it.")

    async def test_a_failed_score_counts_as_a_fail(self):
        line = self.finish("Push it.", None)
        self.assertEqual((line["score"], line["outcome"]), (None, "refused"))

    # --- thresholds -----------------------------------------------------------------

    async def test_the_threshold_rises_with_the_length_of_speech(self):
        for seconds, threshold in ((0.3, 0.35), (1.49, 0.35), (1.5, 0.42), (2.99, 0.42), (3.0, 0.50), (60, 0.50)):
            with self.subTest(seconds=seconds):
                self.assertEqual(speaker_threshold(seconds), threshold)

    async def test_a_score_at_the_threshold_passes_and_just_under_it_is_refused(self):
        for seconds, threshold in ((1.0, 0.35), (2.0, 0.42), (5.0, 0.50)):
            with self.subTest(seconds=seconds):
                self.make()
                self.assertEqual(self.finish("Push it.", threshold, seconds)["outcome"], "pass")
                self.assertEqual(self.finish("Push it.", threshold - 0.001, seconds)["outcome"], "refused")

    async def test_thresholds_can_be_set(self):
        self.conversation.speaker_thresholds = (0.2, 0.3, 0.4)
        line = self.finish("Push it.", 0.45, seconds=10)
        self.assertEqual((line["threshold"], line["outcome"]), (0.4, "pass"))

    # --- shadow mode, and no voiceprint ---------------------------------------------

    async def test_shadow_mode_logs_a_refusal_and_lets_the_message_through(self):
        self.make(enforce=False)
        line = self.finish("Push the release branch.", STRANGER)
        self.assertEqual(line["outcome"], "would-refuse")
        self.assertEqual(self.session.orders[-1]["submit"], "Push the release branch.")
        self.assertEqual(self.tones(), [])
        self.assertNotIn(VOICE_REFUSED, self.said())

    async def test_without_a_voiceprint_nothing_is_checked_or_logged(self):
        self.make(speaker=False)
        self.assertIsNone(self.conversation.detector.score_voice)
        self.assertIsNone(self.finish("Push it.", None))
        self.assertEqual(self.session.orders[-1]["submit"], "Push it.")

    # --- through the turn detector ----------------------------------------------------

    async def test_the_score_is_worked_out_beside_the_words_and_reaches_the_decision(self):
        self.speaker.next_score = STRANGER
        for packet in [QUIET] * 40 + [VOICE] * 40 + [QUIET] * 90:
            self.conversation.mic_packet(packet)
            await asyncio.sleep(0)
        await asyncio.sleep(0.2)
        for packet in [QUIET] * 10:
            self.conversation.mic_packet(packet)
            await asyncio.sleep(0)
        self.assertEqual(len(self.speaker.scored), 1)
        self.assertEqual(len(self.session.orders), 0)
        self.assertIn(VOICE_REFUSED, self.said())


class VoiceprintTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.folder.name, "state")
        self.models = os.path.join(self.folder.name, "models")
        os.makedirs(self.models)
        with open(os.path.join(self.models, SPEAKER_MODEL), "wb") as f:
            f.write(b"a model")

    def tearDown(self):
        self.folder.cleanup()

    def enroll(self, takes, embed):
        """Enrolls with a recorder that hands out takes in order, and returns what the owner was told."""
        told = []
        takes = iter(takes)
        checker = mock.Mock(embed=embed)
        voiceprint.enroll(self.state, self.models, lambda number, how, text: next(takes), told.append, checker)
        return told

    def test_six_parts_average_to_a_voiceprint_saved_privately_with_the_models_sha256(self):
        speech = np.full(3 * RATE, 3000, dtype=np.int16)
        told = self.enroll([speech] * 6, lambda samples: np.array([1.0, 0.0]))
        self.assertEqual(os.stat(voiceprint.path(self.state)).st_mode & 0o777, 0o600)
        with open(voiceprint.path(self.state)) as f:
            saved = json.load(f)
        self.assertEqual(saved["model_sha256"], voiceprint.model_sha256(self.models))
        self.assertEqual(saved["voiceprint"], [1.0, 0.0])
        np.testing.assert_allclose(voiceprint.load(self.state, self.models), [1.0, 0.0])
        self.assertTrue([line for line in told if "only in memory" in line])

    def test_a_part_unlike_the_rest_is_read_again(self):
        owner, other = np.full(3 * RATE, 3000, dtype=np.int16), np.full(3 * RATE, 3001, dtype=np.int16)
        vectors = {3000: np.array([1.0, 0.0]), 3001: np.array([0.0, 1.0])}
        takes = [owner, owner, other, owner, owner, owner, owner]
        told = self.enroll(takes, lambda samples: vectors[int(samples[0])])
        self.assertTrue([line for line in told if line.startswith("Part 3 doesn't sound like the others")])
        np.testing.assert_allclose(voiceprint.load(self.state, self.models), [1.0, 0.0])

    def test_a_silent_or_short_part_is_read_again(self):
        speech = np.full(3 * RATE, 3000, dtype=np.int16)
        takes = [np.zeros(3 * RATE, dtype=np.int16), speech[:RATE], speech] + [speech] * 5
        told = self.enroll(takes, lambda samples: np.array([1.0, 0.0]))
        self.assertTrue([line for line in told if "heard nothing" in line])
        self.assertTrue([line for line in told if "too short" in line])

    def test_each_part_must_agree_with_the_average(self):
        _, agreement = voiceprint.combine([np.array([1.0, 0.0])] * 5 + [np.array([0.0, 1.0])])
        self.assertGreaterEqual(min(agreement[:5]), voiceprint.MIN_AGREEMENT)
        self.assertLess(agreement[5], voiceprint.MIN_AGREEMENT)

    def test_no_voiceprint_means_none(self):
        self.assertIsNone(voiceprint.load(self.state, self.models))

    def test_a_voiceprint_from_another_model_asks_to_enroll_again(self):
        os.makedirs(self.state)
        voiceprint.save(self.state, np.array([1.0, 0.0]), "0" * 64)
        with self.assertRaisesRegex(voiceprint.VoiceprintError, "another speaker model"):
            voiceprint.load(self.state, self.models)

    def test_an_unreadable_voiceprint_is_an_error_not_a_silent_off(self):
        os.makedirs(self.state)
        with open(voiceprint.path(self.state), "w") as f:
            f.write("{}")
        with self.assertRaisesRegex(voiceprint.VoiceprintError, "Can't read the voiceprint"):
            voiceprint.load(self.state, self.models)

    def test_remove_deletes_it_and_revoke_leaves_it(self):
        os.makedirs(self.state)
        voiceprint.save(self.state, np.array([1.0, 0.0]), voiceprint.model_sha256(self.models))
        with mock.patch("builtins.print"):
            cli.revoke(self.state)
        self.assertTrue(os.path.exists(voiceprint.path(self.state)))
        with mock.patch("builtins.print"):
            self.assertEqual(cli.main(["enroll", "--remove", "--state", self.state, "--models", self.models]), 0)
        self.assertFalse(os.path.exists(voiceprint.path(self.state)))

    def test_the_recorder_keeps_all_it_hears_from_an_input_that_holds_one_buffer(self):
        """AVFoundation keeps only the latest buffer, so the recorder must read without pausing."""
        period, size = 0.002, 32  # a 2 ms buffer of 16 kHz samples, as a Mac input hands them out

        class Frame:
            def to_ndarray(self):
                return np.full((1, size), 1000, dtype=np.int16)

        class Input:
            streams = mock.Mock(audio=[object()])

            def __init__(self):
                self.opened, self.delivered = time.monotonic(), 0

            def demux(self, stream):
                latest = int((time.monotonic() - self.opened) / period)
                if latest <= self.delivered:
                    raise BlockingIOError
                self.delivered = latest  # any buffer before the latest was overwritten
                return iter([mock.Mock(decode=lambda: [Frame()])])

            def close(self):
                pass

        resampler = mock.Mock(resample=lambda frame: [frame])
        stop, started, kept = threading.Event(), threading.Event(), {}
        with mock.patch.object(voiceprint.av, "open", lambda *a, **k: Input()), \
                mock.patch.object(voiceprint.av, "AudioResampler", lambda **k: resampler):
            worker = threading.Thread(target=lambda: kept.update(samples=voiceprint.record_until("0", stop, started)))
            worker.start()
            started.wait()
            time.sleep(1.0)
            stop.set()
            worker.join()
        self.assertGreater(len(kept["samples"]) / RATE, 0.8)

    def test_thresholds_from_the_command_line(self):
        self.assertEqual(cli._thresholds("0.3,0.4,0.5"), (0.3, 0.4, 0.5))
        for bad in ("0.3,0.4", "a,b,c", "0.3,0.4,1.5"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                cli._thresholds(bad)


@unittest.skipUnless(MODELS, "needs WALKIE_TALKIE_MODELS")
class RealSpeakerModelTest(unittest.TestCase):
    """Kokoro's voices are different speakers, so CAM++ should tell them apart."""

    @classmethod
    def setUpClass(cls):
        from walkie_gateway.speech import VOICE_RATE, Speech
        speech = Speech(MODELS, threads=2)

        def say(text, voice):
            samples = speech.synthesize(text, voice)
            at = np.linspace(0, len(samples) - 1, int(len(samples) * RATE / VOICE_RATE))
            return np.interp(at, np.arange(len(samples)), samples).astype(np.int16)
        cls.say = staticmethod(say)
        cls.check = SpeakerCheck(MODELS)

    def test_the_enrolled_voice_passes_and_another_does_not(self):
        parts = [self.check.embed(self.say(text, "bm_george")) for _, text in voiceprint.PASSAGE]
        self.check.voiceprint, agreement = voiceprint.combine(parts)
        self.assertGreaterEqual(min(agreement), voiceprint.MIN_AGREEMENT)
        line = "Tell me which tests failed on the last run, and why."
        owner = self.check.score(self.say(line, "bm_george"))
        others = [self.check.score(self.say(line, voice)) for voice in ("af_bella", "am_adam", "bf_emma")]
        self.assertGreaterEqual(owner, speaker_threshold(3.0))
        self.assertLess(max(others), speaker_threshold(3.0))
