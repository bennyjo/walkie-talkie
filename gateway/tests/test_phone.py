"""The phone's connection, over a real socket on loopback, against a stand-in conversation."""
import asyncio
import base64
import json
import os
import tempfile
import unittest

from walkie_gateway import phone as phone_module
from walkie_gateway import security
from walkie_gateway.phone import (
    CONTROL,
    HEADER,
    MIC,
    MIC_HEADER,
    SPEECH,
    SPEECH_HEADER,
    PhoneServer,
    control,
    frame,
)

from .keys import KEY, PhoneKey


class Recorder:
    """A stand-in for the conversation that keeps what it's given."""

    def __init__(self):
        self.packets = []
        self.messages = []
        self.new_runs = 0
        self.connections = 0
        self.grants = []
        self.startable = True

    def mic_packet(self, packet):
        self.packets.append(packet)

    def mic_gap(self):
        self.packets.append("gap")

    def phone_message(self, message):
        self.messages.append(message)

    def phone_new_run(self):
        self.new_runs += 1

    def phone_connected(self):
        self.connections += 1

    def start_problem(self, instance, mod_nonce):
        if not self.startable:
            return "gone"
        return None if mod_nonce == "mod-nonce" else "changed"

    def phone_approved(self, grant):
        self.grants.append(grant)


async def read_frame(reader):
    length, kind = HEADER.unpack(await reader.readexactly(HEADER.size))
    return kind, await reader.readexactly(length)


class Phone:
    """The phone's side of the protocol, written out here so the test states it."""

    def __init__(self, port, token="", run="run-1", down=0):
        self.port, self.token, self.run, self.down = port, token, run, down

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            kind, payload = await read_frame(self.reader)
        except asyncio.IncompleteReadError:
            self.writer.close()
            raise
        self.server_nonce = json.loads(payload)["nonce"]
        return self

    async def login(self, mac=None, approve=None):
        client_nonce = security.new_nonce()
        auth = {
            "t": "auth", "nonce": client_nonce, "run": self.run, "down": self.down,
            "mac": mac or security.phone_mac(self.token, self.server_nonce, client_nonce),
        }
        if approve is not None:
            auth["approve"] = approve
        self.writer.write(control(auth))
        await self.writer.drain()
        kind, payload = await read_frame(self.reader)
        self.answer = json.loads(payload)
        self.expected_proof = security.gateway_mac(self.token, self.server_nonce, client_nonce)
        return self.answer

    async def ask_nonce(self):
        """What the app does as Start is tapped, before the fingerprint."""
        self.writer.write(control({"t": "nonce"}))
        await self.writer.drain()
        while True:
            kind, payload = await asyncio.wait_for(read_frame(self.reader), 2)
            message = json.loads(payload)
            if message["t"] == "nonce":
                return message["nonce"]

    def approval(self, nonce, key=KEY, session="7", mod_nonce="mod-nonce", run=None):
        message = security.start_message(nonce, run or self.run, session, mod_nonce)
        return {"session": session, "modNonce": mod_nonce, "nonce": nonce, "sig": key.sign(message)}

    def send_mic(self, seq, packet):
        self.writer.write(frame(MIC, MIC_HEADER.pack(seq) + packet))

    async def closed_by_server(self):
        return await asyncio.wait_for(self.reader.read(1), 2) == b""

    def close(self):
        self.writer.close()


class PhoneServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = security.PhoneStore(self.folder.name)
        self.token = self.store.pair("127.0.0.1", KEY.public)
        self.conversation = Recorder()
        self.server = PhoneServer(self.store, self.conversation)
        self.port = await self.server.start("127.0.0.1", 0)

    async def asyncTearDown(self):
        await self.server.stop()
        self.folder.cleanup()

    async def start(self, phone):
        """Start with a fingerprint: a challenge on a list-only login, then a login that carries the signature."""
        browse = await Phone(self.port, self.token, run="browse").connect()
        await browse.login()
        nonce = await browse.ask_nonce()
        browse.close()
        await asyncio.sleep(0.02)
        await phone.connect()
        answer = await phone.login(approve=phone.approval(nonce))
        self.assertTrue(answer["approved"], answer)
        return answer

    async def test_login_proves_both_sides(self):
        phone = await Phone(self.port, self.token).connect()
        answer = await phone.login()
        self.assertEqual(answer["t"], "ok")
        self.assertEqual(answer["mac"], phone.expected_proof)
        self.assertEqual(answer["up"], -1)
        phone.close()

    async def test_the_token_never_crosses_the_connection(self):
        phone = await Phone(self.port, self.token).connect()
        answer = await phone.login()
        self.assertNotIn(self.token, json.dumps(answer))
        phone.close()

    async def test_a_wrong_token_is_refused(self):
        phone = await Phone(self.port, "cd" * 32).connect()
        client_writer_closed = None
        try:
            await phone.login()
        except asyncio.IncompleteReadError:
            client_writer_closed = True
        self.assertTrue(client_writer_closed)
        self.assertFalse(self.server.connected)
        phone.close()

    async def test_mic_before_login_is_refused(self):
        phone = await Phone(self.port, self.token).connect()
        phone.send_mic(1, b"sound")
        await phone.writer.drain()
        self.assertTrue(await phone.closed_by_server())
        self.assertEqual(self.conversation.packets, [])
        phone.close()

    async def test_an_oversized_frame_before_login_is_refused(self):
        phone = await Phone(self.port, self.token).connect()
        phone.writer.write(HEADER.pack(1 << 24, CONTROL))
        await phone.writer.drain()
        self.assertTrue(await phone.closed_by_server())
        phone.close()

    async def test_garbage_json_is_refused(self):
        for garbage in (b"not json", b"[1, 2]", b"null", b'{"t": "auth"}'):
            phone = await Phone(self.port, self.token).connect()
            phone.writer.write(frame(CONTROL, garbage))
            await phone.writer.drain()
            self.assertTrue(await phone.closed_by_server(), garbage)
            phone.close()

    async def test_repeated_failures_close_the_door_even_to_the_right_token(self):
        for _ in range(security.FAILURE_LIMIT):
            phone = await Phone(self.port, "cd" * 32).connect()
            with self.assertRaises(asyncio.IncompleteReadError):
                await phone.login()
            phone.close()
        with self.assertRaises((asyncio.IncompleteReadError, ConnectionError)):
            await Phone(self.port, self.token).connect()

    async def test_a_silent_connection_is_closed_at_the_login_deadline(self):
        original = phone_module.LOGIN_SECONDS
        phone_module.LOGIN_SECONDS = 0.05
        try:
            phone = await Phone(self.port, self.token).connect()
            self.assertTrue(await phone.closed_by_server())
            phone.close()
        finally:
            phone_module.LOGIN_SECONDS = original

    async def test_only_the_paired_address_may_try(self):
        self.store.address = "100.64.0.9"
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.assertEqual(await asyncio.wait_for(reader.read(1), 2), b"")
        writer.close()

    async def test_mic_packets_arrive_once_and_in_order(self):
        phone = Phone(self.port, self.token)
        await self.start(phone)
        for seq, packet in ((1, b"a"), (2, b"b"), (2, b"again"), (1, b"old"), (3, b"c")):
            phone.send_mic(seq, packet)
        await phone.writer.drain()
        await asyncio.sleep(0.05)
        self.assertEqual(self.conversation.packets, [b"a", b"b", b"c"])
        phone.close()

    async def test_a_jump_in_the_mic_frames_is_reported_before_the_audio_after_it(self):
        phone = Phone(self.port, self.token)
        await self.start(phone)
        # Back online after an outage longer than the 10 s the phone keeps: frame numbers jump by 250.
        for seq, packet in ((1, b"a"), (2, b"b"), (253, b"c"), (254, b"d")):
            phone.send_mic(seq, packet)
        await phone.writer.drain()
        await asyncio.sleep(0.05)
        self.assertEqual(self.conversation.packets, [b"a", b"b", "gap", b"c", b"d"])
        phone.close()

    async def test_a_resume_with_every_frame_is_no_gap(self):
        phone = Phone(self.port, self.token)
        await self.start(phone)
        phone.send_mic(1, b"a")
        await phone.writer.drain()
        await asyncio.sleep(0.05)
        phone.close()
        await asyncio.sleep(0.05)
        again = await Phone(self.port, self.token).connect()
        await again.login()
        for seq in range(2, 300):
            again.send_mic(seq, b"x")
        await again.writer.drain()
        await asyncio.sleep(0.05)
        self.assertNotIn("gap", self.conversation.packets)
        again.close()

    async def test_a_short_mic_frame_ends_the_connection_not_the_server(self):
        phone = Phone(self.port, self.token)
        await self.start(phone)
        phone.writer.write(frame(MIC, b"\x01"))
        await phone.writer.drain()
        self.assertTrue(await phone.closed_by_server())
        phone.close()
        again = await Phone(self.port, self.token).connect()
        self.assertEqual((await again.login())["t"], "ok")
        again.close()

    async def test_a_ping_gets_a_pong(self):
        phone = await Phone(self.port, self.token).connect()
        await phone.login()
        phone.writer.write(control({"t": "ping", "ms": 42}))
        kind, payload = await asyncio.wait_for(read_frame(phone.reader), 2)
        self.assertEqual(json.loads(payload), {"t": "pong", "ms": 42})
        phone.close()

    async def test_after_a_drop_each_side_resumes_where_the_other_stopped(self):
        phone = Phone(self.port, self.token)
        await self.start(phone)
        phone.send_mic(5, b"e")
        await phone.writer.drain()
        await asyncio.sleep(0.05)
        for packet in (b"one", b"two", b"three"):
            self.server.send_speech(7, packet)
        for _ in range(3):
            kind, payload = await asyncio.wait_for(read_frame(phone.reader), 2)
            self.assertEqual(kind, SPEECH)
        phone.close()
        await asyncio.sleep(0.05)

        again = await Phone(self.port, self.token, down=1).connect()  # got only the first frame
        answer = await again.login()
        self.assertEqual(answer["up"], 5)
        got = []
        for _ in range(2):
            kind, payload = await asyncio.wait_for(read_frame(again.reader), 2)
            seq, gen = SPEECH_HEADER.unpack_from(payload)
            got.append((seq, gen, payload[SPEECH_HEADER.size:]))
        self.assertEqual(got, [(2, 7, b"two"), (3, 7, b"three")])
        self.assertEqual(self.conversation.new_runs, 2)  # the list-only login, then the started run
        again.close()

    async def test_speech_that_was_cut_off_is_not_resent(self):
        phone = await Phone(self.port, self.token).connect()
        await phone.login()
        self.server.send_speech(1, b"one")
        await asyncio.wait_for(read_frame(phone.reader), 2)
        self.server.forget_speech()
        phone.close()
        await asyncio.sleep(0.05)
        again = await Phone(self.port, self.token, down=0).connect()
        await again.login()
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(read_frame(again.reader), 0.2)
        again.close()

    async def test_a_new_run_starts_clean(self):
        phone = Phone(self.port, self.token)
        await self.start(phone)
        phone.send_mic(9, b"x")
        await phone.writer.drain()
        await asyncio.sleep(0.05)
        phone.close()
        await asyncio.sleep(0.05)
        fresh = Phone(self.port, self.token, run="run-2")
        self.assertEqual((await self.start(fresh))["up"], -1)
        fresh.send_mic(1, b"new")
        await fresh.writer.drain()
        await asyncio.sleep(0.05)
        self.assertEqual(self.conversation.packets[-1], b"new")
        self.assertEqual(self.conversation.new_runs, 4)
        fresh.close()

    async def test_speech_of_an_earlier_run_is_never_resent(self):
        old = await Phone(self.port, self.token, run="run-1").connect()
        await old.login()
        self.server.send_speech(1, b"old reply")
        await asyncio.wait_for(read_frame(old.reader), 2)
        old.close()
        await asyncio.sleep(0.05)
        new = await Phone(self.port, self.token, run="run-2").connect()
        await new.login()
        new.close()
        await asyncio.sleep(0.05)
        again = await Phone(self.port, self.token, run="run-2", down=0).connect()
        await again.login()
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(read_frame(again.reader), 0.2)
        again.close()

    async def test_a_known_run_with_no_microphone_yet_is_told_apart_from_an_unknown_one(self):
        first = Phone(self.port, self.token)
        self.assertEqual((await self.start(first))["up"], -1)
        first.close()
        await asyncio.sleep(0.05)
        again = await Phone(self.port, self.token).connect()
        self.assertEqual((await again.login())["up"], 0)
        again.send_mic(1, b"first")
        await again.writer.drain()
        await asyncio.sleep(0.05)
        self.assertEqual(self.conversation.packets, [b"first"])
        again.close()

    async def test_the_newest_login_wins(self):
        first = await Phone(self.port, self.token).connect()
        await first.login()
        second = await Phone(self.port, self.token).connect()
        await second.login()
        self.assertTrue(await first.closed_by_server())
        self.assertTrue(self.server.connected)
        first.close()
        second.close()


    # --- a fingerprint at every Start ------------------------------------------------

    async def test_the_token_alone_gets_the_list_and_nothing_else(self):
        phone = await Phone(self.port, self.token).connect()
        answer = await phone.login()
        self.assertFalse(answer["approved"])
        self.assertEqual(answer["days"], security.PAIRING_DAYS)
        phone.send_mic(1, b"sound")
        phone.writer.write(control({"t": "tap", "gen": 1}))
        phone.writer.write(control({"t": "release"}))
        await phone.writer.drain()
        await asyncio.sleep(0.05)
        self.assertEqual(self.conversation.packets, [])
        self.assertEqual(self.conversation.messages, [])
        self.assertEqual(self.conversation.grants, [])
        phone.close()

    async def test_a_signed_start_approves_the_run_and_names_the_session(self):
        phone = Phone(self.port, self.token, run="walk")
        await self.start(phone)
        self.assertTrue(self.server.approved)
        grant = self.conversation.grants[-1]
        self.assertEqual((grant["session"], grant["modNonce"], grant["run"]), ("7", "mod-nonce", "walk"))
        phone.writer.write(control({"t": "tap", "gen": 1}))
        await phone.writer.drain()
        await asyncio.sleep(0.05)
        self.assertEqual(self.conversation.messages, [{"t": "tap", "gen": 1}])
        phone.close()

    async def refused(self, approve_for):
        browse = await Phone(self.port, self.token, run="browse").connect()
        await browse.login()
        nonce = await browse.ask_nonce()
        browse.close()
        await asyncio.sleep(0.02)
        phone = await Phone(self.port, self.token, run="walk").connect()
        answer = await phone.login(approve=approve_for(phone, nonce))
        phone.close()
        self.assertFalse(answer["approved"])
        self.assertFalse(self.server.approved)
        self.assertEqual(self.conversation.grants, [])
        return answer.get("refused")

    async def test_a_signature_from_another_key_is_refused(self):
        self.assertEqual(await self.refused(lambda phone, nonce: phone.approval(nonce, key=PhoneKey())), "signature")

    async def test_a_signature_for_another_run_is_refused(self):
        self.assertEqual(await self.refused(lambda phone, nonce: phone.approval(nonce, run="other")), "signature")

    async def test_a_signature_over_another_challenge_is_refused(self):
        self.assertEqual(await self.refused(
            lambda phone, nonce: {**phone.approval(security.new_nonce()), "nonce": nonce}), "signature")

    async def test_a_challenge_works_once(self):
        browse = await Phone(self.port, self.token, run="browse").connect()
        await browse.login()
        nonce = await browse.ask_nonce()
        browse.close()
        await asyncio.sleep(0.02)
        first = Phone(self.port, self.token, run="walk")
        await first.connect()
        approval = first.approval(nonce)
        self.assertTrue((await first.login(approve=approval))["approved"])
        first.close()
        await asyncio.sleep(0.02)
        other = await Phone(self.port, self.token, run="replay").connect()
        answer = await other.login(approve=other.approval(nonce))
        self.assertEqual((answer["approved"], answer["refused"]), (False, "late"))
        other.close()

    async def test_a_late_signature_is_refused(self):
        original = phone_module.APPROVE_SECONDS
        phone_module.APPROVE_SECONDS = 0
        try:
            self.assertEqual(await self.refused(lambda phone, nonce: phone.approval(nonce)), "late")
        finally:
            phone_module.APPROVE_SECONDS = original

    async def test_a_session_that_has_gone_is_refused(self):
        self.conversation.startable = False
        self.assertEqual(await self.refused(lambda phone, nonce: phone.approval(nonce)), "gone")

    async def test_a_session_whose_challenge_changed_is_refused(self):
        self.assertEqual(await self.refused(lambda phone, nonce: phone.approval(nonce, mod_nonce="older")), "changed")

    async def test_a_reconnect_in_an_approved_run_needs_no_new_fingerprint(self):
        phone = Phone(self.port, self.token, run="walk")
        await self.start(phone)
        phone.close()
        await asyncio.sleep(0.05)
        again = await Phone(self.port, self.token, run="walk").connect()
        self.assertTrue((await again.login())["approved"])
        self.assertEqual(len(self.conversation.grants), 1)
        again.close()

    async def test_a_run_that_ended_cannot_be_resumed(self):
        phone = Phone(self.port, self.token, run="walk")
        await self.start(phone)
        self.server.end_run()
        phone.close()
        await asyncio.sleep(0.05)
        again = await Phone(self.port, self.token, run="walk").connect()
        self.assertFalse((await again.login())["approved"])
        again.close()

    async def test_a_hold_after_the_run_ended_is_answered_so_the_closing_notice_plays(self):
        phone = Phone(self.port, self.token, run="walk")
        await self.start(phone)
        self.server.end_run()
        phone.writer.write(control({"t": "hold", "id": 4}))
        await phone.writer.drain()
        kind, payload = await asyncio.wait_for(read_frame(phone.reader), 2)
        self.assertEqual(json.loads(payload), {"t": "resume", "id": 4, "leak": False})
        self.assertEqual(self.conversation.messages, [])
        phone.close()

    async def test_a_list_only_login_ends_the_run_it_replaces(self):
        phone = Phone(self.port, self.token, run="walk")
        await self.start(phone)
        phone.close()
        await asyncio.sleep(0.05)
        browse = await Phone(self.port, self.token, run="browse-2").connect()
        await browse.login()
        browse.close()
        await asyncio.sleep(0.05)
        again = await Phone(self.port, self.token, run="walk").connect()
        self.assertFalse((await again.login())["approved"])
        again.close()

    async def test_an_old_pairing_must_pair_again(self):
        self.store.paired_at -= security.PAIRING_DAYS * 86400 + 1
        phone = await Phone(self.port, self.token).connect()
        answer = await phone.login()
        self.assertTrue(answer["expired"])
        self.assertEqual(answer["mac"], phone.expected_proof)
        self.assertTrue(await phone.closed_by_server())
        phone.close()

    async def test_revoking_closes_the_connection_and_refuses_the_next_login(self):
        phone = Phone(self.port, self.token, run="walk")
        await self.start(phone)
        self.server.revoke()
        self.assertTrue(await phone.closed_by_server())
        self.assertFalse(self.server.approved)
        self.assertFalse(os.path.exists(self.store.path))
        self.assertFalse(os.path.exists(self.store.key_path))
        phone.close()
        with self.assertRaises((asyncio.IncompleteReadError, ConnectionError)):
            again = await Phone(self.port, self.token, run="walk").connect()
            await again.login()


class PairingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = security.PhoneStore(self.folder.name)
        self.pairing = security.PairingWindow()
        self.server = PhoneServer(self.store, Recorder(), self.pairing)
        self.port = await self.server.start("127.0.0.1", 0)

    async def asyncTearDown(self):
        await self.server.stop()
        self.folder.cleanup()

    async def pair(self, code, key=KEY.public):
        phone = await Phone(self.port).connect()
        phone.writer.write(control({"t": "pair", "code": code, "key": key}))
        await phone.writer.drain()
        try:
            kind, payload = await asyncio.wait_for(read_frame(phone.reader), 2)
            return json.loads(payload)
        except asyncio.IncompleteReadError:
            return None
        finally:
            phone.close()

    async def test_the_right_code_gets_the_token(self):
        answer = await self.pair(self.pairing.code)
        self.assertEqual(answer, {"t": "paired", "token": self.store.token})
        self.assertTrue(self.store.allows("127.0.0.1"))
        self.assertEqual(self.store.key, KEY.public)
        with open(self.store.key_path) as f:
            self.assertEqual(f.read().strip(), KEY.public)

    async def test_a_pairing_with_no_usable_key_gets_nothing(self):
        for key in (None, "", "not base64!", base64.b64encode(b"junk").decode()):
            with self.subTest(key=key):
                self.assertIsNone(await self.pair(self.pairing.code, key))
                self.assertFalse(self.store.paired)
        self.assertIsNotNone(await self.pair(self.pairing.code))  # the code still works for the real phone

    async def test_wrong_codes_get_nothing_and_close_the_window(self):
        code = self.pairing.code
        for _ in range(security.PAIR_TRIES):
            self.assertIsNone(await self.pair("0"))
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.assertEqual(await asyncio.wait_for(reader.read(1), 2), b"")
        writer.close()
        self.assertFalse(self.store.paired)
        self.assertFalse(self.pairing.attempt(code))

    async def test_a_code_works_once(self):
        code = self.pairing.code
        self.assertIsNotNone(await self.pair(code))
        phone = await Phone(self.port, self.store.token).connect()
        phone.writer.write(control({"t": "pair", "code": code, "key": KEY.public}))
        await phone.writer.drain()
        self.assertTrue(await phone.closed_by_server())
        phone.close()


class WriteBufferTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_phone_that_stops_reading_is_dropped_not_buffered_forever(self):
        with tempfile.TemporaryDirectory() as folder:
            store = security.PhoneStore(folder)
            token = store.pair("127.0.0.1", KEY.public)
            server = PhoneServer(store, Recorder())
            port = await server.start("127.0.0.1", 0)
            phone = await Phone(port, token).connect()
            await phone.login()
            await asyncio.sleep(0.01)
            big = b"x" * 16000
            for _ in range(4000):
                server.send_speech(1, big)
                if not server.connected:
                    break
                await asyncio.sleep(0)
            self.assertFalse(server.connected)
            phone.close()
            await server.stop()


if __name__ == "__main__":
    unittest.main()
