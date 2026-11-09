"""A reply's echo and a nearby voice through the real models, in real time. No audio device is used.

Stubbed transcripts can't catch these: the real transcriber returns one or two words of an echo, and it
transcribes whatever's in the pre-roll. Runs only when WALKIE_TALKIE_MODELS names the models.
"""
import asyncio
import collections
import os
import time
import unittest

import av
import numpy as np
from walkie_gateway.conversation import Conversation
from walkie_gateway.modlink import Session
from walkie_gateway.speech import (
    VOICE_RATE,
    OpusDecoder,
    OpusEncoder,
    Speech,
    VoiceDetector,
)
from walkie_gateway.turns import RATE, WINDOW, TurnDetector, loudness, speaking_level

MODELS = os.environ.get("WALKIE_TALKIE_MODELS")
FRAME = 320
BUFFER_FRAMES = 5   # what the phone can still flush: 100 ms
TAIL = 0.3          # what it can't: the reply keeps reaching the mic for this long
REPLY = ("The login page now keeps people signed in after a restart, because the session token moved into the "
         "database. The first thing to know is that the report covers three separate quarters of work. After that "
         "it explains how the numbers were gathered from every team in the company.")
OWNER = "Could you check whether the tests pass on the main branch please?"
OVER = "Wait, stop, use the staging database instead."
PASSER_BY = "Excuse me, do you know where the train station is?"
READ_BACK = "I'll merge the release branch. Confirm?"
# The worst case for the pause rule: a reply that says the very words, some of them alone.
WORDY_REPLY = ("Let me pause the build first. Pause. You can go now. Go. Press play to hear it. Play. "
               "Then we start again from the top. Start. I will pause here, and then go on, and start the tests.")


def level_of(samples):
    levels = [loudness(samples[i:i + WINDOW]) for i in range(0, len(samples) - WINDOW, WINDOW)]
    loudest = max(levels)
    return speaking_level([level for level in levels if level > loudest - 30])


def noise(count, dbfs):
    return np.random.default_rng(1).normal(size=count) * 32768 * 10 ** (dbfs / 20)


def resampled(spoken):
    spoken = spoken.astype(np.float64)
    return np.interp(np.arange(0, len(spoken), VOICE_RATE / RATE), np.arange(len(spoken)), spoken)


def opus_encoder():
    encoder = av.CodecContext.create("libopus", "w")
    encoder.rate, encoder.layout, encoder.format, encoder.bit_rate = RATE, "mono", "s16", 24000
    encoder.options = {"application": "voip", "frame_duration": "20"}
    encoder.open()
    return encoder


class Stub:
    """The phone and the mod link, for a test that drives the microphone itself."""

    connected = True

    def __init__(self):
        self.controls = []
        self.sessions = {"7": Session("7")}

    def send_control(self, message):
        self.controls.append(message)

    def forget_speech(self):
        pass

    def send_speech(self, gen, packet):
        pass

    def prune(self):
        return False


