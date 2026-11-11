"""The mod's side, over a real Unix socket, against a stand-in conversation."""
import asyncio
import json
import os
import socket
import stat
import tempfile
import unittest

from walkie_gateway import modlink
from walkie_gateway.modlink import ModLink


class Recorder:
    def __init__(self):
        self.events = []
        self.chosen = set()
        self.changes = 0

    def mod_event(self, session, event):
        self.events.append((session.instance, event))

    def is_chosen(self, session):
        return session.instance in self.chosen

    def chosen_run(self, session):
        return "run-1" if session.instance in self.chosen else None

    def sessions_changed(self):
        self.changes += 1

    def revoke_phone(self):
        self.revoked = True


async def post(path, route, body, keep_open=False):
    reader, writer = await asyncio.open_unix_connection(path)
    data = json.dumps(body).encode()
    writer.write(("POST %s HTTP/1.1\r\nHost: walkie\r\nContent-Length: %d\r\n\r\n" % (route, len(data))).encode() + data)
    await writer.drain()
    if keep_open:
        return reader, writer
    return await answer(reader, writer)


async def answer(reader, writer):
    raw = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body)


SESSION = {"instance": "123", "session": "abc", "cwd": "/work/project"}


class ModLinkTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "mod.sock")
        self.conversation = Recorder()
        self.link = ModLink(self.path, self.conversation)
        await self.link.start()
        self.original_hold = modlink.POLL_HOLD

    async def asyncTearDown(self):
        modlink.POLL_HOLD = self.original_hold
        await self.link.stop()
        self.folder.cleanup()

    async def test_the_socket_is_only_for_this_user(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    async def test_a_poll_says_whether_a_session_is_a_front_desk(self):
        modlink.POLL_HOLD = 0.01
        await post(self.path, "/next", SESSION)
        self.assertFalse(self.link.sessions["123"].front_desk)
        await post(self.path, "/next", {**SESSION, "role": "front-desk"})
        self.assertTrue(self.link.sessions["123"].front_desk)

    async def test_a_second_gateway_is_refused(self):
        self.addAsyncCleanup(asyncio.sleep, 0.01)  # the server finishes with the probe's connection
        other = ModLink(self.path, Recorder())
        with self.assertRaises(RuntimeError):
            await other.start()
        await asyncio.sleep(0.01)  # the server sees the refused gateway's probe arrive and go, before it closes

    async def test_a_stale_socket_file_is_replaced(self):
        await self.link.stop()
        leftover = socket.socket(socket.AF_UNIX)
        leftover.bind(self.path)
        leftover.close()
        self.assertTrue(os.path.exists(self.path))
        self.link = ModLink(self.path, self.conversation)
        await self.link.start()

    async def test_a_poll_is_answered_within_the_hold(self):
        modlink.POLL_HOLD = 0.05
        status, body = await post(self.path, "/next", SESSION)
        self.assertEqual((status, body), (200, {"chosen": False}))

    async def test_the_hold_stays_under_the_hosts_limit(self):
        self.assertLess(self.original_hold, 30)

    async def test_an_order_reaches_a_held_poll_at_once(self):
        reader, writer = await post(self.path, "/next", SESSION, keep_open=True)
        await asyncio.sleep(0.02)
        self.link.sessions["123"].send({"id": 1, "submit": "hello"})
        status, body = await answer(reader, writer)
        self.assertEqual(body, {"id": 1, "submit": "hello", "chosen": False})

    async def test_an_order_waits_for_the_next_poll(self):
        modlink.POLL_HOLD = 0.02
        await post(self.path, "/next", SESSION)
        self.link.sessions["123"].send({"id": 2, "abort": True})
        status, body = await post(self.path, "/next", SESSION)
        self.assertEqual(body["id"], 2)

    async def test_the_answer_to_the_chosen_session_names_the_approved_run(self):
        # The mod keeps its grant only while answers name the run the phone signed.
        modlink.POLL_HOLD = 0.02
        self.conversation.chosen.add("123")
        status, body = await post(self.path, "/next", SESSION)
        self.assertEqual(body, {"chosen": True, "run": "run-1"})

    async def test_no_other_session_hears_the_run(self):
        modlink.POLL_HOLD = 0.02
        self.conversation.chosen.add("456")
        status, body = await post(self.path, "/next", SESSION)
        self.assertEqual(body, {"chosen": False})

    async def test_a_release_between_two_polls_is_heard_at_once(self):
        # Nothing was waiting to be woken, and the mod would run a queued prompt during a held poll.
        status, body = await asyncio.wait_for(post(self.path, "/next", {**SESSION, "chosen": True}), 1)
        self.assertEqual(body, {"chosen": False})

    async def test_a_choice_between_two_polls_is_heard_at_once(self):
        self.conversation.chosen.add("123")
        status, body = await asyncio.wait_for(post(self.path, "/next", {**SESSION, "chosen": False}), 1)
        self.assertEqual(body, {"chosen": True, "run": "run-1"})

    async def test_a_poll_that_agrees_about_the_choice_is_held(self):
        reader, writer = await post(self.path, "/next", {**SESSION, "chosen": False}, keep_open=True)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(1), 0.05)
        writer.close()

    async def test_the_newest_poll_wins(self):
        old_reader, old_writer = await post(self.path, "/next", SESSION, keep_open=True)
        await asyncio.sleep(0.02)
        new_reader, new_writer = await post(self.path, "/next", SESSION, keep_open=True)
        status, body = await answer(old_reader, old_writer)
        self.assertEqual(body, {"chosen": False})  # the old poll answered, empty
        self.link.sessions["123"].send({"id": 3, "submit": "for the new one"})
        status, body = await answer(new_reader, new_writer)
        self.assertEqual(body["id"], 3)

    async def test_an_order_never_goes_into_a_poll_that_died(self):
        reader, writer = await post(self.path, "/next", SESSION, keep_open=True)
        await asyncio.sleep(0.02)
        writer.close()  # a reload cuts the poll
        await asyncio.sleep(0.05)
        self.link.sessions["123"].send({"id": 4, "submit": "still here"})
        status, body = await post(self.path, "/next", SESSION)
        self.assertEqual(body["id"], 4)

    async def test_a_failed_write_puts_the_order_back(self):
        held_reader, held_writer = await post(self.path, "/next", SESSION, keep_open=True)
        await asyncio.sleep(0.02)
        session = self.link.sessions["123"]
        original = modlink._respond

        async def broken(writer, status, obj):
            modlink._respond = original
            raise BrokenPipeError()

        modlink._respond = broken
        session.send({"id": 5, "submit": "try again"})
        await asyncio.sleep(0.05)
        self.assertEqual(session.orders[0]["id"], 5)
        status, body = await post(self.path, "/next", SESSION)
        self.assertEqual(body["id"], 5)
        held_writer.close()

    async def test_the_session_id_follows_a_clear(self):
        modlink.POLL_HOLD = 0.01
        await post(self.path, "/next", SESSION)
        before = self.conversation.changes
        await post(self.path, "/next", {**SESSION, "session": "after-clear"})
        self.assertEqual(self.link.sessions["123"].session_id, "after-clear")
        self.assertEqual(len(self.link.sessions), 1)
        self.assertGreater(self.conversation.changes, before)

    async def test_events_reach_the_conversation_in_order(self):
        events = [{"kind": "text", "text": "a"}, {"kind": "text", "text": "b"}]
        status, body = await post(self.path, "/event", {**SESSION, "events": events})
        self.assertEqual(status, 200)
        self.assertEqual([e for _, e in self.conversation.events], events)

    async def test_a_bad_request_does_no_harm(self):
        reader, writer = await asyncio.open_unix_connection(self.path)
        writer.write(b"GET /next HTTP/1.1\r\n\r\n")
        await writer.drain()
        status, _ = await answer(reader, writer)
        self.assertEqual(status, 400)
        status, _ = await post(self.path, "/nowhere", SESSION)
        self.assertEqual(status, 404)
        reader, writer = await asyncio.open_unix_connection(self.path)
        writer.write(b"POST /event HTTP/1.1\r\nContent-Length: 9\r\n\r\nnot json!")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        writer.close()
        status, _ = await post(self.path, "/event", {**SESSION, "events": ["not an object"]})
        self.assertEqual(self.conversation.events, [("123", "not an object")])

    async def test_a_session_that_stops_polling_is_forgotten(self):
        modlink.POLL_HOLD = 0.01
        await post(self.path, "/next", SESSION)
        self.assertFalse(self.link.prune())
        self.link.sessions["123"].last_seen -= modlink.SESSION_STALE + 1
        self.assertTrue(self.link.prune())
        self.assertEqual(self.link.sessions, {})

    async def test_the_phone_is_told_only_a_short_description(self):
        modlink.POLL_HOLD = 0.01
        await post(self.path, "/next", {**SESSION, "nonce": "challenge", "promptedAt": 1000})
        self.assertEqual(self.link.sessions["123"].describe(61000, home="/home/me"),
                         {"id": "123", "name": "project", "where": "/work", "login": "", "session": "abc",
                          "nonce": "challenge", "ago": 60})

    async def test_two_folders_of_one_name_differ_by_where_and_login(self):
        modlink.POLL_HOLD = 0.01
        await post(self.path, "/next", {**SESSION, "cwd": "/home/me/Code/firstmate", "login": "default"})
        await post(self.path, "/next", {**SESSION, "instance": "456", "cwd": "/home/me/.copies/1/firstmate",
                                        "login": "personal"})
        one, two = (self.link.sessions[key].describe(0, home="/home/me") for key in ("123", "456"))
        self.assertEqual((one["name"], one["where"], one["login"]), ("firstmate", "~/Code", "default"))
        self.assertEqual((two["name"], two["where"], two["login"]), ("firstmate", "~/.copies/1", "personal"))

    async def test_where_names_home_only_as_a_whole_folder(self):
        session = modlink.Session("1")
        for cwd, where in (("/home/me/project", "~"), ("/home/meg/project", "/home/meg"), ("/project", "/"), ("/", "")):
            session.cwd = cwd
            self.assertEqual(session.describe(0, home="/home/me")["where"], where)

    async def test_a_new_login_name_updates_the_phones_list_and_stays_short(self):
        modlink.POLL_HOLD = 0.01
        await post(self.path, "/next", {**SESSION, "login": "default"})
        before = self.conversation.changes
        await post(self.path, "/next", {**SESSION, "login": "x" * 100})
        self.assertGreater(self.conversation.changes, before)
        self.assertEqual(self.link.sessions["123"].login, "x" * modlink.MAX_LOGIN)
        await post(self.path, "/event", {**SESSION, "events": []})
        self.assertEqual(self.link.sessions["123"].login, "x" * modlink.MAX_LOGIN)

    async def test_a_new_challenge_or_prompt_updates_the_phones_list(self):
        modlink.POLL_HOLD = 0.01
        await post(self.path, "/next", {**SESSION, "nonce": "one"})
        before = self.conversation.changes
        await post(self.path, "/next", {**SESSION, "nonce": "one"})
        self.assertEqual(self.conversation.changes, before)
        await post(self.path, "/next", {**SESSION, "nonce": "two"})
        await post(self.path, "/next", {**SESSION, "nonce": "two", "promptedAt": 5})
        self.assertEqual(self.conversation.changes, before + 2)
        self.assertEqual((self.link.sessions["123"].nonce, self.link.sessions["123"].prompted_at), ("two", 5))

    async def test_a_session_is_spoken_of_by_its_folder_then_its_login_unless_the_default(self):
        session = modlink.Session("1")
        session.cwd = "/work/firstmate/"
        for login, name in (("default", "firstmate"), ("", "firstmate"), ("personal", "firstmate personal")):
            session.login = login
            self.assertEqual(session.name, name, login)

    async def test_an_event_leaves_the_challenge_alone(self):
        modlink.POLL_HOLD = 0.01
        await post(self.path, "/next", {**SESSION, "nonce": "one"})
        await post(self.path, "/event", {**SESSION, "events": []})
        self.assertEqual(self.link.sessions["123"].nonce, "one")

    async def test_revoke_reaches_the_conversation(self):
        status, _ = await post(self.path, "/revoke", {})
        self.assertEqual(status, 200)
        self.assertTrue(self.conversation.revoked)


if __name__ == "__main__":
    unittest.main()
