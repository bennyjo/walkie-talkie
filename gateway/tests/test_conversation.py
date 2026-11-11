"""The conversation's rules, with stand-ins for the speech models, the phone, and the mod."""
import asyncio
import json
import logging
import os
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np
from walkie_gateway import conversation as module
from walkie_gateway import turns
from walkie_gateway.conversation import (
    CANCELLED,
    DESK_CANCELLED,
    DIALOG_OPEN,
    NO_SESSION,
    NOT_FILED,
    NOT_SENT,
    NOTHING_TO_CONFIRM,
    NOTHING_TO_TYPE,
    PERMISSION,
    REPLY_NOT_SPOKEN,
    SESSION_GONE,
    STOPPED_AT_KEYBOARD,
    YES_NOT_SENT,
    YES_SENT,
    Conversation,
)
from walkie_gateway.modlink import Session
from walkie_gateway.speech import DEFAULT_VOICE, VOICES
from walkie_gateway.turns import SECONDS, WINDOW

SPOKEN_SECONDS = 0.1
VOICE = np.array(([3000] * 8 + [-3000] * 8) * (WINDOW // 16), dtype=np.int16).tobytes()
QUIET = np.zeros(WINDOW, dtype=np.int16).tobytes()


class FakeSpeech:
    def transcribe(self, audio):
        return "what you said"

    unspeakable = ()

    def __init__(self):
        self.voices = []

    def synthesize(self, text, voice):
        self.voices.append(voice)
        if text in self.unspeakable:
            raise RuntimeError("no voice for that")
        return text  # the fake encoder turns this into frames


class FakeEncoder:
    def encode(self, text):
        return [("%s:%d" % (text, i)).encode() for i in range(int(SPOKEN_SECONDS / module.FRAME_SECONDS))]


class FakeDecoder:
    def decode(self, packet):
        return np.frombuffer(packet, dtype=np.int16)


class FakePhone:
    connected = True

    def __init__(self):
        self.controls = []
        self.speech = []
        self.run_id = "run-1"
        self.approved_run = "run-1"  # a fingerprint started this run
        self.revoked = False

    @property
    def approved(self):
        return self.approved_run is not None and self.approved_run == self.run_id

    def end_run(self):
        self.approved_run = None

    def send_ended(self, run, why):
        if run == self.run_id and not self.approved:
            self.send_control({"t": "ended", "why": why})

    def revoke(self):
        self.revoked = True

    def signed(self, message, signature):
        self.last_signed = message
        return signature == "good"

    def send_control(self, message):
        self.controls.append(message)

    def send_speech(self, gen, packet):
        self.speech.append((gen, packet.decode()))

    def forget_speech(self):
        self.controls.append({"t": "forgot"})

    def said(self):
        return list(dict.fromkeys(packet.rsplit(":", 1)[0] for _, packet in self.speech))


class FakeLink:
    def __init__(self):
        self.sessions = {"7": Session("7"), "8": Session("8")}
        for instance, session in self.sessions.items():
            session.nonce = "nonce-" + instance

    def prune(self):
        return False


class ConversationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conversation = Conversation(FakeSpeech(), lambda w: bool(w.any()), FakeDecoder, FakeEncoder())
        self.phone = FakePhone()
        self.link = FakeLink()
        self.conversation.phone = self.phone
        self.conversation.modlink = self.link
        self.conversation.start()
        self.conversation.owner_level = -100.0  # the owner has spoken, and any test voice is as loud
        self.session = self.link.sessions["7"]

    async def asyncTearDown(self):
        self.conversation.stop()
        await asyncio.sleep(0)

    def choose(self, instance="7"):
        """What a fingerprint's start does to the routing, without its notice and the mod's grant."""
        self.conversation._set_chosen(instance)

    def confirm_in_app(self, sig="good"):
        """The app's fingerprint over the read-back, if a spoken yes asked for one."""
        asked = [c for c in self.phone.controls if c["t"] == "confirm"]
        if asked and asked[-1]["nonce"]:
            self.conversation.phone_message({"t": "confirmed", "nonce": asked[-1]["nonce"], "sig": sig})

    def yes(self):
        self.finish_turn("Yes.")
        self.confirm_in_app()

    def order(self):
        return self.session.orders.popleft()

    def order_for(self, what):
        return next(order for order in reversed(self.session.orders) if what in order)

    async def settle(self, seconds=0.3):
        await asyncio.sleep(seconds)

    async def by_then(self, done, seconds=5):
        """Waits until done() holds, or the deadline passes: speech is paced in real time, so a busy machine is slower."""
        deadline = asyncio.get_running_loop().time() + seconds
        while not done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

    async def past_the_gap(self):
        """A notice and a reply keep the output delay between them, so one cut can't take the other."""
        await self.settle(module.OUTPUT_DELAY_SECONDS + 0.4)

    def tones(self):
        return [c["kind"] for c in self.phone.controls if c["t"] == "tone"]

    def event(self, **fields):
        self.conversation.mod_event(self.session, fields)

    def finish_turn(self, text):
        self.conversation.detector.finished_marks = {"speech_detected": self.conversation.clock()}
        self.conversation._turn_finished(text, self.conversation.clock())

    # --- the front desk's read-back and yes ----------------------------------------

    def relay(self, note, text):
        self.event(kind="relay", note=note, text=text)

    async def play_readback(self, text="Say yes to merge the release branch."):
        self.session.front_desk = True
        self.choose()
        self.relay("note-1", text)
        await self.past_the_gap()
        self.assertTrue(self.conversation.readback, "the reply should have played through")

    # --- what the independent review reproduced -----------------------------------

    async def test_a_cancel_at_a_desk_never_says_a_filed_note_was_cancelled(self):
        self.session.front_desk = True
        self.choose()
        self.finish_turn("Post the launch update.")
        self.event(kind="order", id=self.order()["id"], ok=True)
        self.finish_turn("cancel")
        await self.settle()
        self.assertIn(DESK_CANCELLED, self.phone.said())
        self.assertNotIn(CANCELLED, self.phone.said())
        self.assertIn("already filed still stands", DESK_CANCELLED)

    async def test_a_cancel_in_a_normal_session_still_says_cancelled(self):
        self.choose()
        self.finish_turn("cancel")
        await self.settle()
        self.assertEqual(self.phone.said(), [CANCELLED])

    async def test_a_failed_hand_off_never_claims_nothing_was_saved(self):
        # A command can save the note and then fail or hang, so its failure says nothing about the note.
        await self.play_readback()
        self.yes()
        self.event(kind="order", id=self.order()["id"], error="the hand-off command failed")
        self.event(kind="not-filed")
        await self.past_the_gap()
        for false_claim in ("nothing will happen", "isn't saved", "Say it again"):
            self.assertFalse([said for said in self.phone.said() if false_claim in said], false_claim)
        self.assertIn(YES_NOT_SENT, self.phone.said())
        self.assertIn(NOT_FILED, self.phone.said())
        self.assertIn("can't tell", YES_NOT_SENT)
        self.assertIn("can't tell", NOT_FILED)

    async def test_a_prompt_with_a_reply_held_after_a_tap_is_marked_unheard(self):
        await self.play_readback("Deploy finished on staging.")
        self.conversation.phone_message({"t": "tap", "gen": 0})
        self.order()  # the tap's own abort
        self.relay("note-2", "Should I delete the old release branches?")
        await self.settle()
        self.finish_turn("Good, do that.")
        order = self.order()
        self.assertEqual(order["submit"], "Good, do that.")
        self.assertTrue(order.get("unheard"))

    async def test_a_prompt_with_nothing_held_is_not_marked_unheard(self):
        await self.play_readback("Deploy finished on staging.")
        await self.settle()  # past the reply's tail
        self.finish_turn("Good, do that.")
        self.assertNotIn("unheard", self.order())

    async def test_a_reply_with_anything_left_out_cannot_take_a_yes(self):
        self.session.front_desk = True
        self.choose()
        for text in ("Delete these: ```rm -rf prod``` Say yes to confirm.",
                     "Delete /var/data/prod and ~/backup. Say yes to confirm.",
                     "Delete `prod`. Say yes to confirm."):
            with self.subTest(text=text):
                self.relay("note-1", text)
                await self.settle(0.5)
                self.assertIsNone(self.conversation.readback)
                self.yes()
                await self.settle()
                self.assertEqual(len(self.session.orders), 0)
                self.assertIn(NOTHING_TO_CONFIRM, self.phone.said())

    async def test_a_question_is_not_a_yes(self):
        await self.play_readback()
        for words in ("Yes?", "Oh, yes?", "Yes -"):
            with self.subTest(words=words):
                self.conversation.readback = None
                self.finish_turn(words)
                self.assertNotIn("confirm", self.order())

    async def test_a_yes_after_speech_that_lost_its_start_still_counts(self):
        await self.play_readback()
        self.conversation.detector.skip_speech()
        self.yes()
        self.assertEqual(self.order()["confirm"]["note"], "note-1")

    async def test_a_no_ends_the_wait_and_sends_nothing(self):
        await self.play_readback()
        self.finish_turn("No.")
        await self.past_the_gap()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said()[-1], module.NOT_CONFIRMED)
        self.yes()
        await self.past_the_gap()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said()[-1], NOTHING_TO_CONFIRM)

    async def test_a_no_while_the_app_waits_for_a_fingerprint_withdraws_it_and_sends_nothing(self):
        await self.play_readback()
        self.finish_turn("Yes.")
        asked = [c for c in self.phone.controls if c["t"] == "confirm"][-1]
        self.finish_turn("No.")
        await self.past_the_gap()
        self.assertEqual([c for c in self.phone.controls if c["t"] == "confirm"][-1], {"t": "confirm", "nonce": None})
        self.conversation.phone_message({"t": "confirmed", "nonce": asked["nonce"], "sig": "good"})
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said()[-1], module.NOT_CONFIRMED)

    async def test_a_no_with_nothing_waiting_is_an_ordinary_prompt(self):
        self.session.front_desk = True
        self.choose()
        self.finish_turn("No.")
        self.assertEqual(self.order()["submit"], "No.")
        self.assertNotIn(module.NOT_CONFIRMED, self.phone.said())

    async def test_a_no_after_the_wait_ran_out_is_an_ordinary_prompt(self):
        await self.play_readback()
        self.conversation.readback.expires = self.conversation.clock() - 1
        self.finish_turn("No.")
        self.assertEqual(self.order()["submit"], "No.")

    async def test_an_unsure_no_is_an_ordinary_prompt_and_ends_the_wait(self):
        await self.play_readback()
        self.finish_turn("No?")
        self.assertEqual(self.order()["submit"], "No?")
        self.assertIsNone(self.conversation.readback)

    async def test_a_yes_wholly_after_the_read_back_still_counts(self):
        await self.play_readback()
        self.yes()
        self.assertIn("confirm", self.order())

    async def test_a_relayed_reply_is_spoken_word_for_word_with_no_turn(self):
        await self.play_readback()
        self.assertEqual(self.phone.said(), ["Say yes to merge the release branch."])
        self.assertEqual(len(self.session.orders), 0)

    async def test_a_yes_after_the_read_back_goes_to_the_mod_with_the_words_heard(self):
        await self.play_readback()
        self.yes()
        order = self.order()
        self.assertEqual(order["confirm"], {"note": "note-1", "spoken": "Say yes to merge the release branch."})
        self.assertEqual(self.tones(), [])
        self.event(kind="order", id=order["id"], ok=True)
        await self.past_the_gap()
        self.assertEqual(self.phone.said()[-1], YES_SENT)

    async def test_a_yes_that_could_not_be_filed_says_so(self):
        await self.play_readback()
        self.yes()
        self.event(kind="order", id=self.order()["id"], error="the hand-off command failed")
        await self.past_the_gap()
        self.assertEqual(self.phone.said()[-1], YES_NOT_SENT)

    async def test_a_second_yes_answers_nothing(self):
        await self.play_readback()
        self.yes()
        self.order()
        self.yes()
        await self.past_the_gap()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said()[-1], NOTHING_TO_CONFIRM)

    async def test_a_yes_with_nothing_to_answer_sends_nothing(self):
        self.session.front_desk = True
        self.choose()
        self.yes()
        await self.settle()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said(), [NOTHING_TO_CONFIRM])

    async def test_a_late_yes_sends_nothing(self):
        await self.play_readback()
        self.conversation.readback.expires = self.conversation.clock() - 1
        self.yes()
        await self.past_the_gap()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said()[-1], NOTHING_TO_CONFIRM)

    async def test_the_wait_for_a_yes_starts_when_the_read_back_has_played(self):
        await self.play_readback()
        left = self.conversation.readback.expires - self.conversation.clock()
        self.assertAlmostEqual(left, module.READBACK_SECONDS, delta=2)

    async def test_a_yes_while_the_read_back_is_still_queued_answers_nothing(self):
        self.session.front_desk = True
        self.choose()
        self.relay("note-1", "Say yes to merge the release branch.")
        self.yes()
        self.assertEqual(len(self.session.orders), 0)

    async def test_a_tap_leaves_nothing_to_say_yes_to(self):
        await self.play_readback()
        self.conversation.phone_message({"t": "tap", "gen": 0})
        self.order()  # the tap's own abort
        self.yes()
        await self.settle()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said()[-1], NOTHING_TO_CONFIRM)

    async def test_a_tap_during_the_read_back_leaves_nothing_to_say_yes_to(self):
        self.session.front_desk = True
        self.choose()
        self.relay("note-1", "Say yes to merge the release branch.")
        self.conversation.phone_message({"t": "tap", "gen": 0})
        await self.settle()
        self.assertIsNone(self.conversation.readback)

    async def test_another_prompt_leaves_nothing_to_say_yes_to(self):
        await self.play_readback()
        self.finish_turn("What's the status?")
        self.assertEqual(self.order()["submit"], "What's the status?")
        self.yes()
        self.assertEqual(len(self.session.orders), 0)

    async def test_pausing_leaves_nothing_to_say_yes_to(self):
        await self.play_readback()
        self.finish_turn("Pause.")
        self.yes()  # while paused, a yes is only someone talking
        self.finish_turn("Go.")
        self.yes()
        await self.settle()
        self.assertEqual(len(self.session.orders), 0)
        self.assertIn(NOTHING_TO_CONFIRM, self.phone.said())

    async def test_a_cough_leaves_the_read_back_open(self):
        await self.play_readback()
        self.finish_turn("um")
        self.yes()
        self.assertIn("confirm", self.order())

    async def test_a_yes_answers_only_the_newest_reply(self):
        await self.play_readback()
        self.relay("note-2", "The build passed.")
        await self.past_the_gap()
        self.yes()
        self.assertEqual(self.order()["confirm"]["note"], "note-2")

    async def test_a_new_reply_closes_the_old_read_back_before_it_plays(self):
        await self.play_readback()
        self.relay("note-2", "The build passed.")
        self.yes()
        self.assertEqual(len(self.session.orders), 0)

    async def test_a_reply_cut_short_or_unspeakable_cannot_take_a_yes(self):
        self.session.front_desk = True
        self.choose()
        self.relay("note-1", "`git push`")
        await self.settle()
        self.assertIsNone(self.conversation.readback)
        self.relay("note-2", "Long sentence here. " * 100)
        await self.settle(0.5)
        self.assertIsNone(self.conversation.readback)

    async def test_a_yes_to_a_session_that_is_no_front_desk_is_an_ordinary_prompt(self):
        self.choose()
        self.yes()
        self.assertEqual(self.order()["submit"], "Yes.")

    async def test_a_relay_from_a_session_not_chosen_is_not_spoken(self):
        self.session.front_desk = True
        self.relay("note-1", "Say yes to merge the release branch.")
        await self.settle()
        self.assertEqual(self.phone.said(), [])

    def answer(self, order, text):
        self.event(kind="turn-start", turnId="turn-1", order=order["id"])
        self.event(kind="text", turnId="turn-1", text=text)
        self.event(kind="turn-end", turnId="turn-1", reason="answer")

    async def test_talking_over_a_relayed_reply_keeps_the_later_ones_as_a_tap_does(self):
        self.messages_end_quickly()
        self.session.front_desk = True
        self.choose()
        await self.hear(QUIET, 1.1)
        with mock.patch(__name__ + ".SPOKEN_SECONDS", 1.0):
            self.relay("note-1", "The build passed.")
            self.relay("note-2", "The deploy failed.")
            await self.settle()
            self.conversation.phone_message({"t": "hold", "id": 30})
            await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.assertEqual([relayed.note for relayed in self.conversation.held_relays], ["note-2"])
        self.assertIsNone(self.conversation.readback)

    async def test_a_tap_cuts_only_the_reply_playing_and_the_rest_wait_for_the_next_response(self):
        self.session.front_desk = True
        self.choose()
        with mock.patch(__name__ + ".SPOKEN_SECONDS", 1.0):
            self.relay("note-1", "The build passed.")
            self.relay("note-2", "The deploy failed.")
            await self.settle()
            self.assertEqual(self.phone.said(), ["The build passed."])
            self.conversation.phone_message({"t": "tap", "gen": 0})
        self.relay("note-3", "Say yes to merge the release branch.")
        self.finish_turn("um")
        await self.settle()
        self.assertEqual(self.phone.said(), ["The build passed."])
        self.finish_turn("What's the status?")
        await self.settle()
        self.assertEqual(self.phone.said(), ["The build passed."])
        self.answer(self.order_for("submit"), "Two workers are running.")
        expected = ["The build passed.", "Two workers are running.", "The deploy failed.", "Say yes to merge the release branch."]
        await self.by_then(lambda: self.conversation.readback is not None)  # once the read-back's words have gone out
        self.assertEqual(self.phone.said(), expected)
        self.assertEqual(self.conversation.readback.note, "note-3")

    async def test_a_tap_during_the_response_keeps_the_replies_waiting(self):
        self.session.front_desk = True
        self.choose()
        self.conversation.phone_message({"t": "tap", "gen": 0})
        self.relay("note-1", "The build passed.")
        self.finish_turn("What's the status?")
        order = self.order_for("submit")
        self.event(kind="turn-start", turnId="turn-1", order=order["id"])
        self.conversation.phone_message({"t": "tap", "gen": 0})
        self.event(kind="turn-end", turnId="turn-1", reason="aborted")
        await self.settle()
        self.assertEqual(self.phone.said(), [])
        self.finish_turn("What's the build doing?")
        self.answer(self.order_for("submit"), "It's running.")
        await self.settle()
        self.assertEqual(self.phone.said(), ["It's running.", "The build passed."])

    async def test_a_tap_does_not_silence_a_failed_hand_off_or_release_the_replies_waiting(self):
        self.session.front_desk = True
        self.choose()
        self.relay("note-1", "The build passed.")
        self.conversation.phone_message({"t": "tap", "gen": 0})
        self.relay("note-2", "The deploy failed.")
        self.finish_turn("Merge the release branch.")
        order = self.order_for("submit")
        self.event(kind="turn-start", turnId="desk-1", order=order["id"])
        self.conversation.phone_message({"t": "tap", "gen": 0})
        self.event(kind="not-filed")
        self.event(kind="turn-end", turnId="desk-1", reason="answer")
        await self.settle()
        self.assertEqual(self.phone.said(), [NOT_FILED])

    async def test_a_yes_with_replies_waiting_hears_its_notice_first_and_the_newest_can_take_the_next_yes(self):
        self.session.front_desk = True
        self.choose()
        self.conversation.phone_message({"t": "tap", "gen": 0})
        self.relay("note-1", "Say yes to merge the release branch.")
        self.yes()
        await self.past_the_gap()
        self.assertEqual(self.phone.said(), [NOTHING_TO_CONFIRM, "Say yes to merge the release branch."])
        self.session.orders.clear()
        await self.settle(module.OUTPUT_DELAY_SECONDS)
        self.yes()
        self.assertEqual(self.order()["confirm"]["note"], "note-1")

    async def test_a_tap_before_a_reply_sounds_keeps_that_reply(self):
        self.session.front_desk = True
        self.choose()
        self.relay("note-1", "The build passed.")
        self.conversation.phone_message({"t": "tap", "gen": 0})
        await self.past_the_gap()
        self.assertEqual(self.phone.said(), [])
        self.finish_turn("Cancel.")
        await self.past_the_gap()
        self.assertIn("The build passed.", self.phone.said())

    async def test_a_second_tap_keeps_the_replies_already_waiting(self):
        self.session.front_desk = True
        self.choose()
        self.conversation.phone_message({"t": "tap", "gen": 0})
        self.relay("note-1", "The build passed.")
        self.conversation.phone_message({"t": "tap", "gen": 0})
        self.finish_turn("Cancel.")
        await self.past_the_gap()
        self.assertIn("The build passed.", self.phone.said())

    async def test_a_new_run_or_a_session_switch_drops_the_waiting_replies(self):
        self.session.front_desk = True
        for cut in (self.conversation.phone_new_run, lambda: self.choose("8")):
            with self.subTest(cut=cut):
                self.choose()
                self.conversation.phone_message({"t": "tap", "gen": 0})
                self.relay("note-1", "The build passed.")
                cut()
                self.choose()
                self.finish_turn("Cancel.")
                await self.settle()
                self.assertNotIn("The build passed.", self.phone.said())

    async def test_a_newer_reply_that_cannot_take_a_yes_ends_the_wait_for_an_older_one(self):
        self.session.front_desk = True
        self.choose()
        self.relay("note-1", "Say yes to merge the release branch.")
        self.relay("note-2", "`git push`")
        await self.past_the_gap()
        self.yes()
        await self.past_the_gap()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said()[-1], NOTHING_TO_CONFIRM)

    async def test_a_reply_with_a_newer_one_queued_cannot_take_a_yes(self):
        self.session.front_desk = True
        self.choose()
        with mock.patch(__name__ + ".SPOKEN_SECONDS", 0.02):
            self.relay("note-1", "Say yes to merge the release branch.")
            self.relay("note-2", "The build passed.")
            for _ in range(200):
                await asyncio.sleep(0.005)
                if not self.conversation._queued_relays():
                    break
                self.assertIsNone(self.conversation.readback)
        self.assertEqual(self.conversation.readback.note, "note-2")

    async def test_a_reply_that_cannot_be_synthesized_gets_a_notice_and_the_next_one_plays(self):
        self.session.front_desk = True
        self.choose()
        self.conversation.speech.unspeakable = ("Merge the release branch.",)
        with self.assertLogs("walkie.conversation", "ERROR"):
            self.relay("note-1", "Merge the release branch. Say yes to confirm.")
            await self.past_the_gap()
        self.assertEqual(self.phone.said(), [REPLY_NOT_SPOKEN])
        self.assertIsNone(self.conversation.readback)
        with self.assertLogs("walkie.conversation", "ERROR"):
            self.relay("note-2", "Merge the release branch. Say yes to confirm.")
            self.relay("note-3", "The build passed.")
            await self.past_the_gap()
        self.assertEqual(self.phone.said()[-1], "The build passed.")
        await self.settle(module.OUTPUT_DELAY_SECONDS)
        self.yes()
        self.assertEqual(self.order()["confirm"]["note"], "note-3")

    async def test_a_failed_newer_reply_leaves_no_older_read_back_to_say_yes_to(self):
        self.session.front_desk = True
        self.choose()
        self.conversation.speech.unspeakable = ("The deploy failed.",)
        with self.assertLogs("walkie.conversation", "ERROR"):
            self.relay("note-1", "Say yes to merge the release branch.")
            self.relay("note-2", "The deploy failed.")
            await self.past_the_gap()
        self.assertEqual(self.phone.said(), ["Say yes to merge the release branch.", REPLY_NOT_SPOKEN])
        self.assertIsNone(self.conversation.readback)

    # --- confirming in the app ----------------------------------------------------

    async def test_a_spoken_yes_alone_sends_nothing_and_asks_for_the_app(self):
        await self.play_readback()
        self.finish_turn("Yes.")
        await self.settle()
        self.assertEqual(len(self.session.orders), 0)
        asked = [c for c in self.phone.controls if c["t"] == "confirm"][-1]
        self.assertEqual(asked["text"], "Say yes to merge the release branch.")
        self.assertEqual(self.phone.said()[-1], module.CONFIRM_IN_APP)

    async def test_the_fingerprint_signs_the_exact_words_heard(self):
        await self.play_readback()
        self.yes()
        nonce = [c for c in self.phone.controls if c["t"] == "confirm"][0]["nonce"]
        self.assertEqual(self.phone.last_signed,
                         module.security.confirm_message(nonce, "Say yes to merge the release branch."))

    async def test_a_bad_signature_confirms_nothing(self):
        await self.play_readback()
        self.finish_turn("Yes.")
        self.confirm_in_app(sig="forged")
        await self.settle()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said()[-1], module.NOT_CONFIRMED)
        self.confirm_in_app()  # and it can't be tried again
        self.assertEqual(len(self.session.orders), 0)

    async def test_declining_in_the_app_confirms_nothing(self):
        await self.play_readback()
        self.finish_turn("Yes.")
        nonce = [c for c in self.phone.controls if c["t"] == "confirm"][-1]["nonce"]
        self.conversation.phone_message({"t": "declined", "nonce": nonce})
        self.confirm_in_app()
        await self.settle()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said()[-1], module.NOT_CONFIRMED)

    async def test_a_late_fingerprint_confirms_nothing(self):
        await self.play_readback()
        self.finish_turn("Yes.")
        nonce, readback, _ = self.conversation.confirming
        self.conversation.confirming = (nonce, readback, self.conversation.clock() - 1)
        self.confirm_in_app()
        self.assertEqual(len(self.session.orders), 0)

    async def withdrawn_by(self, cut):
        await self.play_readback()
        self.finish_turn("Yes.")
        cut()
        self.assertEqual([c for c in self.phone.controls if c["t"] == "confirm"][-1], {"t": "confirm", "nonce": None})
        self.session.orders.clear()
        self.confirm_in_app()
        self.assertEqual(len(self.session.orders), 0)

    async def test_a_tap_withdraws_the_app_confirmation(self):
        await self.withdrawn_by(lambda: self.conversation.phone_message({"t": "tap", "gen": 0}))

    async def test_another_prompt_withdraws_the_app_confirmation(self):
        await self.withdrawn_by(lambda: self.finish_turn("What's the status?"))

    # --- switching sessions by voice -----------------------------------------------

    def two_logins(self):
        """The owner's two sessions: Firstmate on the work login, and one in a folder of the same name on the personal one."""
        work, personal = self.link.sessions["7"], self.link.sessions["8"]
        work.cwd, work.login = "/Users/me/Code/firstmate", "default"
        personal.cwd, personal.login = "/Users/me/home/firstmate", "personal"
        self.choose("7")
        return work, personal

    def asked_switch(self):
        return [c for c in self.phone.controls if c["t"] == "confirm"][-1]

    async def test_a_spoken_switch_asks_the_app_for_a_fingerprint_and_moves_nothing_yet(self):
        _, personal = self.two_logins()
        self.finish_turn("Switch session.")
        await self.settle()
        asked = self.asked_switch()
        self.assertEqual(asked["switch"], {"id": "8", "modNonce": "nonce-8"})
        self.assertEqual(asked["text"], "Switch to firstmate personal?")
        self.assertEqual(self.phone.said(), [module.SWITCH_IN_APP % "firstmate personal"])
        self.assertEqual(self.conversation.chosen, "7")
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(len(personal.orders), 0)

    async def test_the_fingerprint_signs_the_named_session_as_at_start_and_the_switch_says_where_you_are(self):
        work, personal = self.two_logins()
        self.finish_turn("Switch session.")
        nonce = self.asked_switch()["nonce"]
        self.confirm_in_app()
        self.assertEqual(self.asked_switch(), {"t": "confirm", "nonce": None})  # the app stops asking
        self.assertEqual(self.phone.last_signed, module.security.start_message(nonce, "run-1", "8", "nonce-8"))
        self.assertEqual(self.conversation.chosen, "8")
        self.assertEqual(personal.orders.popleft(),
                         {"grant": {"session": "8", "modNonce": "nonce-8", "nonce": nonce, "run": "run-1", "sig": "good"}})
        self.assertIsNone(self.conversation.chosen_run(work))
        self.assertEqual(self.conversation.chosen_run(personal), "run-1")
        await self.settle()
        self.assertEqual(self.phone.said()[-1], "Connected to firstmate personal.")

    async def test_prompts_after_a_switch_go_to_the_new_session(self):
        work, personal = self.two_logins()
        self.finish_turn("Switch to first mate personal.")
        self.confirm_in_app()
        personal.orders.clear()
        self.finish_turn("What's the status?")
        self.assertEqual(personal.orders.popleft()["submit"], "What's the status?")
        self.assertEqual(len(work.orders), 0)

    async def test_a_switch_finds_a_session_by_its_whole_name_only(self):
        self.two_logins()
        self.finish_turn("Switch to first mate personal.")
        self.assertEqual(self.asked_switch()["switch"]["id"], "8")
        self.choose("8")
        self.finish_turn("Switch to first mate.")
        self.assertEqual(self.asked_switch()["switch"]["id"], "7")

    async def test_a_folder_or_login_alone_is_an_ordinary_prompt(self):
        self.two_logins()
        for said in ("Switch to personal.", "Switch to personal first mate."):
            self.finish_turn(said)
            self.assertEqual(self.order()["submit"], said)
            self.assertIsNone(self.conversation.confirming)

    async def test_a_name_that_matches_no_session_is_an_ordinary_prompt(self):
        self.two_logins()
        self.finish_turn("Switch to the main branch.")
        self.assertEqual(self.order()["submit"], "Switch to the main branch.")
        self.assertIsNone(self.conversation.confirming)

    async def test_naming_the_session_you_are_on_is_an_ordinary_prompt(self):
        # A folder named after a common word, as in a worktree layout: "switch to main" is a git instruction for it.
        work, other = self.two_logins()
        work.cwd, work.login = "/Users/me/proj/main", "default"
        other.cwd, other.login = "/Users/me/proj/feature-x", "default"
        for said in ("Switch to main.", "Switch to main, then pull."):
            self.finish_turn(said)
            self.assertEqual(self.order()["submit"], said)
            self.assertIsNone(self.conversation.confirming)
        self.assertEqual(len(other.orders), 0)

    async def test_with_more_than_one_other_session_a_bare_switch_asks_which(self):
        self.two_logins()
        third = self.link.sessions["9"] = Session("9")
        third.cwd, third.nonce = "/work/walkie-talkie", "nonce-9"
        self.finish_turn("Switch session.")
        await self.settle()
        self.assertEqual(self.phone.said(), [module.SWITCH_WHICH % "firstmate personal, walkie-talkie"])
        self.assertIsNone(self.conversation.confirming)

    async def test_with_no_other_session_a_switch_says_so(self):
        del self.link.sessions["8"]
        self.choose("7")
        self.finish_turn("Switch session.")
        await self.settle()
        self.assertEqual(self.phone.said(), [module.NO_OTHER_SESSION])
        self.assertIsNone(self.conversation.confirming)

    async def not_switched(self):
        await self.settle()
        self.assertEqual(self.conversation.chosen, "7")
        self.assertEqual(len(self.link.sessions["8"].orders), 0)
        self.assertEqual(self.phone.said()[-1], module.NOT_SWITCHED % "firstmate")

    async def test_a_bad_signature_switches_nothing(self):
        self.two_logins()
        self.finish_turn("Switch session.")
        self.confirm_in_app(sig="forged")
        await self.not_switched()
        self.confirm_in_app()  # and it can't be tried again
        self.assertEqual(self.conversation.chosen, "7")

    async def test_declining_in_the_app_switches_nothing(self):
        self.two_logins()
        self.finish_turn("Switch session.")
        self.conversation.phone_message({"t": "declined", "nonce": self.asked_switch()["nonce"]})
        await self.not_switched()

    async def test_a_late_fingerprint_switches_nothing(self):
        self.two_logins()
        self.finish_turn("Switch session.")
        nonce, switch, _ = self.conversation.confirming
        self.conversation.confirming = (nonce, switch, self.conversation.clock() - 1)
        self.confirm_in_app()
        await self.not_switched()

    async def test_a_session_whose_challenge_moved_on_since_the_switch_is_not_chosen(self):
        _, personal = self.two_logins()
        self.finish_turn("Switch session.")
        personal.nonce = "nonce-8-new"
        self.confirm_in_app()
        await self.not_switched()

    async def test_a_tap_or_another_prompt_withdraws_a_switch(self):
        self.two_logins()
        for cut in (lambda: self.conversation.phone_message({"t": "tap", "gen": 0}),
                    lambda: self.finish_turn("What's the status?")):
            self.finish_turn("Switch session.")
            asked = self.asked_switch()
            cut()
            self.assertEqual(self.asked_switch(), {"t": "confirm", "nonce": None})
            self.conversation.phone_message({"t": "confirmed", "nonce": asked["nonce"], "sig": "good"})
            self.assertEqual(self.conversation.chosen, "7")

    async def test_a_yes_or_no_while_a_switch_waits_withdraws_it_and_reaches_no_session(self):
        # The switch notice invites a spoken yes, and the session you're leaving may have just asked a question.
        work, personal = self.two_logins()
        for desk in (False, True):
            work.front_desk = desk
            for answer in ("Yes.", "No."):
                self.finish_turn("Switch session.")
                asked = self.asked_switch()
                self.finish_turn(answer)
                self.assertEqual(self.asked_switch(), {"t": "confirm", "nonce": None})
                self.assertIsNone(self.conversation.confirming)
                await self.not_switched()
                self.assertEqual(len(work.orders), 0)
                self.conversation.phone_message({"t": "confirmed", "nonce": asked["nonce"], "sig": "good"})
                self.assertEqual(self.conversation.chosen, "7")
                self.assertEqual(len(personal.orders), 0)

    async def test_a_fingerprint_for_a_switch_that_a_later_one_replaced_moves_nothing_and_says_so(self):
        self.two_logins()
        third = self.link.sessions["9"] = Session("9")
        third.cwd, third.nonce = "/work/walkie-talkie", "nonce-9"
        self.finish_turn("Switch to first mate personal.")
        first = self.asked_switch()["nonce"]
        self.finish_turn("Switch to walkie talkie.")
        self.assertEqual(self.asked_switch()["switch"]["id"], "9")
        self.conversation.phone_message({"t": "confirmed", "nonce": first, "sig": "good"})
        await self.not_switched()
        self.assertEqual(len(third.orders), 0)
        self.confirm_in_app()  # the latest switch still stands
        self.assertEqual(self.conversation.chosen, "9")
        self.assertEqual(third.orders.popleft()["grant"]["session"], "9")

    async def test_a_switch_lets_the_session_before_go_as_stop_does(self):
        work, _ = self.two_logins()
        self.finish_turn("Write the release notes.")
        self.event(kind="order", id=self.order()["id"], accepted=True, busy=True)
        self.finish_turn("Switch session.")
        self.confirm_in_app()
        self.assertEqual(self.conversation.chosen, "8")
        await self.settle()
        self.assertIn(module.QUEUE_DROPPED, self.phone.said())
        self.event(kind="text", turnId="t1", text="Anything from the old session.")
        await self.settle()
        self.assertNotIn("Anything from the old session.", self.phone.said())

    # --- a run, from a fingerprint to its end --------------------------------------

    async def test_a_fingerprint_chooses_the_session_hands_the_mod_its_grant_and_names_it(self):
        self.session.cwd = "/work/project"
        grant = {"session": "7", "modNonce": "nonce-7", "nonce": "n", "run": "run-1", "sig": "s"}
        self.assertIsNone(self.conversation.start_problem("7", "nonce-7"))
        self.conversation.phone_approved(grant)
        self.assertEqual(self.conversation.chosen, "7")
        self.assertEqual(self.order(), {"grant": grant})
        await self.settle()
        self.assertEqual(self.phone.said(), ["Connected to project."])

    async def test_only_the_chosen_session_hears_the_run_the_fingerprint_approved_and_only_until_it_ends(self):
        # The mod keeps its grant only while answers name this run, so a listener that never asked the gateway can't ride it.
        self.conversation.phone_approved({"session": "7", "modNonce": "nonce-7", "nonce": "n", "run": "run-1", "sig": "s"})
        self.assertEqual(self.conversation.chosen_run(self.session), "run-1")
        self.assertIsNone(self.conversation.chosen_run(self.link.sessions["8"]))
        self.conversation.phone_message({"t": "release"})
        self.assertIsNone(self.conversation.chosen_run(self.session))

    async def test_only_the_challenge_the_phone_saw_can_start_a_session(self):
        self.assertEqual(self.conversation.start_problem("7", "nonce-8"), "changed")
        self.session.nonce = ""
        self.assertEqual(self.conversation.start_problem("7", ""), "gone")

    async def ended(self):
        return [c["why"] for c in self.phone.controls if c["t"] == "ended"]

    async def test_stop_ends_the_run(self):
        self.choose()
        self.conversation.phone_message({"t": "release"})
        self.assertIsNone(self.conversation.chosen)
        self.assertFalse(self.phone.approved)

    async def test_the_mod_letting_go_ends_the_run_and_says_so(self):
        with mock.patch.object(module, "ENDED_AFTER_SECONDS", 0.05):
            self.choose()
            self.event(kind="grant-ended", reason="none")
            self.assertIsNone(self.conversation.chosen)
            self.assertFalse(self.phone.approved)
            self.assertEqual(await self.ended(), [])
            await self.settle()
        self.assertEqual(self.phone.said(), [module.SESSION_CLOSED])
        self.assertEqual(await self.ended(), ["closed"])

    async def test_an_unchosen_session_cannot_end_the_run(self):
        self.choose()
        self.conversation.mod_event(self.link.sessions["8"], {"kind": "grant-ended"})
        self.assertEqual(self.conversation.chosen, "7")
        self.assertTrue(self.phone.approved)

    async def test_a_session_that_goes_ends_the_run(self):
        with mock.patch.object(module, "ENDED_AFTER_SECONDS", 0.05):
            self.choose()
            self.event(kind="bye")
            await self.settle()
        self.assertFalse(self.phone.approved)
        self.assertEqual(await self.ended(), ["gone"])

    async def test_an_ended_notice_never_ends_a_newer_run(self):
        with mock.patch.object(module, "ENDED_AFTER_SECONDS", 0.05):
            self.choose()
            self.event(kind="grant-ended")
            self.phone.run_id = self.phone.approved_run = "run-2"  # Start again at once
            await self.settle()
        self.assertEqual(await self.ended(), [])

    async def test_thirty_minutes_with_nothing_said_ends_the_run(self):
        self.choose()
        self.conversation.active_at -= module.IDLE_SECONDS - 5
        self.conversation._check_run()
        self.assertTrue(self.phone.approved)
        self.finish_turn("Still here.")
        self.conversation.active_at -= module.IDLE_SECONDS + 1
        self.conversation._check_run()
        self.assertFalse(self.phone.approved)
        self.assertIsNone(self.conversation.chosen)
        await self.settle()
        self.assertIn(module.IDLE_ENDED, self.phone.said())

    async def test_a_reply_still_coming_keeps_the_run(self):
        self.choose()
        self.event(kind="turn-start", turnId="long")
        self.conversation.active_at -= module.IDLE_SECONDS + 1
        self.conversation._check_run()
        self.assertTrue(self.phone.approved)

    async def test_a_phone_gone_two_minutes_ends_the_run_with_nobody_to_tell(self):
        self.choose()
        self.phone.connected = False
        self.conversation._check_run()
        self.assertTrue(self.phone.approved)
        self.conversation.lost_at -= module.PHONE_LOST_SECONDS - 5
        self.conversation._check_run()
        self.assertTrue(self.phone.approved)  # back within the time: nothing changes
        self.conversation.lost_at -= 10
        self.conversation._check_run()
        self.assertFalse(self.phone.approved)
        self.assertIsNone(self.conversation.chosen)
        self.assertEqual(self.phone.said(), [])

    async def test_a_phone_back_in_time_keeps_the_run(self):
        self.choose()
        self.phone.connected = False
        self.conversation._check_run()
        self.phone.connected = True
        self.conversation._check_run()
        self.assertIsNone(self.conversation.lost_at)

    async def test_revoking_ends_the_run_and_the_pairing(self):
        self.choose()
        self.conversation.revoke_phone()
        self.assertIsNone(self.conversation.chosen)
        self.assertFalse(self.phone.approved)
        self.assertTrue(self.phone.revoked)

    async def test_the_list_shows_the_latest_prompt_first_and_only_sessions_that_check_fingerprints(self):
        self.link.sessions["7"].prompted_at = 1000
        self.link.sessions["8"].prompted_at = 2000
        self.link.sessions["9"] = Session("9")  # a mod from before fingerprints
        self.conversation.sessions_changed()
        listed = self.phone.controls[-1]["list"]
        self.assertEqual([s["id"] for s in listed], ["8", "7"])
        self.assertEqual(listed[0]["nonce"], "nonce-8")

    # --- routing -----------------------------------------------------------------

    async def test_speech_goes_only_to_the_chosen_session(self):
        self.choose()
        self.finish_turn("Fix the failing test.")
        self.assertEqual(self.tones(), [])
        order = self.order()
        self.assertEqual(order["submit"], "Fix the failing test.")
        self.assertEqual(len(self.link.sessions["8"].orders), 0)

    async def test_nothing_is_routed_until_a_session_is_chosen(self):
        self.finish_turn("Fix the failing test.")
        await self.settle()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said(), [NO_SESSION])
        self.assertEqual(self.tones(), ["warn"])

    async def test_a_session_that_does_not_exist_cannot_be_started(self):
        self.assertEqual(self.conversation.start_problem("nope", "nonce-nope"), "gone")

    async def test_a_phone_message_can_no_longer_choose_a_session(self):
        self.conversation.phone_message({"t": "select", "session": "7"})
        self.assertIsNone(self.conversation.chosen)

    async def test_noise_goes_nowhere(self):
        self.choose()
        self.finish_turn("um")
        await self.settle()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.tones(), [])

    async def test_cancel_stops_what_is_running_like_a_tap(self):
        self.choose()
        self.event(kind="turn-start", turnId="t1")
        self.finish_turn("Oh never mind, cancel.")
        await self.settle()
        self.assertEqual(self.order()["abort"], "t1")
        self.assertEqual(self.tones(), [])
        self.assertEqual(self.phone.said(), [CANCELLED])

    async def test_cancel_sends_no_words_to_the_session(self):
        self.choose()
        self.finish_turn("Cancel.")
        order = self.order()
        self.assertEqual(order["abort"], None)
        self.assertNotIn("submit", order)

    async def test_cancel_drops_the_queue_and_says_so(self):
        # What the adversarial review heard: "Cancelled.", and about 5 s later the queued prompt ran.
        self.choose()
        self.finish_turn("Delete the old release branch.")
        queued = self.order()["id"]
        self.event(kind="order", id=queued, accepted=True, busy=True)
        await self.settle()
        self.finish_turn("Cancel.")
        abort = self.order()["id"]
        self.event(kind="order", id=queued, dropped=True)
        self.event(kind="order", id=abort, ok=True, dropped=True)
        await self.settle()
        self.assertEqual(self.phone.said(), [module.QUEUED, CANCELLED, module.QUEUE_DROPPED])
        self.assertEqual(self.conversation.orders, {})

    async def test_cancel_with_no_session_still_answers(self):
        self.finish_turn("Cancel.")
        await self.settle()
        self.assertEqual(self.phone.said(), [CANCELLED])

    async def test_a_prompt_that_ends_with_the_word_cancel_reaches_the_session(self):
        self.choose()
        self.finish_turn("Which of these jobs should I cancel")
        self.assertEqual(self.order()["submit"], "Which of these jobs should I cancel")

    async def test_a_prompt_never_starts_as_a_command(self):
        self.choose()
        self.finish_turn("/clear everything")
        self.assertEqual(self.order()["submit"], "clear everything")

    # --- replies -----------------------------------------------------------------

    async def test_a_reply_is_spoken_as_it_streams(self):
        self.choose()
        self.event(kind="turn-start", turnId="t")
        self.event(kind="text", turnId="t", text="Done. The tests")
        await self.settle()
        self.assertEqual(self.phone.said(), ["Done."])
        self.event(kind="text", turnId="t", text=" pass.")
        self.event(kind="turn-end", turnId="t", reason="answer")
        await self.settle()
        self.assertEqual(self.phone.said(), ["Done.", "The tests pass."])

    async def test_other_sessions_are_not_heard(self):
        self.choose()
        other = self.link.sessions["8"]
        self.conversation.mod_event(other, {"kind": "turn-start", "turnId": "x"})
        self.conversation.mod_event(other, {"kind": "text", "turnId": "x", "text": "Secret words. More."})
        await self.settle()
        self.assertEqual(self.phone.said(), [])

    async def test_text_from_another_turn_is_not_read(self):
        self.choose()
        self.event(kind="turn-start", turnId="t")
        self.event(kind="text", turnId="other", text="Wrong turn. More.")
        await self.settle()
        self.assertEqual(self.phone.said(), [])

    async def test_a_failed_turn_says_so(self):
        self.choose()
        self.event(kind="turn-start", turnId="t")
        self.event(kind="turn-end", turnId="t", reason="error")
        await self.settle()
        self.assertEqual(self.phone.said(), [module.TURN_FAILED])

    async def test_a_permission_prompt_during_a_spoken_turn_is_announced(self):
        self.choose()
        self.event(kind="turn-start", turnId="t")
        self.event(kind="permission")
        await self.settle()
        self.assertEqual(self.phone.said(), [PERMISSION])

    # --- the tap -----------------------------------------------------------------

    async def test_a_tap_cuts_speech_and_stops_the_turn(self):
        self.choose()
        self.event(kind="turn-start", turnId="t")
        self.conversation.say("A long sentence that keeps going.")
        await self.settle(0.02)
        gen_before = self.conversation.gen
        self.conversation.phone_message({"t": "tap"})
        self.assertEqual(self.conversation.gen, gen_before + 1)
        self.assertIn({"t": "cut", "gen": gen_before + 1}, self.phone.controls)
        self.assertEqual(self.order()["abort"], "t")
        sent = len(self.phone.speech)
        self.event(kind="text", turnId="t", text="Late words. After the tap.")
        await self.settle()
        self.assertEqual(len(self.phone.speech), sent)

    async def test_a_tap_catches_up_with_the_generation_the_phone_cut_to(self):
        self.choose()
        ahead = self.conversation.gen + 3
        self.conversation.phone_message({"t": "tap", "gen": ahead})
        self.assertEqual(self.conversation.gen, ahead)
        self.assertEqual(self.phone.controls[-1], {"t": "cut", "gen": ahead})
        self.conversation.say("The next reply.")
        await self.settle()
        self.assertEqual({gen for gen, _ in self.phone.speech}, {ahead})

    async def test_a_tap_sent_again_still_moves_the_generation_on(self):
        self.choose()
        self.conversation.phone_message({"t": "tap", "gen": self.conversation.gen + 1})
        again = self.conversation.gen
        self.conversation.phone_message({"t": "tap", "gen": again})
        self.assertEqual(self.conversation.gen, again + 1)

    async def test_a_tap_with_no_spoken_turn_names_no_turn(self):
        self.choose()
        self.conversation.phone_message({"t": "tap"})
        self.assertIsNone(self.order()["abort"])

    async def test_a_tap_after_the_turn_ended_names_no_turn(self):
        self.choose()
        self.event(kind="turn-start", turnId="t")
        self.event(kind="turn-end", turnId="t", reason="answer")
        self.conversation.phone_message({"t": "tap"})
        self.assertIsNone(self.order()["abort"])

    async def test_speech_made_before_a_tap_is_not_played_after_it(self):
        self.choose()
        self.conversation.say("Queued before the tap.")
        self.conversation.phone_message({"t": "tap"})
        await self.settle()
        self.assertEqual(self.phone.speech, [])

    # --- talking over a reply -----------------------------------------------------

    def messages_end_quickly(self):
        """So these tests needn't sit through the two seconds that end a message."""
        for name, value in (("END_HOLD", 0.6), ("THINKING_HOLD", 0.8)):
            self.addCleanup(setattr, turns, name, getattr(turns, name))
            setattr(turns, name, value)

    async def hear(self, packet, seconds):
        """The microphone, in real time, so the conversation's own clock sees it."""
        for _ in range(int(seconds / SECONDS)):
            self.conversation.mic_packet(packet)
            await asyncio.sleep(SECONDS)

    async def reply_sounding(self, seconds=30):
        """A long reply in a running turn, and enough quiet first that the floor has settled."""
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        self.conversation.frames.extend((b"reply:%d" % i, None, True, [], None, False) for i in range(int(seconds * 50)))
        self.conversation.frames_ready.set()
        await self.settle(0.1)

    def controls(self, kind):
        return [c for c in self.phone.controls if c["t"] == kind]

    def cuts(self):
        return len(self.controls("cut"))

    async def test_a_reply_leaking_into_the_mic_starts_nothing(self):
        await self.reply_sounding()
        cuts = self.cuts()
        await self.hear(VOICE, 1)
        self.assertFalse(self.conversation.detector.talking)
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.cuts(), cuts)

    async def test_speech_that_carries_on_stops_the_reply_and_its_turn_and_is_the_next_message(self):
        await self.reply_sounding()
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 1})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.assertEqual(self.cuts(), cuts + 1)
        self.assertEqual(self.order()["abort"], "t")
        self.assertEqual(len(self.conversation.frames), 0)
        await self.hear(QUIET, 0.8)
        self.assertEqual(self.order()["submit"], "what you said")
        self.assertEqual(self.controls("resume"), [])

    async def test_a_cough_lets_the_reply_resume_and_cancels_nothing(self):
        await self.reply_sounding()
        self.conversation.owner_level = turns.loudness(np.frombuffer(VOICE, dtype=np.int16)) + 20  # a cough, quieter than the owner
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 4})
        sent = len(self.phone.speech)
        await self.hear(VOICE, 0.1)
        await self.settle(0.1)
        self.assertEqual(len(self.phone.speech), sent)  # nothing is sent during a hold
        await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.7)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 4, "leak": True}])
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(len(self.session.orders), 0)
        await self.settle(0.1)
        self.assertGreater(len(self.phone.speech), sent)
        self.assertEqual(self.conversation.reply_turn, "t")

    async def test_a_short_noise_the_transcriber_calls_nothing_also_resumes(self):
        await self.reply_sounding()
        self.conversation.speech.transcribe = lambda audio: ""
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 2})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.assertEqual(self.cuts(), cuts)
        await self.hear(QUIET, 1)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 2, "leak": False}])
        self.assertEqual(len(self.session.orders), 0)

    async def test_the_reply_coming_back_through_the_mic_is_never_a_prompt(self):
        self.choose()
        self.conversation.say("the tests pass on the main branch today")
        await self.settle(0.05)
        self.conversation.speech.transcribe = lambda audio: "The tests pass on the main branch."
        self.event(kind="turn-start", turnId="t")
        self.conversation.frames.extend((b"reply:%d" % i, None, True, [], None, False) for i in range(1000))
        self.conversation.frames_ready.set()
        await self.settle(0.1)
        self.conversation.phone_message({"t": "hold", "id": 3})
        cuts = self.cuts()
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        await self.hear(QUIET, 1)
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 3, "leak": False}])

    async def test_a_message_that_cuts_a_reply_tells_the_session_it_was_not_all_heard(self):
        await self.reply_sounding()
        self.conversation.phone_message({"t": "hold", "id": 14})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.order()  # the abort
        await self.hear(QUIET, 0.8)
        order = self.order()
        self.assertEqual(order["submit"], "what you said")
        self.assertTrue(order["unheard"])
        self.finish_turn("A later message.")
        self.assertNotIn("unheard", self.order())

    async def talk_over_with(self, heard, hold_id=20):
        self.conversation.speech.transcribe = lambda audio: heard
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": hold_id})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        await self.hear(QUIET, 0.9)
        return self.cuts() - cuts

    async def reply_played(self, sentence):
        """A sentence that has played to its end, with more of the reply still to come."""
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        self.conversation.say(sentence, None, DEFAULT_VOICE)
        await self.settle(0.1)
        self.conversation.frames.extend((b"reply:%d" % i, None, True, [], None, False) for i in range(1000))
        self.conversation.frames_ready.set()
        await self.settle(0.1)

    async def test_answering_with_words_the_reply_just_played_is_a_message(self):
        await self.reply_played("Should I use option one or option two?")
        self.assertEqual(await self.talk_over_with("Option two."), 1)
        self.assertEqual(self.order()["abort"], "t")
        self.assertEqual(self.order()["submit"], "Option two.")
        self.assertEqual(self.controls("resume"), [])
        self.assertEqual(self.controls("leak"), [{"t": "leak"}])

    async def test_a_three_word_answer_from_the_reply_is_a_message(self):
        await self.reply_played("Should I use option two?")
        self.assertEqual(await self.talk_over_with("Use option two."), 1)
        self.assertEqual(self.order()["abort"], "t")
        self.assertEqual(self.order()["submit"], "Use option two.")

    async def test_four_words_that_just_played_are_a_message_and_a_lesson(self):
        await self.reply_played("the tests pass on the main branch")
        self.assertEqual(await self.talk_over_with("On the main branch."), 1)
        self.assertEqual(self.order()["abort"], "t")
        self.assertEqual(self.order()["submit"], "On the main branch.")
        self.assertEqual(self.controls("leak"), [{"t": "leak"}])

    async def test_a_long_answer_with_one_word_of_its_own_is_a_message_and_no_lesson(self):
        await self.reply_played("the tests pass on the main branch")
        self.assertEqual(await self.talk_over_with("Yes the tests pass on the main branch."), 1)
        self.assertEqual(self.order()["abort"], "t")
        self.assertEqual(self.order()["submit"], "Yes the tests pass on the main branch.")
        self.assertEqual(self.controls("leak"), [])

    async def test_saying_type_it_over_the_notice_that_offers_it_is_a_message(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.conversation.typing_offer = ("Fix the test.", self.conversation.clock() + 60)
        self.conversation.say(STOPPED_AT_KEYBOARD)
        await self.settle(0.1)
        self.conversation.frames.extend((b"reply:%d" % i, None, False, [], None, False) for i in range(1000))
        self.conversation.frames_ready.set()
        await self.settle(0.1)
        self.assertEqual(await self.talk_over_with("Type it."), 1)
        self.assertEqual(self.order()["type"], "Fix the test.")

    async def test_words_the_reply_has_not_played_yet_are_not_an_echo(self):
        await self.reply_sounding()
        self.conversation.say("the tests pass on the main branch", None, DEFAULT_VOICE)
        await self.settle(0.1)  # synthesized, and waiting behind the rest of the reply
        self.assertEqual(await self.talk_over_with("The tests pass on the main branch."), 1)
        self.assertEqual(self.order()["abort"], "t")
        self.assertEqual(self.order()["submit"], "The tests pass on the main branch.")
        self.assertEqual(self.controls("leak"), [])

    async def test_the_reply_words_in_another_order_are_not_an_echo(self):
        await self.reply_played("the tests pass on the main branch")
        self.assertEqual(await self.talk_over_with("On the branch the main tests pass."), 1)
        self.assertEqual(self.order()["abort"], "t")

    async def test_words_played_too_long_ago_are_not_an_echo(self):
        self.addCleanup(setattr, module, "ECHO_SECONDS", module.ECHO_SECONDS)
        module.ECHO_SECONDS = 0.05
        await self.reply_played("the tests pass on the main branch")
        self.assertEqual(await self.talk_over_with("The tests pass on the main branch."), 1)
        self.assertEqual(self.order()["abort"], "t")

    async def test_five_words_that_just_played_are_an_echo(self):
        await self.reply_played("the tests pass on the main branch")
        self.assertEqual(await self.talk_over_with("Pass on the main branch."), 0)
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 20, "leak": False}])

    async def test_speech_that_ends_inside_the_output_delay_is_the_earbuds_and_resumes(self):
        # What the independent review heard: the tail of a reply reached the session as "The log".
        await self.reply_sounding()
        self.conversation.speech.transcribe = lambda audio: "The log."
        self.conversation.owner_level = turns.loudness(np.frombuffer(VOICE, dtype=np.int16)) + 20  # the earbuds are quieter than the owner
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 5})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS - 0.1)
        await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.2)
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 5, "leak": True}])
        self.assertEqual(self.conversation.reply_turn, "t")

    async def test_a_voice_quieter_than_the_owner_stops_nothing(self):
        # The independent review's passer-by stopped a running turn from 7 to 12 dB under the owner.
        await self.reply_sounding()
        self.conversation.owner_level = turns.loudness(np.frombuffer(VOICE, dtype=np.int16))
        quieter = (np.frombuffer(VOICE, dtype=np.int16) // 4).tobytes()  # 12 dB
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 21})
        await self.hear(quieter, module.OUTPUT_DELAY_SECONDS + 0.6)
        self.assertEqual(self.cuts(), cuts)
        await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.2)
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual([c["id"] for c in self.controls("resume")], [21])
        self.assertEqual(self.conversation.reply_turn, "t")

    async def repeat_a_word_inside_the_output_delay(self, hold_ids):
        for hold_id in hold_ids:
            self.conversation.phone_message({"t": "hold", "id": hold_id})
            await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS - 0.2)
            await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.2)

    async def test_the_owners_repeated_short_words_cannot_teach_the_phone_their_voice_is_leak(self):
        # The review's sequence: each lesson raised the gate's threshold 6 dB, until the owner's voice no longer tripped it.
        await self.reply_sounding()
        self.conversation.owner_level = turns.loudness(np.frombuffer(VOICE, dtype=np.int16))
        await self.repeat_a_word_inside_the_output_delay((31, 32, 33))
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": i, "leak": False} for i in (31, 32, 33)])
        self.assertEqual(self.controls("leak"), [])

    async def test_a_short_word_teaches_the_phone_until_the_owners_level_is_known(self):
        await self.reply_sounding()
        self.conversation.owner_level = None
        await self.repeat_a_word_inside_the_output_delay((36,))
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 36, "leak": True}])

    async def test_speech_that_carries_past_the_output_delay_teaches_the_phone_nothing(self):
        await self.reply_sounding()
        self.conversation.owner_level = None
        self.conversation.phone_message({"t": "hold", "id": 37})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.1)
        await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.2)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 37, "leak": False}])

    def hold_lines(self, logs):
        return [json.loads(line.split("hold ", 1)[1]) for line in logs.output if ":hold " in line]

    async def test_the_log_gives_the_level_of_a_quieter_voice_refused_under_a_hold(self):
        await self.reply_sounding()
        voice = np.frombuffer(VOICE, dtype=np.int16)
        self.conversation.owner_level = turns.loudness(voice)
        with self.assertLogs("walkie.conversation", "INFO") as logs:
            self.conversation.phone_message({"t": "hold", "id": 34})
            await self.hear((voice // 4).tobytes(), module.OUTPUT_DELAY_SECONDS + 0.6)
            await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.2)
        line = self.hold_lines(logs)[0]
        self.assertEqual(line["outcome"], "resumed")
        self.assertGreater(line["voiced_in_delay"], 0)
        self.assertGreater(line["voiced_after"], 0)
        self.assertAlmostEqual(line["level"], turns.loudness(voice // 4), 0)
        self.assertAlmostEqual(line["owner_level"], turns.loudness(voice), 0)

    async def test_the_log_counts_speech_that_ended_inside_the_output_delay(self):
        await self.reply_sounding()
        with self.assertLogs("walkie.conversation", "INFO") as logs:
            self.conversation.phone_message({"t": "hold", "id": 35})
            await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS - 0.2)
            await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.2)
        line = self.hold_lines(logs)[0]
        self.assertGreater(line["voiced_in_delay"], 0)
        self.assertEqual(line["voiced_after"], 0)
        self.assertIsNone(line["level"])

    async def test_a_voice_as_loud_as_the_owner_counts(self):
        await self.reply_sounding()
        self.conversation.owner_level = turns.loudness(np.frombuffer(VOICE, dtype=np.int16))
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 22})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.assertEqual(self.cuts(), cuts + 1)

    async def test_until_the_owner_has_spoken_a_voice_cannot_stop_a_turn(self):
        await self.reply_sounding()
        self.conversation.owner_level = None
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 23})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.6)
        await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.2)
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.conversation.reply_turn, "t")

    async def test_the_owners_level_comes_from_their_last_message(self):
        self.messages_end_quickly()
        self.choose()
        self.conversation.owner_level = None
        await self.hear(QUIET, 1.1)
        await self.hear(VOICE, 0.6)
        await self.hear(QUIET, 0.9)
        self.assertEqual(self.order()["submit"], "what you said")
        self.assertAlmostEqual(self.conversation.owner_level, turns.loudness(np.frombuffer(VOICE, dtype=np.int16)), 1)

    async def test_a_message_over_a_reply_cannot_replace_words_waiting_to_be_typed(self):
        # The independent review's sequence: an echo overwrote the offer, and "type it" typed the echo.
        await self.reply_sounding()
        self.finish_turn("Delete the old release branch.")
        order = self.order()
        self.event(kind="order", id=order["id"], drop="stopped")
        self.assertEqual(self.conversation.typing_offer[0], "Delete the old release branch.")
        self.conversation.over_a_reply = True
        self.finish_turn("This session was")
        order = self.order()
        self.event(kind="order", id=order["id"], drop="stopped")
        self.assertEqual(self.conversation.typing_offer[0], "Delete the old release branch.")
        self.finish_turn("Type it.")
        self.assertEqual(self.order()["type"], "Delete the old release branch.")

    async def test_a_reply_still_reaching_the_earbuds_as_it_ends_warns_nobody(self):
        # The independent review heard the warning tone with nobody talking, from leak after the mic had opened.
        self.choose()
        await self.hear(QUIET, 1.1)
        self.conversation.say("A sentence.", None, DEFAULT_VOICE)
        await self.settle(SPOKEN_SECONDS + 0.1)
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS - 0.2)  # the end of the reply, on its way
        await self.hear(QUIET, 1.5)
        self.assertEqual(self.tones(), [])
        self.assertEqual(len(self.session.orders), 0)

    async def test_talking_over_a_reply_whose_turn_has_ended_stops_only_the_sound(self):
        await self.reply_sounding()
        self.event(kind="turn-end", turnId="t", reason="answer")
        self.conversation.phone_message({"t": "hold", "id": 6})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        await self.hear(QUIET, 0.8)
        self.assertEqual(self.order()["submit"], "what you said")
        self.assertEqual(len(self.session.orders), 0)

    async def test_a_hold_with_nothing_playing_resumes_at_once(self):
        self.choose()
        self.conversation.phone_message({"t": "hold", "id": 7})
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 7, "leak": False}])
        self.assertIsNone(self.conversation.hold)

    async def test_a_hold_ends_when_the_mic_stops(self):
        await self.reply_sounding()
        self.conversation.phone_message({"t": "hold", "id": 8})
        self.conversation.last_mic = self.conversation.clock() - module.MIC_STALE_SECONDS - 1
        await self.settle(0.1)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 8, "leak": False}])

    async def test_a_knock_with_no_speech_in_it_teaches_the_phone_nothing(self):
        await self.reply_sounding()
        self.conversation.phone_message({"t": "hold", "id": 10})
        await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.1)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 10, "leak": False}])

    async def test_a_tap_during_a_hold_needs_no_resume(self):
        await self.reply_sounding()
        self.conversation.phone_message({"t": "hold", "id": 9})
        self.conversation.phone_message({"t": "tap"})
        await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.1)
        self.assertEqual(self.controls("resume"), [])

    async def test_a_false_alarm_resumes_without_waiting_for_a_message_to_end(self):
        await self.reply_sounding()
        turns.END_HOLD, turns.THINKING_HOLD = 30, 30  # a hold mustn't depend on these
        self.conversation.speech.transcribe = lambda audio: ""
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 11})
        await self.hear(VOICE, 0.4)
        self.assertEqual(self.controls("resume"), [])
        await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.2)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 11, "leak": False}])
        self.assertEqual(self.cuts(), cuts)
        self.assertFalse(self.conversation.detector.talking)

    async def test_a_short_word_resumes_the_reply_without_waiting_for_a_message_to_end(self):
        # A very short word can't be told from the tail of the reply, so it fails toward not stopping a turn.
        await self.reply_sounding()
        turns.END_HOLD, turns.THINKING_HOLD = 30, 30
        self.conversation.speech.transcribe = lambda audio: "Stop."
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 12})
        await self.hear(VOICE, 0.3)
        await self.hear(QUIET, module.OUTPUT_DELAY_SECONDS + module.HOLD_WAIT_SECONDS)
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 12, "leak": False}])
        self.assertEqual(len(self.session.orders), 0)
        self.assertFalse(self.conversation.detector.talking)

    async def test_words_right_after_a_short_word_in_a_replys_tail_are_a_message(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.conversation.frames.extend((b"reply:%d" % i, None, True, [], None, False) for i in range(10))
        self.conversation.frames_ready.set()
        await self.hear(QUIET, 0.3)
        self.assertFalse(self.conversation.frames)
        self.assertLess(self.conversation.played_until, self.conversation.clock())
        self.conversation.synthesizing = ("Next sentence.", None, DEFAULT_VOICE)
        self.conversation.phone_message({"t": "hold", "id": 17})
        self.assertIsNotNone(self.conversation.hold, "the hold should open in the reply's tail")
        await self.hear(VOICE, 0.2)
        await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.1)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 17, "leak": False}])
        await self.hear(VOICE, 0.6)
        await self.hear(QUIET, 0.9)
        self.assertEqual(self.tones(), [])
        self.assertEqual(self.order()["submit"], "what you said")

    async def play_out_a_reply(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.relay("note-1", "Say yes to merge the release branch.")
        await self.hear(QUIET, 0.3)
        self.assertFalse(self.conversation.frames)
        self.assertLess(self.conversation.played_until, self.conversation.clock())
        self.assertTrue(self.conversation._sounding(), "the reply's tail should still be sounding")

    async def test_a_yes_inside_the_read_backs_output_delay_confirms_it(self):
        self.session.front_desk = True
        await self.play_out_a_reply()
        self.assertTrue(self.conversation.readback)
        self.conversation.speech.transcribe = lambda audio: "Yes."
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 18})
        await self.hear(VOICE, 0.35)
        await self.hear(QUIET, 0.9)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 18, "leak": False}])
        self.assertEqual(self.cuts(), cuts)
        self.confirm_in_app()
        self.assertEqual(self.order()["confirm"]["note"], "note-1")
        self.assertNotIn(NOTHING_TO_CONFIRM, self.phone.said())

    async def test_a_yes_after_the_output_delay_confirms_the_read_back(self):
        self.session.front_desk = True
        await self.play_out_a_reply()
        self.conversation.speech.transcribe = lambda audio: "Yes."
        await self.hear(QUIET, module.OUTPUT_DELAY_SECONDS)
        await self.hear(VOICE, 0.35)
        await self.hear(QUIET, 0.9)
        self.confirm_in_app()
        self.assertEqual(self.order()["confirm"]["note"], "note-1")
        self.assertNotIn(NOTHING_TO_CONFIRM, self.phone.said())

    async def test_an_answer_right_after_a_reply_ends_is_not_marked_unheard(self):
        await self.play_out_a_reply()
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 19})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        await self.hear(QUIET, 0.9)
        self.assertEqual(self.cuts(), cuts)
        order = self.order()
        self.assertEqual(order["submit"], "what you said")
        self.assertNotIn("unheard", order)
        self.assertFalse(self.conversation.orders[order["id"]][2], "it wasn't said over a reply")

    async def test_talking_over_a_notice_stops_no_turn(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        self.conversation.say(module.QUEUED)
        await self.settle(0.1)
        self.conversation.phone_message({"t": "hold", "id": 13})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        await self.hear(QUIET, 0.9)
        self.assertEqual(self.order()["submit"], "what you said")
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.conversation.reply_turn, "t")

    async def test_talking_over_a_notice_keeps_the_reply_queued_behind_it(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        release = threading.Event()
        self.addCleanup(release.set)
        synthesize = self.conversation.speech.synthesize
        self.conversation.speech.synthesize = lambda text, voice: (
            release.wait(5) if text == "Second sentence." else None, synthesize(text, voice))[1]
        encode = self.conversation.encoder.encode
        self.conversation.encoder.encode = lambda text: (
            [b"%s:%d" % (text.encode(), i) for i in range(100)] if text == module.QUEUED else encode(text))
        self.conversation.say(module.QUEUED)
        await self.settle(0.1)
        for sentence in ("First sentence.", "Second sentence.", "Third sentence."):
            self.conversation.say(sentence, None, DEFAULT_VOICE)
        await self.settle(0.1)
        self.assertEqual(self.conversation.synthesizing[0], "Second sentence.")
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 14})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.assertEqual(self.cuts(), cuts + 1)
        heard_before_the_cut = len(self.phone.speech)
        release.set()
        await self.hear(QUIET, 0.9)
        await self.settle(0.3 + 3 * SPOKEN_SECONDS)
        after_the_cut = self.phone.speech[heard_before_the_cut:]
        self.assertEqual(list(dict.fromkeys(packet.rsplit(":", 1)[0] for _, packet in after_the_cut)),
                         ["First sentence.", "Second sentence.", "Third sentence."])
        self.assertEqual({gen for gen, _ in after_the_cut}, {self.conversation.gen})
        self.assertEqual(self.order()["submit"], "what you said")
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.conversation.reply_turn, "t")

    def speak_for_a_second(self, spoken):
        encode = self.conversation.encoder.encode
        self.conversation.encoder.encode = lambda text: (
            [b"%s:%d" % (text.encode(), i) for i in range(50)] if text == spoken else encode(text))

    async def test_talking_over_a_notices_last_moment_keeps_the_reply_behind_it(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        self.speak_for_a_second(module.QUEUED)
        self.conversation.say(module.QUEUED)
        self.conversation.say("First sentence.", None, DEFAULT_VOICE)
        await self.settle(0.8)  # all of the notice has gone ahead of the clock, and it still sounds
        self.assertEqual(self.phone.said(), [module.QUEUED])
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 17})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.assertEqual(self.cuts(), cuts + 1)
        await self.hear(QUIET, 0.9)
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS)
        self.assertEqual(self.phone.said(), [module.QUEUED, "First sentence."])
        self.assertEqual(self.order()["submit"], "what you said")
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.conversation.reply_turn, "t")

    async def test_a_notice_still_on_its_way_to_the_earbuds_keeps_the_reply_behind_it(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        self.speak_for_a_second(module.QUEUED)
        self.conversation.say(module.QUEUED)
        self.conversation.say("First sentence.", None, DEFAULT_VOICE)
        await self.settle(1.2)  # the notice was due to end, and the earbuds may still be playing it
        self.assertEqual(self.phone.said(), [module.QUEUED])
        cuts = self.cuts()
        self.conversation.phone_message({"t": "hold", "id": 19})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.assertEqual(self.cuts(), cuts + 1)
        await self.hear(QUIET, 0.9)
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS)
        self.assertEqual(self.phone.said(), [module.QUEUED, "First sentence."])
        self.assertEqual(self.order()["submit"], "what you said")
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.conversation.reply_turn, "t")

    async def test_talking_over_a_replys_last_moment_stops_its_turn_with_a_notice_behind_it(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        self.speak_for_a_second("First sentence.")
        self.conversation.say("First sentence.", None, DEFAULT_VOICE)
        self.conversation.say(module.QUEUED)
        await self.settle(0.8)
        self.assertEqual(self.phone.said(), ["First sentence."])
        self.conversation.phone_message({"t": "hold", "id": 18})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.assertEqual(self.order()["abort"], "t")
        self.assertEqual(self.phone.said(), ["First sentence."])

    async def test_talking_over_a_notice_after_a_reply_has_sounded_stops_no_turn(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        self.conversation.say("First sentence.", None, DEFAULT_VOICE)
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + module.OUTPUT_DELAY_SECONDS)
        self.conversation.say(module.QUEUED)
        await self.settle(0.1)
        self.conversation.phone_message({"t": "hold", "id": 15})
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        await self.hear(QUIET, 0.9)
        self.assertEqual(self.order()["submit"], "what you said")
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.conversation.reply_turn, "t")

    async def test_talking_between_sentences_waits_its_turn(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        self.conversation.say("First sentence.", None, DEFAULT_VOICE)
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + module.OUTPUT_DELAY_SECONDS)
        self.conversation.synthesizing = ("Next sentence.", None, DEFAULT_VOICE)  # the next sentence is on its way
        cuts = self.cuts()
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        await self.hear(QUIET, 0.9)
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(self.order()["submit"], "what you said")
        self.assertEqual(len(self.session.orders), 0)

    async def test_a_message_begun_in_silence_stops_nothing_when_the_next_sentence_arrives(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        self.conversation.say("Let me check the tests.", None, DEFAULT_VOICE)
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + module.OUTPUT_DELAY_SECONDS)
        cuts = self.cuts()
        await self.hear(VOICE, 0.3)
        self.conversation.say("They all pass.", None, DEFAULT_VOICE)
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.assertNotIn("They all pass.", self.phone.said())  # it waits while you're mid-message
        await self.hear(QUIET, 0.9)
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS)
        self.assertEqual(self.cuts(), cuts)
        order = self.order()
        self.assertEqual(order["submit"], "what you said")
        self.assertTrue(order["unheard"])
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.conversation.reply_turn, "t")
        self.assertIn("They all pass.", self.phone.said())

    async def test_a_hold_with_a_sentence_on_its_way_but_no_sound_resumes_at_once(self):
        self.choose()
        self.conversation.synthesizing = ("Next sentence.", None, DEFAULT_VOICE)
        self.conversation.phone_message({"t": "hold", "id": 16})
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 16, "leak": False}])
        self.assertIsNone(self.conversation.hold)

    async def test_adding_to_your_message_before_its_reply_sounds_stops_nothing(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        self.conversation.synthesizing = ("Next sentence.", None, DEFAULT_VOICE)
        cuts = self.cuts()
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        await self.hear(QUIET, 0.8)
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(self.order()["submit"], "what you said")
        self.assertEqual(self.conversation.reply_turn, "t")

    async def test_talking_while_a_turn_works_in_silence_stops_nothing(self):
        self.messages_end_quickly()
        self.choose()
        await self.hear(QUIET, 1.1)
        self.event(kind="turn-start", turnId="t")
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        await self.hear(QUIET, 0.8)
        self.assertEqual(self.order()["submit"], "what you said")
        self.assertEqual(self.conversation.reply_turn, "t")

    # --- the voice ---------------------------------------------------------------

    def voices(self):
        return self.controls("voices")

    async def test_the_phone_gets_the_voices_when_it_connects(self):
        self.conversation.phone_connected()
        message = self.voices()[-1]
        self.assertEqual(message["chosen"], DEFAULT_VOICE)
        self.assertEqual([v["id"] for v in message["list"]], [name for name, _ in VOICES])

    async def test_a_new_voice_starts_with_the_next_reply(self):
        self.choose()
        self.event(kind="turn-start", turnId="t")
        self.event(kind="text", turnId="t", text="Before the change. ")
        self.conversation.phone_message({"t": "voice", "id": "bm_george"})
        self.event(kind="text", turnId="t", text="Still this reply. More.")
        self.event(kind="turn-end", turnId="t", reason="answer")
        await self.settle()
        self.event(kind="turn-start", turnId="u")
        self.event(kind="text", turnId="u", text="The next reply. More.")
        await self.settle()
        self.assertEqual(self.conversation.speech.voices, [DEFAULT_VOICE] * 3 + ["bm_george"])
        self.assertEqual(self.voices()[-1]["chosen"], "bm_george")

    async def test_a_voice_the_model_lacks_is_refused(self):
        self.conversation.phone_message({"t": "voice", "id": "../../etc/passwd"})
        self.assertEqual(self.conversation.voice, DEFAULT_VOICE)

    async def test_the_choice_of_voice_survives_a_restart_in_a_private_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "voice")

            def make():
                return Conversation(FakeSpeech(), bool, FakeDecoder, FakeEncoder(), voice_file=path)

            first = make()
            first.phone = self.phone
            first.phone_message({"t": "voice", "id": "af_nicole"})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(make().voice, "af_nicole")
            with open(path, "w") as f:
                f.write("nobody")
            self.assertEqual(make().voice, DEFAULT_VOICE)

    def mic(self, samples, seconds):
        for _ in range(int(seconds * 16000 / WINDOW)):
            self.conversation.mic_packet(samples.tobytes())

    async def test_talking_over_the_end_of_a_reply_warns_and_sends_nothing(self):
        self.choose()
        loud = np.array(([3000] * 8 + [-3000] * 8) * (WINDOW // 16), dtype=np.int16)
        self.mic(np.zeros(WINDOW, dtype=np.int16), 1.5)
        self.conversation.say("A sentence.")
        await self.settle(0.05)
        self.mic(loud, 0.5)  # the reply is still playing
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + module.OUTPUT_DELAY_SECONDS)
        self.mic(loud, 1)
        self.assertEqual(self.tones(), ["warn"])
        self.assertFalse(self.conversation.detector.talking)
        self.assertEqual(len(self.session.orders), 0)

    # --- pausing -------------------------------------------------------------------

    def say_over_the_reply(self, text, loud=True):
        """What the transcriber makes of the words, and the hold the phone sends for them."""
        self.conversation.speech.transcribe = lambda audio: text
        if not loud:
            self.conversation.owner_level = turns.loudness(np.frombuffer(VOICE, dtype=np.int16)) + 20
        self.conversation.phone_message({"t": "hold", "id": 50})

    async def short_word_over_the_reply(self, text, loud=True):
        self.say_over_the_reply(text, loud)
        await self.hear(VOICE, 0.3)
        await self.hear(QUIET, module.OUTPUT_DELAY_SECONDS + module.HOLD_WAIT_SECONDS)

    async def test_a_lone_pause_stops_listening_and_never_reaches_the_session(self):
        self.choose()
        for text in ("Pause.", "pause", "Pause it."):
            with self.subTest(text=text):
                self.conversation.paused = False
                self.finish_turn(text)
                self.assertTrue(self.conversation.paused)
                self.assertEqual(len(self.session.orders), 0)
        await self.settle()
        self.assertEqual(self.phone.said(), [module.PAUSED])

    async def test_a_prompt_with_the_word_pause_in_it_goes_to_the_session(self):
        self.choose()
        self.finish_turn("Pause the build.")
        self.assertFalse(self.conversation.paused)
        self.assertEqual(self.order()["submit"], "Pause the build.")

    async def test_pause_needs_no_session(self):
        self.finish_turn("Pause.")
        self.assertTrue(self.conversation.paused)
        await self.settle()
        self.assertEqual(self.phone.said(), [module.PAUSED])
        self.assertEqual(self.tones(), [])

    async def test_nothing_heard_while_paused_reaches_the_session(self):
        self.choose()
        self.finish_turn("Pause.")
        self.conversation.typing_offer = ("Fix the test.", self.conversation.clock() + 60)
        level = self.conversation.owner_level
        for text in ("Nice to meet you.", "Cancel.", "Type it.", "Let's go."):
            self.conversation.detector.finished_level = -3.0
            self.finish_turn(text)
        await self.settle()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said(), [module.PAUSED])
        self.assertEqual(self.conversation.owner_level, level)
        self.assertTrue(self.conversation.paused)

    async def test_each_resume_word_starts_listening_and_is_never_a_prompt(self):
        self.choose()
        for text in ("Go.", "Play.", "Start."):
            with self.subTest(text=text):
                self.phone.speech.clear()
                self.finish_turn("Pause.")
                self.finish_turn(text)
                self.assertFalse(self.conversation.paused)
                self.assertEqual(len(self.session.orders), 0)
                await self.settle()
                self.assertIn(module.LISTENING, self.phone.said())
        self.finish_turn("Fix the failing test.")
        self.assertEqual(self.order()["submit"], "Fix the failing test.")

    async def test_the_resume_words_mean_nothing_special_unless_paused(self):
        self.choose()
        for text in ("Go.", "Play.", "Start."):
            self.finish_turn(text)
            self.assertEqual(self.order()["submit"], text)
        await self.settle()
        self.assertEqual(self.phone.said(), [])

    async def test_a_tap_still_stops_the_turn_while_paused_and_does_not_resume(self):
        self.choose()
        self.event(kind="turn-start", turnId="t")
        self.finish_turn("Pause.")
        self.conversation.phone_message({"t": "tap", "gen": 0})
        self.assertEqual(self.order()["abort"], "t")
        self.assertTrue(self.conversation.paused)

    async def test_a_running_turn_keeps_running_and_its_reply_plays_while_paused(self):
        self.choose()
        self.event(kind="turn-start", turnId="t")
        self.finish_turn("Pause.")
        self.event(kind="text", turnId="t", text="The tests pass on main. ")
        self.event(kind="turn-end", turnId="t", reason="end")
        await self.settle(SPOKEN_SECONDS * 2 + module.LEAD_SECONDS + module.OUTPUT_DELAY_SECONDS + 0.2)
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said(), [module.PAUSED, "The tests pass on main."])

    async def test_the_confirmation_goes_ahead_of_a_waiting_reply(self):
        await self.reply_sounding()
        self.conversation.say("The next sentence.", None, DEFAULT_VOICE)
        await self.settle(0.1)
        self.finish_turn("Pause.")
        await self.settle(module.LEAD_SECONDS + module.OUTPUT_DELAY_SECONDS + SPOKEN_SECONDS + 0.3)
        said = self.phone.said()
        self.assertIn(module.PAUSED, said)
        self.assertNotIn("The next sentence.", said)
        self.assertGreater(len(self.conversation.frames), 100)  # the reply is still there, and it carries on

    async def test_a_lone_pause_over_a_reply_pauses_and_the_reply_carries_on(self):
        await self.reply_sounding()
        cuts = self.cuts()
        await self.short_word_over_the_reply("Pause.")
        self.assertTrue(self.conversation.paused)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 50, "leak": False}])
        self.assertEqual(self.controls("leak"), [])
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.conversation.reply_turn, "t")

    async def test_a_slow_pause_over_a_reply_stops_no_turn(self):
        await self.reply_sounding()
        cuts = self.cuts()
        self.say_over_the_reply("Pause.")
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.assertEqual(self.cuts(), cuts)
        await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.2)
        self.assertTrue(self.conversation.paused)
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(len(self.session.orders), 0)

    async def test_speech_that_carries_on_past_a_pause_is_a_talk_over_as_ever(self):
        await self.reply_sounding()
        self.say_over_the_reply("Pause.")
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 0.4)
        self.conversation.speech.transcribe = lambda audio: "Pause the build."
        await self.hear(VOICE, 0.4)
        self.assertEqual(self.order()["abort"], "t")
        await self.hear(QUIET, 0.8)
        self.assertEqual(self.order()["submit"], "Pause the build.")
        self.assertFalse(self.conversation.paused)

    async def test_a_word_said_as_a_hold_settles_then_carried_on_from_isnt_lone(self):
        await self.reply_sounding()
        turns.END_HOLD, turns.THINKING_HOLD = 30, 30
        self.say_over_the_reply("Pause.")
        await self.hear(VOICE, 0.3)
        real = self.conversation.speech.transcribe

        def slowly(audio):
            threading.Event().wait(0.5)
            return real(audio)
        self.conversation.speech.transcribe = slowly
        await self.hear(QUIET, module.OUTPUT_DELAY_SECONDS + module.HOLD_WAIT_SECONDS - 0.3)
        await self.hear(VOICE, 0.6)  # they carried on while it was being read
        self.assertFalse(self.conversation.paused)
        self.assertIsNotNone(self.conversation.hold)

    async def test_the_reply_saying_the_word_does_not_pause(self):
        await self.reply_played("I will pause here")
        await self.short_word_over_the_reply("Pause.")
        self.assertFalse(self.conversation.paused)
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 50, "leak": False}])

    async def test_the_reply_saying_pause_is_not_saved_by_the_transcriber_adding_it(self):
        await self.reply_played("I will pause here")
        await self.short_word_over_the_reply("Pause it.")
        self.assertFalse(self.conversation.paused)

    async def test_a_quieter_voice_saying_pause_over_a_reply_does_not_pause(self):
        await self.reply_sounding()
        turns.END_HOLD, turns.THINKING_HOLD = 30, 30
        calls = []
        self.conversation.owner_level = turns.loudness(np.frombuffer(VOICE, dtype=np.int16)) + 20
        self.conversation.speech.transcribe = lambda audio: calls.append(1) or "Pause."
        self.conversation.phone_message({"t": "hold", "id": 50})
        await self.hear(VOICE, 0.3)
        await self.hear(QUIET, module.OUTPUT_DELAY_SECONDS + module.HOLD_WAIT_SECONDS)
        self.assertFalse(self.conversation.paused)
        self.assertEqual(calls, [])  # never read
        self.assertEqual([c["id"] for c in self.controls("resume")], [50])

    async def test_another_short_word_over_a_reply_still_just_resumes(self):
        await self.reply_sounding()
        await self.short_word_over_the_reply("Stop.")
        self.assertFalse(self.conversation.paused)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 50, "leak": False}])
        self.assertEqual(len(self.session.orders), 0)

    async def test_a_resume_word_over_a_reply_starts_listening_only_while_paused(self):
        await self.reply_sounding()
        await self.short_word_over_the_reply("Go.")
        self.assertFalse(self.conversation.paused)
        self.conversation.paused = True
        self.phone.controls.clear()
        await self.short_word_over_the_reply("Go.")
        self.assertFalse(self.conversation.paused)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 50, "leak": False}])
        self.assertEqual(self.controls("leak"), [])
        self.assertEqual(len(self.session.orders), 0)

    async def test_the_reply_saying_go_does_not_start_listening(self):
        await self.reply_played("You can go")
        self.conversation.paused = True
        await self.short_word_over_the_reply("Go.")
        self.assertTrue(self.conversation.paused)

    async def test_talking_over_a_reply_while_paused_stops_nothing_and_teaches_nothing(self):
        await self.reply_sounding()
        self.conversation.paused = True
        cuts = self.cuts()
        self.say_over_the_reply("Nice to meet you, what brings you here today?")
        await self.hear(VOICE, module.OUTPUT_DELAY_SECONDS + 1)
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(self.controls("resume"), [])  # the reply waits until they've stopped
        await self.hear(QUIET, module.HOLD_WAIT_SECONDS + 0.2)
        self.assertEqual(self.cuts(), cuts)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 50, "leak": False}])
        self.assertEqual(self.controls("leak"), [])
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.conversation.reply_turn, "t")

    async def test_a_short_sound_while_paused_teaches_the_phone_nothing(self):
        await self.reply_sounding()
        self.conversation.paused = True
        await self.short_word_over_the_reply("The log.", loud=False)
        self.assertEqual(self.controls("resume"), [{"t": "resume", "id": 50, "leak": False}])

    async def test_a_gap_in_the_mic_while_paused_says_nothing(self):
        self.choose()
        self.mic(np.zeros(WINDOW, dtype=np.int16), 1.5)
        self.conversation.paused = True
        self.mic(np.array(([3000] * 8 + [-3000] * 8) * (WINDOW // 16), dtype=np.int16), 1)
        self.assertTrue(self.conversation.detector.talking)
        self.conversation.mic_gap()
        await self.settle()
        self.assertEqual(self.phone.said(), [])

    async def test_talking_over_the_end_of_a_reply_while_paused_gives_no_warning(self):
        self.choose()
        loud = np.array(([3000] * 8 + [-3000] * 8) * (WINDOW // 16), dtype=np.int16)
        self.mic(np.zeros(WINDOW, dtype=np.int16), 1.5)
        self.conversation.paused = True
        self.conversation.say("A sentence.")
        await self.settle(0.05)
        self.mic(loud, 0.5)
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + module.OUTPUT_DELAY_SECONDS)
        self.mic(loud, 1)
        self.assertEqual(self.tones(), [])

    async def test_a_pause_survives_a_restart_and_a_new_run_says_so(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "paused")

            def make():
                made = Conversation(FakeSpeech(), bool, FakeDecoder, FakeEncoder(), pause_file=path)
                made.phone, made.modlink = FakePhone(), FakeLink()
                return made

            first = make()
            self.assertFalse(first.paused)
            first._turn_finished("Pause.", 0)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            second = make()
            self.assertTrue(second.paused)
            second.phone_new_run()
            self.assertEqual([unit[0] for unit in second.say_queue], [module.STILL_PAUSED])
            second._turn_finished("Nice to meet you.", 0)
            self.assertEqual(len(second.modlink.sessions["7"].orders), 0)
            second._turn_finished("Go.", 0)
            self.assertFalse(os.path.exists(path))
            self.assertFalse(make().paused)

    def heard(self, gen=None):
        """Each sentence the phone was sent, in the order it plays, as many times as it plays."""
        packets = [packet for sent_gen, packet in self.phone.speech if gen is None or sent_gen == gen]
        return [packet.rsplit(":", 1)[0] for packet in packets if packet.endswith(":0")]

    def whole(self, text, gen):
        frames = [packet for sent_gen, packet in self.phone.speech if sent_gen == gen and packet.startswith(text + ":")]
        return frames == [packet.decode() for packet in FakeEncoder().encode(text)]

    def sound_due(self, seconds):
        """A reply just went out, so a notice has to wait for it, as it does after any reply."""
        self.conversation.reply_heard = True
        self.conversation.played_until = self.conversation.clock() + seconds

    async def test_only_the_current_state_is_heard_if_an_earlier_notice_has_not_gone_out(self):
        played = 0.3 + module.OUTPUT_DELAY_SECONDS + SPOKEN_SECONDS + module.LEAD_SECONDS
        self.choose()
        for window, wait in (("in the queue", None), ("in frames", 0.15)):
            with self.subTest(window=window):
                self.phone.speech.clear()
                self.sound_due(0.3)
                self.finish_turn("Pause.")
                if wait is not None:
                    await asyncio.sleep(wait)
                if window == "in frames":
                    self.assertEqual(self.phone.speech, [])
                    self.assertTrue(self.conversation.frames)
                self.finish_turn("Go.")
                await self.settle(played)
                self.assertFalse(self.conversation.paused)
                self.assertEqual(self.heard(), [module.LISTENING])

    async def test_only_the_current_state_is_heard_if_an_earlier_notice_is_being_made(self):
        release = threading.Event()
        self.addCleanup(release.set)
        synthesize = self.conversation.speech.synthesize
        self.conversation.speech.synthesize = lambda text, voice: (release.wait(), synthesize(text, voice))[1]
        self.choose()
        self.finish_turn("Pause.")
        await self.settle(0.05)
        self.assertEqual(self.conversation.synthesizing[0], module.PAUSED)
        self.finish_turn("Go.")
        release.set()
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + 0.3)
        self.assertEqual(self.heard(), [module.LISTENING])

    async def test_the_rest_of_a_state_notice_stops_when_the_state_changes(self):
        self.choose()
        self.finish_turn("Pause.")
        await self.settle(0.1)
        self.conversation.frames.extend((b"%s:%d" % (module.PAUSED.encode(), i), None, False, [], None, True) for i in range(100, 150))
        self.conversation.frames_ready.set()
        self.finish_turn("Go.")
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + 0.3)
        self.assertEqual([packet for _, packet in self.phone.speech if packet.endswith(":149")], [])
        self.assertEqual(self.phone.said()[-1], module.LISTENING)

    async def test_a_new_state_notice_keeps_the_waiting_reply_behind_it(self):
        self.choose()
        self.sound_due(0.8)  # far enough ahead that more of the reply isn't due yet
        self.conversation.say("The next sentence.", None, DEFAULT_VOICE)
        await self.settle(0.1)
        self.finish_turn("Pause.")
        await self.settle(0.1)
        self.finish_turn("Go.")
        await self.settle(0.8 + 2 * module.OUTPUT_DELAY_SECONDS + 2 * SPOKEN_SECONDS + module.LEAD_SECONDS)
        self.assertEqual(self.heard(), [module.LISTENING, "The next sentence."])

    async def test_a_reply_with_the_words_of_a_state_notice_replaces_nothing(self):
        self.choose()
        self.finish_turn("Pause.")
        self.conversation.say(module.LISTENING, None, DEFAULT_VOICE)
        await self.settle(module.OUTPUT_DELAY_SECONDS + 2 * SPOKEN_SECONDS + module.LEAD_SECONDS + 0.2)
        self.assertEqual(self.heard(), [module.PAUSED, module.LISTENING])
        self.assertTrue(self.conversation.paused)

    async def test_choosing_a_session_again_after_a_new_run_still_says_the_whole_notice(self):
        for window, wait in (("not yet sent", None), ("partly played", 0.08)):
            with self.subTest(window=window):
                self.conversation.phone_message({"t": "release"})
                self.conversation.paused = True
                self.phone.speech.clear()
                self.conversation.phone_new_run()
                self.conversation.phone_connected()
                if wait is not None:
                    await self.settle(wait)
                self.choose()
                await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + 0.2)
                self.assertEqual(self.heard(self.conversation.gen), [module.STILL_PAUSED])
                self.assertTrue(self.whole(module.STILL_PAUSED, self.conversation.gen))

    async def test_choosing_a_session_keeps_a_confirmation_that_has_not_finished(self):
        self.choose()
        self.finish_turn("Pause.")
        await self.settle(0.08)
        self.conversation.phone_message({"t": "select", "session": "8"})
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + 0.2)
        self.assertTrue(self.whole(module.PAUSED, self.conversation.gen))

    async def test_choosing_a_session_does_not_repeat_a_notice_that_finished(self):
        self.conversation.paused = True
        self.conversation.phone_new_run()
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + module.OUTPUT_DELAY_SECONDS + 0.2)
        self.assertEqual(self.heard(), [module.STILL_PAUSED])
        self.choose()
        await self.settle()
        self.assertEqual(self.heard(self.conversation.gen), [])

    async def test_a_tap_silences_a_state_notice_and_choosing_a_session_does_not_bring_it_back(self):
        self.conversation.paused = True
        self.conversation.phone_new_run()
        await self.settle(0.08)
        self.conversation.phone_message({"t": "tap", "gen": 0})
        self.choose()
        await self.settle()
        self.assertEqual(self.heard(self.conversation.gen), [])
        self.assertTrue(self.conversation.paused)

    async def test_a_new_run_with_nothing_paused_says_nothing(self):
        self.conversation.phone_new_run()
        await self.settle()
        self.assertEqual(self.phone.said(), [])

    # --- audio that went missing --------------------------------------------------

    async def test_a_gap_in_the_mic_throws_away_the_message_in_progress(self):
        # What the adversarial review heard: "Do not run anything yet" fell in the gap, and the rest joined up.
        self.choose()
        loud = np.array(([3000] * 8 + [-3000] * 8) * (WINDOW // 16), dtype=np.int16)
        quiet = np.zeros(WINDOW, dtype=np.int16)
        self.mic(quiet, 1.5)
        self.mic(loud, 1)
        self.assertTrue(self.conversation.detector.talking)
        self.conversation.mic_gap()
        self.assertFalse(self.conversation.detector.talking)
        self.mic(loud, 1)  # what's left in the phone's buffer starts in the middle of a sentence
        self.assertFalse(self.conversation.detector.talking)
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + module.OUTPUT_DELAY_SECONDS)  # the notice plays
        self.mic(loud, 1)  # still talking as the microphone opens again: one warning is enough
        self.mic(quiet, 3)
        await self.settle()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.tones(), ["warn"])
        self.assertEqual(self.phone.said(), [module.LOST_PART])

    async def test_a_gap_with_nobody_talking_says_nothing(self):
        self.choose()
        self.conversation.mic_gap()
        self.mic(np.zeros(WINDOW, dtype=np.int16), 1)
        await self.settle()
        self.assertEqual(self.tones(), [])
        self.assertEqual(self.phone.said(), [])

    async def test_a_reply_waits_while_you_are_mid_sentence(self):
        self.choose()
        self.conversation.detector.talking = True
        self.conversation.last_mic = self.conversation.clock()
        self.conversation.say("A sentence.")
        await self.settle(0.05)
        self.conversation.last_mic = self.conversation.clock()
        await self.settle(0.05)
        self.assertEqual(self.phone.speech, [])
        self.conversation.detector.talking = False
        await self.settle()
        self.assertEqual(self.phone.said(), ["A sentence."])

    async def test_a_reply_does_not_wait_for_a_mic_that_stopped(self):
        self.choose()
        self.conversation.detector.talking = True
        self.conversation.last_mic = self.conversation.clock() - module.MIC_STALE_SECONDS - 1
        self.conversation.say("A sentence.")
        await self.settle()
        self.assertEqual(self.phone.said(), ["A sentence."])

    async def test_a_stall_in_the_mic_keeps_the_words_so_far(self):
        self.choose()
        self.conversation.detector.talking = True
        self.conversation.detector.audio = [np.zeros(WINDOW, dtype=np.int16)]
        self.conversation.last_mic = self.conversation.clock() - module.MIC_STALE_SECONDS - 1
        self.conversation.say("A sentence.")
        await self.settle()
        self.assertTrue(self.conversation.detector.talking)
        self.assertEqual(len(self.conversation.detector.audio), 1)

    async def test_a_short_stall_in_the_mic_still_holds_a_reply_back(self):
        self.choose()
        self.conversation.detector.talking = True
        self.conversation.last_mic = self.conversation.clock() - 2
        self.conversation.say("A sentence.")
        await self.settle()
        self.assertEqual(self.phone.speech, [])

    # --- a session stopped at the keyboard ---------------------------------------

    async def test_a_refused_prompt_is_explained_then_typed_on_request(self):
        self.choose()
        self.finish_turn("Fix the failing test.")
        order = self.order()
        self.event(kind="order", id=order["id"], drop="the session was stopped and nobody has sent input since")
        await self.settle()
        self.assertEqual(self.phone.said(), [STOPPED_AT_KEYBOARD])
        self.finish_turn("Type it.")
        self.assertEqual(self.order()["type"], "Fix the failing test.")

    async def test_nothing_is_typed_without_a_refused_prompt_waiting(self):
        self.choose()
        self.finish_turn("Type it.")
        await self.settle()
        self.assertEqual(len(self.session.orders), 0)
        self.assertEqual(self.phone.said(), [NOTHING_TO_TYPE])

    async def test_the_offer_to_type_runs_out(self):
        self.choose()
        self.finish_turn("Fix it.")
        self.event(kind="order", id=self.order()["id"], drop="stopped")
        self.conversation.typing_offer = (self.conversation.typing_offer[0], self.conversation.clock() - 1)
        self.finish_turn("Type it.")
        self.assertEqual(len(self.session.orders), 0)

    async def test_the_offer_to_type_works_once(self):
        self.choose()
        self.finish_turn("Fix it.")
        self.event(kind="order", id=self.order()["id"], drop="stopped")
        self.finish_turn("Type it.")
        self.order()
        self.finish_turn("Type it.")
        self.assertEqual(len(self.session.orders), 0)

    async def test_an_open_dialog_is_reported(self):
        self.choose()
        self.finish_turn("Fix it.")
        self.event(kind="order", id=self.order()["id"], drop="stopped")
        self.finish_turn("Type it.")
        self.event(kind="order", id=self.order()["id"], refused="dialog")
        await self.settle()
        self.assertIn(DIALOG_OPEN, self.phone.said())

    async def test_a_failed_submit_is_reported(self):
        self.choose()
        self.finish_turn("Fix it.")
        self.event(kind="order", id=self.order()["id"], error="no")
        await self.settle()
        self.assertEqual(self.phone.said(), [NOT_SENT])

    async def test_a_prompt_nobody_picked_up_is_reported(self):
        original = module.ORDER_WAIT_SECONDS
        module.ORDER_WAIT_SECONDS = 0.01
        try:
            self.choose()
            self.finish_turn("Fix it.")
            await self.settle()
            self.assertEqual(self.phone.said(), [module.NOT_REACHED])
        finally:
            module.ORDER_WAIT_SECONDS = original

    async def test_a_prompt_the_session_holds_but_has_not_started_is_reported(self):
        # A dialog at the keyboard. What the adversarial review heard: a tone, then nothing for as long as it stayed open.
        original = module.ORDER_WAIT_SECONDS
        module.ORDER_WAIT_SECONDS = 0.05
        try:
            self.choose()
            self.finish_turn("Fix it.")
            self.event(kind="order", id=self.order()["id"], accepted=True, busy=False)
            await self.settle()
            said = self.phone.said()
            self.assertIn(said[0], module.HEARD_WORDS)
            self.assertEqual(said[1:], [module.NOT_STARTED])
        finally:
            module.ORDER_WAIT_SECONDS = original

    async def test_a_prompt_that_started_in_time_says_only_its_thinking_word(self):
        original = module.ORDER_WAIT_SECONDS
        module.ORDER_WAIT_SECONDS = 0.05
        try:
            self.choose()
            self.finish_turn("Fix it.")
            order_id = self.order()["id"]
            self.event(kind="order", id=order_id, accepted=True, busy=False)
            self.event(kind="turn-start", turnId="t1", order=order_id)
            await self.settle()
            said = self.phone.said()
            self.assertEqual(len(said), 1)
            self.assertIn(said[0], module.HEARD_WORDS)
        finally:
            module.ORDER_WAIT_SECONDS = original

    async def test_a_prompt_to_a_busy_session_is_reported_as_queued_and_nothing_else(self):
        original = module.ORDER_WAIT_SECONDS
        module.ORDER_WAIT_SECONDS = 0.05
        try:
            self.choose()
            self.finish_turn("Fix it.")
            self.event(kind="order", id=self.order()["id"], accepted=True, busy=True)
            await self.settle()
            self.assertEqual(self.phone.said(), [module.QUEUED])
        finally:
            module.ORDER_WAIT_SECONDS = original

    async def test_a_prompt_an_idle_session_took_gets_one_thinking_word(self):
        # The owner couldn't tell a prompt that arrived from one the network lost, until the reply came.
        self.choose()
        self.finish_turn("Fix it.")
        self.event(kind="order", id=self.order()["id"], accepted=True, busy=False)
        await self.settle()
        said = self.phone.said()
        self.assertEqual(len(said), 1)
        self.assertIn(said[0], module.HEARD_WORDS)
        self.assertEqual(self.tones(), [])

    async def test_the_thinking_word_comes_once_for_a_prompt(self):
        self.choose()
        self.finish_turn("Fix it.")
        order_id = self.order()["id"]
        self.event(kind="order", id=order_id, accepted=True, busy=False)
        self.event(kind="order", id=order_id, ok=True)
        await self.settle()
        self.assertEqual(len(self.phone.said()), 1)

    async def test_a_front_desk_that_took_a_prompt_gets_a_thinking_word(self):
        self.session.front_desk = True
        self.choose()
        self.finish_turn("Post the launch update.")
        self.event(kind="order", id=self.order()["id"], ok=True)
        await self.settle()
        said = self.phone.said()
        self.assertEqual(len(said), 1)
        self.assertIn(said[0], module.HEARD_WORDS)

    async def test_no_thinking_word_for_what_isnt_a_prompt(self):
        self.choose()
        self.conversation.phone_message({"t": "tap"})
        self.event(kind="order", id=self.order()["id"], accepted=True, busy=False)
        await self.settle()
        self.assertEqual(self.phone.said(), [])

    async def test_a_prompt_that_wasnt_delivered_gets_the_warning_tone(self):
        self.choose()
        self.finish_turn("Fix it.")
        self.event(kind="order", id=self.order()["id"], error="no")
        await self.settle()
        self.assertEqual(self.tones(), ["warn"])
        self.assertEqual(self.phone.said(), [NOT_SENT])

    async def test_a_prompt_nobody_picked_up_gets_the_warning_tone(self):
        with mock.patch.object(module, "ORDER_WAIT_SECONDS", 0.01):
            self.choose()
            self.finish_turn("Fix it.")
            await self.settle()
        self.assertEqual(self.tones(), ["warn"])
        self.assertEqual(self.phone.said(), [module.NOT_REACHED])

    async def test_a_prompt_refused_at_the_keyboard_gets_the_warning_tone(self):
        self.choose()
        self.finish_turn("Fix it.")
        self.event(kind="order", id=self.order()["id"], drop="stopped at the keyboard")
        await self.settle()
        self.assertEqual(self.tones(), ["warn"])

    async def test_no_heard_turns_off_the_word_and_the_tone(self):
        self.conversation.heard = False
        self.choose()
        self.finish_turn("Fix it.")
        self.event(kind="order", id=self.order()["id"], accepted=True, busy=False)
        self.finish_turn("Fix that too.")
        self.event(kind="order", id=self.order()["id"], error="no")
        await self.settle()
        self.assertEqual(self.phone.said(), [NOT_SENT])
        self.assertEqual(self.tones(), [])

    async def test_a_prompt_spoken_while_a_reply_is_on_its_way_is_marked_unheard(self):
        # The session is idle by then, so only the gateway knows the question hasn't been heard.
        self.choose()
        self.conversation.detector.talking = True
        self.conversation.last_mic = self.conversation.clock()
        self.conversation.say("Say yes to confirm.")
        await self.settle(0.05)
        self.conversation.detector.talking = False
        self.finish_turn("Yes, go ahead.")
        self.assertTrue(self.order()["unheard"])

    async def test_a_prompt_after_a_reply_has_played_is_not_marked(self):
        self.choose()
        self.conversation.say("Say yes to confirm.")
        await self.settle(SPOKEN_SECONDS + module.LEAD_SECONDS + module.OUTPUT_DELAY_SECONDS + 0.1)
        self.finish_turn("Yes, go ahead.")
        self.assertNotIn("unheard", self.order())

    async def test_a_queued_prompt_still_gets_its_result(self):
        self.choose()
        self.finish_turn("Fix it.")
        order_id = self.order()["id"]
        self.event(kind="order", id=order_id, accepted=True, busy=True)
        self.event(kind="order", id=order_id, drop="stopped at the keyboard")
        await self.settle()
        self.assertEqual(self.phone.said(), [module.QUEUED, STOPPED_AT_KEYBOARD])
        self.assertEqual(self.conversation.typing_offer[0], "Fix it.")

    async def test_a_tap_that_dropped_queued_prompts_says_so_once(self):
        self.choose()
        self.finish_turn("One.")
        first = self.order()["id"]
        self.finish_turn("Two.")
        second = self.order()["id"]
        self.conversation.phone_message({"t": "tap"})
        abort = self.order()["id"]
        self.event(kind="order", id=first, dropped=True)
        self.event(kind="order", id=second, dropped=True)
        self.event(kind="order", id=abort, ok=True, dropped=True)
        await self.settle()
        self.assertEqual(self.phone.said(), [module.QUEUE_DROPPED])
        self.assertEqual(self.conversation.orders, {})

    async def test_a_refused_prompt_says_the_ones_behind_it_were_dropped(self):
        # What the adversarial review heard: the same long notice twice, and no word of the prompt that was lost.
        self.choose()
        self.finish_turn("One.")
        first = self.order()["id"]
        self.event(kind="order", id=first, accepted=True, busy=True)
        self.finish_turn("Two.")
        second = self.order()["id"]
        self.event(kind="order", id=second, accepted=True, busy=True)
        self.event(kind="order", id=first, drop="stopped", later=1)
        self.event(kind="order", id=second, dropped=True)
        await self.settle(0.5)
        self.assertEqual(self.phone.said(), [module.QUEUED, STOPPED_AT_KEYBOARD, module.LATER_DROPPED])
        self.finish_turn("Type it.")
        self.assertEqual(self.order()["type"], "One.")
        self.assertEqual(self.conversation.prompt_metrics, {})

    async def test_order_ids_differ_from_an_earlier_gateways(self):
        other = Conversation(FakeSpeech(), lambda w: bool(w.any()), FakeDecoder, FakeEncoder())
        self.assertNotEqual(self.conversation.order_prefix, other.order_prefix)
        other.stop()

    # --- sessions ----------------------------------------------------------------

    async def test_a_chosen_session_that_ends_is_reported(self):
        self.choose()
        self.event(kind="bye")
        await self.settle()
        self.assertIsNone(self.conversation.chosen)
        self.assertEqual(self.phone.said(), [SESSION_GONE])
        self.assertNotIn("7", self.link.sessions)

    async def test_an_unchosen_session_that_ends_leaves_the_list(self):
        self.choose()
        self.conversation.mod_event(self.link.sessions["8"], {"kind": "bye"})
        self.assertNotIn("8", self.link.sessions)
        self.assertEqual(self.phone.controls[-1]["t"], "sessions")
        self.assertEqual([s["id"] for s in self.phone.controls[-1]["list"]], ["7"])
        self.assertEqual(self.conversation.chosen, "7")

    async def test_choosing_another_session_cuts_the_old_ones_speech(self):
        self.choose()
        self.conversation.frames.extend((b"old:%d" % i, None, False, [], None, False) for i in range(100))
        self.conversation.say("Old words.")
        gen_before = self.conversation.gen
        self.choose("8")
        self.assertEqual(self.conversation.gen, gen_before + 1)
        self.assertIn({"t": "cut", "gen": gen_before + 1}, self.phone.controls)
        self.assertIn({"t": "forgot"}, self.phone.controls)
        self.assertEqual(len(self.conversation.frames), 0)
        self.assertEqual(len(self.conversation.say_queue), 0)
        self.assertEqual(len(self.session.orders), 0)

    async def test_choosing_another_session_drops_the_old_ones_open_orders(self):
        original = module.ORDER_WAIT_SECONDS
        module.ORDER_WAIT_SECONDS = 0.01
        try:
            self.choose()
            self.finish_turn("Fix it.")
            self.choose("8")
            await self.settle()
            self.assertEqual(self.phone.said(), [])
        finally:
            module.ORDER_WAIT_SECONDS = original

    async def test_letting_a_session_go_takes_back_its_prompts_but_not_a_tap(self):
        # What the adversarial review saw: after Stop, the next poll still got the prompt, and it ran.
        self.choose()
        self.finish_turn("Delete the old release branch.")
        self.conversation.phone_message({"t": "tap"})
        self.finish_turn("Delete it anyway.")
        self.conversation.phone_message({"t": "release"})
        self.assertEqual([("abort" in order) for order in self.session.orders], [True])

    async def test_choosing_another_session_says_the_queue_was_dropped(self):
        self.choose()
        self.finish_turn("Fix it.")
        self.event(kind="order", id=self.order()["id"], accepted=True, busy=True)
        self.choose("8")
        await self.settle()
        self.assertEqual(self.phone.said(), [module.QUEUE_DROPPED])

    async def test_choosing_another_session_with_nothing_queued_says_nothing(self):
        self.choose()
        self.finish_turn("Fix it.")
        self.event(kind="order", id=self.order()["id"], accepted=True, busy=False)
        self.choose("8")
        await self.settle()
        self.assertEqual(self.phone.said(), [])

    async def test_choosing_the_same_session_again_keeps_its_reply(self):
        self.choose()
        self.event(kind="turn-start", turnId="t")
        self.choose()
        self.event(kind="text", turnId="t", text="Still here. More.")
        await self.settle()
        self.assertEqual(self.phone.said(), ["Still here."])

    async def test_the_phone_stopping_releases_the_session(self):
        self.choose()
        self.event(kind="turn-start", turnId="t")
        self.conversation.say("Old words.")
        self.conversation.phone_message({"t": "release"})
        self.assertIsNone(self.conversation.chosen)
        self.assertFalse(self.conversation.is_chosen(self.session))
        self.assertIsNone(self.phone.controls[-1]["chosen"])
        self.assertEqual({s["id"] for s in self.phone.controls[-1]["list"]}, {"7", "8"})
        self.event(kind="text", turnId="t", text="Late words. More.")
        await self.settle()
        self.assertEqual(self.phone.said(), [])

    async def test_a_new_phone_run_starts_with_no_session_and_no_old_speech(self):
        self.choose()
        self.conversation.say("Old words.")
        self.conversation.phone_new_run()
        await self.settle()
        self.assertIsNone(self.conversation.chosen)
        self.assertEqual(self.phone.said(), [])

    async def test_a_disconnect_keeps_the_choice(self):
        self.choose()
        self.conversation.phone_connected()
        self.assertEqual(self.conversation.chosen, "7")
        self.assertEqual(self.phone.controls[-1]["chosen"], "7")

    async def test_the_phone_gets_the_session_list(self):
        self.conversation.phone_connected()
        message = self.phone.controls[-1]
        self.assertEqual(message["t"], "sessions")
        self.assertEqual({s["id"] for s in message["list"]}, {"7", "8"})
        self.assertIsNone(message["chosen"])

    # --- what's logged -----------------------------------------------------------

    def timing_lines(self, logs):
        return [json.loads(line.split("turn-timing ", 1)[1]) for line in logs.output if "turn-timing" in line]

    async def test_a_later_prompt_does_not_take_a_replys_timings(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.choose()
            self.finish_turn("First.")
            first = self.order()["id"]
            self.event(kind="turn-start", turnId="t1", order=first)
            self.finish_turn("Second.")  # spoken before the first one's reply has any audio
            self.event(kind="text", turnId="t1", text="The first reply. More.")
            await self.settle()
            self.conversation.phone_message({"t": "played", "rtt": 0})
        line = self.timing_lines(logs)[0]
        self.assertIn("turn_start", line)
        self.assertIn("first_text", line)
        self.assertIn("first_audio_played", line)

    async def test_a_notice_does_not_count_as_a_replys_first_audio(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.choose()
            self.finish_turn("A message.")
            order = self.order()["id"]
            self.event(kind="order", id=order, accepted=True, busy=True)
            await self.settle()
            self.conversation.phone_message({"t": "played", "rtt": 0})  # that sound was the notice
            self.assertEqual(self.timing_lines(logs), [])
            self.event(kind="turn-start", turnId="t", order=order)
            self.event(kind="text", turnId="t", text="The reply. More.")
            await self.settle(module.OUTPUT_DELAY_SECONDS + 0.3)
            self.conversation.phone_message({"t": "played", "rtt": 0})
        line = self.timing_lines(logs)[0]
        self.assertEqual(line["queued"], 1)
        self.assertIn("first_audio_played", line)

    async def test_a_refused_prompt_still_gets_a_line_in_the_log(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.choose()
            self.finish_turn("A message.")
            self.event(kind="order", id=self.order()["id"], drop="stopped")
            await self.settle()
        self.assertEqual(self.timing_lines(logs)[0]["refused"], 1)
        self.assertTrue(any("notice stopped-at-keyboard" in line for line in logs.output))

    async def test_a_dropped_prompt_still_gets_a_line_in_the_log(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.choose()
            self.finish_turn("A message.")
            self.event(kind="order", id=self.order()["id"], dropped=True)
        self.assertEqual(self.timing_lines(logs)[0]["dropped"], 1)

    async def test_prompts_still_waiting_get_a_line_when_the_session_is_let_go(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.choose()
            self.finish_turn("A message.")
            self.conversation.phone_message({"t": "release"})
        self.assertEqual(len(self.timing_lines(logs)), 1)

    async def test_a_tap_before_the_first_audio_keeps_the_replys_line(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.choose()
            self.finish_turn("A message.")
            self.event(kind="turn-start", turnId="t", order=self.order()["id"])
            self.conversation.phone_message({"t": "tap", "gen": 0})
            self.event(kind="turn-end", turnId="t", reason="answer")
            self.finish_turn("The next message.")
            self.event(kind="turn-start", turnId="t2", order=self.order_after_abort()["id"])
        lines = self.timing_lines(logs)
        self.assertEqual(len(lines), 1)
        self.assertIn("turn_start", lines[0])

    def order_after_abort(self):
        self.assertIn("abort", self.order())
        return self.order()

    async def test_a_prompt_a_tap_stopped_gets_one_line_in_the_log(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.choose()
            self.finish_turn("A message.")
            order = self.order()["id"]
            self.conversation.phone_message({"t": "tap", "gen": 0})
            self.event(kind="order", id=order, ok=True)  # it stops as it starts, so no turn-start follows
        lines = self.timing_lines(logs)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["dropped"], 1)

    async def test_a_prompt_dropped_by_a_tap_gets_only_one_line_in_the_log(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.choose()
            self.finish_turn("A message.")
            order = self.order()["id"]
            self.conversation.phone_message({"t": "tap", "gen": 0})
            self.event(kind="order", id=order, dropped=True)
        self.assertEqual(len(self.timing_lines(logs)), 1)

    async def test_a_reply_cut_short_by_releasing_the_session_keeps_its_line(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.choose()
            self.finish_turn("A message.")
            self.event(kind="turn-start", turnId="t", order=self.order()["id"])
            self.conversation.phone_message({"t": "release"})
        self.assertIn("turn_start", self.timing_lines(logs)[0])

    async def test_a_reply_whose_session_ended_keeps_its_line(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.choose()
            self.finish_turn("A message.")
            self.event(kind="turn-start", turnId="t", order=self.order()["id"])
            self.event(kind="bye")
        self.assertIn("turn_start", self.timing_lines(logs)[0])

    async def test_a_reply_whose_turn_end_never_came_keeps_its_line(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.choose()
            self.finish_turn("A message.")
            self.event(kind="turn-start", turnId="t", order=self.order()["id"])
            self.event(kind="turn-start", turnId="t2")  # after a /clear, say, which ends no turn
        self.assertIn("turn_start", self.timing_lines(logs)[0])

    async def test_notices_are_logged_by_name_without_their_words(self):
        with self.assertLogs("walkie.conversation", level=logging.INFO) as logs:
            self.finish_turn("A message.")
        self.assertTrue(any("notice no-session" in line for line in logs.output))
        self.assertFalse(any(NO_SESSION in line for line in logs.output))

    async def test_no_words_reach_the_log(self):
        with self.assertLogs(level=logging.INFO) as logs:
            logging.getLogger("walkie.test").info("start")
            self.choose()
            self.finish_turn("Private words from the owner.")
            order = self.order()
            self.event(kind="order", id=order["id"], ok=True)
            self.event(kind="turn-start", turnId="t", order=order["id"])
            self.event(kind="text", turnId="t", text="Private reply text.")
            self.event(kind="turn-end", turnId="t", reason="answer")
            await self.settle()
            self.conversation.phone_message({"t": "played", "rtt": 10})
        output = "\n".join(logs.output)
        self.assertIn("turn-timing", output)
        self.assertNotIn("Private", output)


if __name__ == "__main__":
    unittest.main()