class Rig:
    """The phone: it plays the reply through a delay into the mic, and sends what the mic hears as Opus."""

    connected = True
    approved = True  # a fingerprint started this run

    def __init__(self, speech, leak_db, delay, trip_on_first_sound=False):
        self.trip_on_first_sound = trip_on_first_sound
        self.line = collections.deque([np.zeros(FRAME)] * round(delay / 0.02))
        self.queue = collections.deque()
        self.pcm = np.zeros(0)
        self.decoder = OpusDecoder()
        self.cut_gen = 0
        self.held = False
        self.mic = []
        self.prompts = []
        self.aborts = []
        self.confirms = []
        self.finished = False
        self.live = np.zeros(0)
        self.resumes = 0
        spoken = speech.synthesize(REPLY[:120], "af_bella").astype(np.float64)
        heard = np.interp(np.arange(0, len(spoken), VOICE_RATE / RATE), np.arange(len(spoken)), spoken)
        self.leak_gain = 10 ** ((leak_db - level_of(heard)) / 20)
        self.encoder = opus_encoder()
        self.session = Session("7")
        self.sessions = {"7": self.session}
        self.conversation = Conversation(speech, VoiceDetector(MODELS), OpusDecoder, OpusEncoder())
        self.conversation.phone, self.conversation.modlink = self, self
        self.conversation.start()
        self.conversation._set_chosen("7")

    def prune(self):
        return False

    def forget_speech(self):
        pass

    def signed(self, message, signature):
        return True

    def send_control(self, message):
        if message["t"] == "cut" and message["gen"] > self.cut_gen:
            self.cut_gen = message["gen"]
            self.queue.clear()
            self.held = False
            self.flush()
        elif message["t"] == "resume":
            self.held = False
            self.resumes += 1
        elif message["t"] == "confirm" and message["nonce"]:  # the owner's fingerprint in the app
            asyncio.get_running_loop().call_soon(self.conversation.phone_message,
                                                 {"t": "confirmed", "nonce": message["nonce"], "sig": "good"})

    def send_speech(self, gen, packet):
        if gen < self.cut_gen:
            return
        self.pcm = np.concatenate((self.pcm, self.decoder.decode(packet).astype(np.float64)))
        while len(self.pcm) >= FRAME:
            self.queue.append(self.pcm[:FRAME])
            self.pcm = self.pcm[FRAME:]

    def flush(self):
        for i in range(max(0, len(self.line) - BUFFER_FRAMES), len(self.line)):
            self.line[i] = np.zeros(FRAME)

    def hold(self):
        self.held = True
        self.flush()
        self.conversation.phone_message({"t": "hold", "id": 1})

    def reply(self):
        self.conversation.mod_event(self.session, {"kind": "turn-start", "turnId": "t"})
        self.conversation.mod_event(self.session, {"kind": "text", "turnId": "t", "text": REPLY})

    def speak(self, samples):
        self.live = np.concatenate((self.live, samples))

    def answer_orders(self):
        while self.session.orders:
            order = self.session.orders.popleft()
            if "abort" in order:
                self.aborts.append(order)
            elif "submit" in order:
                self.prompts.append(order["submit"])
            elif "confirm" in order:
                self.confirms.append(order["confirm"])
            self.conversation.mod_event(self.session, {"kind": "order", "id": order["id"], "ok": True})

    async def run(self, seconds, timeline=()):
        frames = int(seconds / 0.02)
        background = noise(frames * FRAME + FRAME, -60)
        spoken = np.zeros(frames * FRAME + FRAME)
        for at, samples in self.mic:
            start = int(at * RATE)
            end = min(len(spoken), start + len(samples))
            spoken[start:end] += samples[:end - start]
        todo = sorted(timeline, key=lambda item: item[0])
        started = time.monotonic()
        for i in range(frames):
            if self.finished:
                break  # a script has seen all it needs
            while todo and todo[0][0] <= i * 0.02:
                todo.pop(0)[1]()
            self.line.append(self.queue.popleft() if self.queue and not self.held else np.zeros(FRAME))
            out = self.line.popleft()
            if self.trip_on_first_sound and np.abs(out).max() > 500:
                self.trip_on_first_sound = False
                self.hold()  # what the phone's gate does in the first reply, before it has learned the leak
            mixed = out * self.leak_gain + background[i * FRAME:(i + 1) * FRAME] + spoken[i * FRAME:(i + 1) * FRAME]
            live, self.live = self.live[:FRAME], self.live[FRAME:]
            mixed[:len(live)] += live
            piece = av.AudioFrame.from_ndarray(np.clip(mixed, -32768, 32767).astype(np.int16).reshape(1, -1),
                                               format="s16", layout="mono")
            piece.rate, piece.pts = RATE, i * FRAME
            for packet in self.encoder.encode(piece):
                self.conversation.mic_packet(bytes(packet))
            self.answer_orders()
            if self.confirms:
                break  # nothing later can change a yes that's been filed
            await asyncio.sleep(max(started + (i + 1) * 0.02 - time.monotonic(), 0))
        self.finished = True
        self.conversation.stop()


