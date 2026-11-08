"""Quiet speech through the real voice detector. Runs only when WALKIE_TALKIE_MODELS names the models."""
import asyncio
import os
import unittest

import av
import numpy as np
from walkie_gateway.speech import VOICE_RATE, OpusDecoder, Speech, VoiceDetector
from walkie_gateway.turns import RATE, TurnDetector

MODELS = os.environ.get("WALKIE_TALKIE_MODELS")
SENTENCE = "Could you check whether the tests pass on the main branch before we merge anything?"


def at_level(samples, dbfs):
    return samples * 10 ** (dbfs / 20) * 32768 / np.sqrt(np.mean(samples[np.abs(samples) > 0.05 * np.abs(samples).max()] ** 2))


def noise(count, dbfs):
    rng = np.random.default_rng(1)
    rumble = np.convolve(rng.normal(size=count), np.ones(160) / np.sqrt(160), "same")
    mixed = np.sqrt(1 / 3) * rng.normal(size=count) + np.sqrt(2 / 3) * rumble
    return mixed * 10 ** (dbfs / 20) * 32768 / np.sqrt(np.mean(mixed ** 2))


def through_opus(pcm):
    encoder = av.CodecContext.create("libopus", "w")
    encoder.rate, encoder.layout, encoder.format, encoder.bit_rate = RATE, "mono", "s16", 24000
    encoder.options = {"application": "voip", "frame_duration": "20"}
    encoder.open()
    decoder = OpusDecoder()
    pts = 0
    for start in range(0, len(pcm) - 319, 320):
        frame = av.AudioFrame.from_ndarray(pcm[start:start + 320].reshape(1, -1), format="s16", layout="mono")
        frame.rate, frame.pts = RATE, pts
        pts += 320
        for packet in encoder.encode(frame):
            yield decoder.decode(bytes(packet))


@unittest.skipUnless(MODELS, "needs WALKIE_TALKIE_MODELS")
class CollarLevelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.speech = Speech(MODELS)
        voice = cls.speech.synthesize(SENTENCE).astype(np.float64)
        cls.clip = np.interp(np.arange(0, len(voice), VOICE_RATE / RATE), np.arange(len(voice)), voice)

    async def turn_from(self, level, floor):
        return await self.turns_from([level], floor)

    async def turns_from(self, levels, floor):
        """One unbroken stream: each level is the sentence again, with quiet between."""
        pieces = [np.zeros(RATE * 3)]
        for level in levels:
            pieces += [at_level(self.clip, level), np.zeros(RATE * 5)]
        clean = np.concatenate(pieces)
        mixed = np.clip(clean + noise(len(clean), floor), -32768, 32767).astype(np.int16)
        now = [0.0]
        turns = []

        async def transcribe(audio):
            return self.speech.transcribe(audio)

        detector = TurnDetector(VoiceDetector(MODELS), transcribe,
                                lambda text, *_: turns.append(text), clock=lambda: now[0])
        played = 0
        for samples in through_opus(mixed):
            detector.feed(samples)
            played += len(samples)
            now[0] = played / RATE
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        return turns

    def test_speech_at_collar_levels_is_one_whole_turn(self):
        for level, floor in ((-26, -60), (-32, -50), (-38, -50), (-38, -60), (-44, -60)):
            with self.subTest(level=level, floor=floor):
                turns = asyncio.run(self.turn_from(level, floor))
                self.assertEqual(len(turns), 1)
                self.assertTrue(turns[0].lower().startswith("could you"), turns[0])

    def test_speaking_close_to_the_mic_does_not_deafen_it_for_a_voice_at_the_collar(self):
        # The owner's second symptom. With Silero's state kept, the quiet sentences scored 0% voiced.
        for floor in (-50, -60):
            with self.subTest(floor=floor):
                turns = asyncio.run(self.turns_from([-20, -38, -38, -38], floor))
                self.assertEqual(len(turns), 4, turns)
                for text in turns:
                    self.assertTrue(text.lower().startswith("could you"), text)

    def test_a_voice_lost_in_the_background_is_not_heard(self):
        self.assertEqual(asyncio.run(self.turn_from(-62, -60)), [])


if __name__ == "__main__":
    unittest.main()