@unittest.skipUnless(MODELS, "needs WALKIE_TALKIE_MODELS")
class TalkOverRealTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.speech = Speech(MODELS)

    def voice(self, text, dbfs, name="am_adam"):
        spoken = self.speech.synthesize(text, name).astype(np.float64)
        heard = np.interp(np.arange(0, len(spoken), VOICE_RATE / RATE), np.arange(len(spoken)), spoken)
        return heard * 10 ** ((dbfs - level_of(heard)) / 20)

    def play(self, seconds, leak_db, delay, spoken=(), holds=(), reply_at=None, trip_on_first_sound=False):
        async def scenario():
            rig = Rig(self.speech, leak_db, delay, trip_on_first_sound)
            rig.mic.extend(spoken)
            timeline = [(at, rig.hold) for at in holds]
            if reply_at is None:
                rig.reply()
            else:
                timeline.append((reply_at, rig.reply))
            await rig.run(seconds, timeline)
            return rig
        return asyncio.run(scenario())

    def read_back(self, seconds, leak_db, delay, script=None):
        # Times count from the events themselves: synthesis runs late when the machine is busy.
        async def scenario():
            rig = Rig(self.speech, leak_db, delay)
            rig.session.front_desk = True
            rig.conversation.mod_event(rig.session, {"kind": "relay", "note": "note-1", "text": READ_BACK})
            await asyncio.gather(rig.run(seconds), script(rig) if script else asyncio.sleep(0))
            return rig
        return asyncio.run(scenario())

    @staticmethod
    async def until(rig, happened):
        while not happened() and not rig.finished:
            await asyncio.sleep(0.02)

    def test_a_yes_after_a_read_back_counts_at_loud_leak(self):
        # What the harness measured: the read-back's echo marked speech as lost past its end, so every yes was refused.
        yes = self.voice("Yes.", -31, "af_bella")  # the transcriber hears am_adam's as "Yeah"

        async def script(rig):
            await self.until(rig, lambda: rig.conversation.readback)
            await asyncio.sleep(2.5)
            rig.speak(yes)
        for leak_db, delay in ((-45, 0.5), (-40, 0.5)):
            with self.subTest(leak_db=leak_db, delay=delay):
                rig = self.read_back(30, leak_db, delay, script)
                self.assertEqual(len(rig.confirms), 1)
                self.assertEqual(rig.prompts, [])

    def test_a_read_backs_echo_approves_nothing(self):
        for leak_db, delay in ((-45, 0.5), (-40, 0.5)):
            with self.subTest(leak_db=leak_db, delay=delay):
                rig = self.read_back(10, leak_db, delay)
                self.assertEqual(rig.confirms, [])
                self.assertEqual(rig.prompts, [])

    def test_a_reply_tripping_the_phone_with_nobody_talking_stops_nothing(self):
        # What the independent review measured: with a slow output, the tail of a reply stopped its own
        # turn and reached the session as "The log".
        for delay in (0.4, 0.5):
            with self.subTest(delay=delay):
                rig = self.play(9, leak_db=-41, delay=delay, trip_on_first_sound=True)
                self.assertEqual(rig.prompts, [])
                self.assertEqual(rig.aborts, [])
                self.assertEqual(rig.resumes, 1)

    def test_a_passer_by_quieter_than_the_owner_stops_nothing(self):
        spoken = [(1.0, self.voice(OWNER, -31)), (13.0, self.voice(PASSER_BY, -41, "bm_lewis"))]  # 10 dB quieter
        rig = self.play(22, leak_db=-60, delay=0.3, spoken=spoken, holds=[13.1], reply_at=8.5)
        self.assertEqual(len(rig.prompts), 1, rig.prompts)
        self.assertEqual(rig.aborts, [])

    def test_the_owner_over_a_reply_stops_it(self):
        spoken = [(1.0, self.voice(OWNER, -31)), (13.0, self.voice(OVER, -31))]
        rig = self.play(22, leak_db=-38, delay=0.3, spoken=spoken, holds=[13.1], reply_at=8.5)
        self.assertEqual(len(rig.prompts), 2, rig.prompts)
        self.assertEqual(len(rig.aborts), 1)
        self.assertTrue(rig.prompts[1].lower().startswith("wait"), rig.prompts[1])

    def test_no_reply_word_joins_what_the_owner_says_over_it(self):
        # What the independent review measured: "The law Wait, stop, use the staging database instead."
        # The pre-roll of a message under a hold is the reply's own sound, and the transcriber reads it.
        spoken = self.speech.synthesize(REPLY, "af_bella").astype(np.float64)
        heard = np.interp(np.arange(0, len(spoken), VOICE_RATE / RATE), np.arange(len(spoken)), spoken)
        leak = heard * 10 ** ((-38 - level_of(heard)) / 20)
        owner = self.voice(OVER, -31)
        for cut in range(RATE, 11 * RATE, RATE // 3):  # before the fix: "after that." and "Is that the way it's. Wait"
            with self.subTest(onset=round(cut / RATE, 1)):
                text = asyncio.run(self.heard_over(leak, owner, cut))
                self.assertTrue(text.lower().startswith("wait"), text)

    async def heard_over(self, leak, owner, cut):
        now = [0.0]
        turns = []

        async def transcribe(audio):
            return self.speech.transcribe(audio)

        detector = TurnDetector(VoiceDetector(MODELS), transcribe, lambda text, *_: turns.append(text),
                                clock=lambda: now[0])
        onset = int(cut)
        trip = onset + RATE // 16  # the phone trips about 60 ms into the voice
        heard = np.concatenate((leak[:onset + int(TAIL * RATE)], np.zeros(len(owner) + RATE * 4)))  # the reply's tail
        heard[onset:onset + len(owner)] += owner
        stream = np.clip(heard + noise(len(heard), -60), -32768, 32767).astype(np.int16)
        for start in range(0, len(stream) - WINDOW, WINDOW):
            if start <= trip < start + WINDOW:
                detector.allow_start()
            detector.feed(stream[start:start + WINDOW], ignored=start + WINDOW <= trip)
            now[0] = start / RATE
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        return turns[0] if turns else ""

    # --- pausing -------------------------------------------------------------------

    def test_pausing_keeps_a_conversation_out_of_the_session_until_go(self):
        # Each voice waits for the last one's turn and any spoken confirmation to end, because speech that
        # starts under a notice doesn't count. Times count from the events: transcription runs late when the machine is busy.
        voices = [self.voice(OWNER, -31), self.voice("Pause.", -31, "bm_george"),
                  self.voice(PASSER_BY, -31, "bm_lewis"), self.voice("Go.", -31, "bm_george"), self.voice(OVER, -31)]

        async def script(rig):
            turns = []
            on_turn = rig.conversation.detector.on_turn
            rig.conversation.detector.on_turn = lambda text, *rest: (turns.append(text), on_turn(text, *rest))
            await asyncio.sleep(1)
            for count, samples in enumerate(voices, 1):
                rig.speak(samples)
                await self.until(rig, lambda: len(turns) >= count)
                await self.until(rig, lambda: not rig.conversation._reply_pending())
            rig.finished = True

        async def scenario():
            rig = Rig(self.speech, -60, 0.3)
            await asyncio.gather(rig.run(120), script(rig))
            return rig
        rig = asyncio.run(scenario())
        self.assertEqual(len(rig.prompts), 2, rig.prompts)
        self.assertTrue(rig.prompts[0].lower().startswith("could"), rig.prompts)
        self.assertTrue(rig.prompts[1].lower().startswith("wait"), rig.prompts)
        self.assertEqual(rig.aborts, [])
        self.assertFalse(rig.conversation.paused)

    async def heard_under_hold(self, stream, trip, paused=False):
        """What the mic hears, through Opus and the real detector, on a clock that keeps to the audio."""
        now = [100.0]
        stub = Stub()
        conversation = Conversation(self.speech, VoiceDetector(MODELS), OpusDecoder, None, clock=lambda: now[0])
        conversation.phone, conversation.modlink = stub, stub
        conversation.chosen, conversation.owner_level, conversation.paused = "7", -31, paused
        conversation.reply_heard = True
        conversation.played_until = now[0] + len(stream) / RATE + 60
        encoder = opus_encoder()
        held = False
        for start in range(0, len(stream) - FRAME, FRAME):
            if not held and start >= trip:
                held = True
                conversation.phone_message({"t": "hold", "id": 1})
            piece = av.AudioFrame.from_ndarray(stream[start:start + FRAME].reshape(1, -1), format="s16", layout="mono")
            piece.rate, piece.pts = RATE, start
            for packet in encoder.encode(piece):
                conversation.mic_packet(bytes(packet))
            now[0] += FRAME / RATE
            await asyncio.sleep(0)
            while conversation.hold and conversation.hold.checking:
                await asyncio.sleep(0.002)
            if held and conversation.hold is None:
                break
        conversation.stop()
        return conversation, stub

    def over_a_reply(self, reply, word, onset, leak_db, tail=TAIL):
        """The stream under a hold: the reply's leak until the phone trips and its tail runs out, with a word on top."""
        leak = reply * 10 ** ((leak_db - level_of(reply)) / 20)
        trip = onset + RATE // 16
        first = max(0, onset - 4 * RATE)
        heard = np.zeros(trip - first + int(tail * RATE) + len(word) + 4 * RATE)
        heard[:trip - first + int(tail * RATE)] = leak[first:trip + int(tail * RATE)]
        heard[onset - first:onset - first + len(word)] += word
        stream = np.clip(heard + noise(len(heard), -60), -32768, 32767).astype(np.int16)
        return stream, trip - first

    def heard_reply(self, text):
        return resampled(self.speech.synthesize(text, "af_bella"))

    def test_a_lone_pause_over_a_reply_pauses_or_misses_and_stops_nothing_either_way(self):
        # The transcriber misses a word this short at many moments, and which ones shifts with its threads, so
        # no single moment can be promised. This voice is one it reads well, so it catches some of these.
        reply = self.heard_reply(REPLY)
        word = self.voice("Pause.", -31, "bm_george")
        caught = 0
        for onset in (1, 3, 5, 7, 9):
            with self.subTest(onset=onset):
                stream, trip = self.over_a_reply(reply, word, onset * RATE, -41)
                conversation, stub = asyncio.run(self.heard_under_hold(stream, trip))
                caught += conversation.paused
                self.assertEqual(len(stub.sessions["7"].orders), 0)
                self.assertEqual([c["leak"] for c in stub.controls if c["t"] == "resume"], [False])
                self.assertEqual([c for c in stub.controls if c["t"] == "cut"], [])
        self.assertGreater(caught, 0)

    def test_a_replys_own_echo_never_changes_whether_it_is_listening(self):
        # A reply full of the very words, tripping the phone everywhere, with leak quieter than the owner.
        reply = self.heard_reply(WORDY_REPLY)
        for paused in (False, True):
            for onset in np.arange(0.5, len(reply) / RATE - 1, 0.5):
                with self.subTest(paused=paused, onset=float(onset)):
                    stream, trip = self.over_a_reply(reply, np.zeros(0), int(onset * RATE), -41)
                    conversation, stub = asyncio.run(self.heard_under_hold(stream, trip, paused))
                    self.assertEqual(conversation.paused, paused)
                    self.assertEqual(len(stub.sessions["7"].orders), 0)
                    self.assertEqual([c for c in stub.controls if c["t"] in ("cut", "leak")], [])


if __name__ == "__main__":
    unittest.main()
