"""The mod's side: a small HTTP server on a Unix socket only this user can reach.

Each live session's mod long-polls POST /next and answers with POST /event. The
rules come from running the mod against a stub:

- A poll is answered within 25 s, because the host cuts a fetch at 30 s.
- A poll that's wrong about whether its session is chosen is answered at once, because
  a release can land between two polls, and queued prompts wait on hearing it.
- The newest poll for a session wins, because a reloaded mod leaves an old one behind.
- An order whose write fails goes back in the queue, because that poll was already dead.

Each poll also carries the session's own challenge, which a fingerprint on the phone signs
to choose it, when it last took a prompt, and a short name for its login. A session with no challenge is a mod from before
fingerprints, which can't check a choice, so the phone never sees it. Each answer to the chosen
session names the run the fingerprint approved, and the mod keeps its grant only while answers do.
So something that answers on the socket without having asked this gateway can't ride a grant.
Any poll in the chosen session's name hears the run, so a process running as this user can.

Any process running as this user can reach this socket. POST /revoke drops the phone's pairing:
see `walkie-gateway revoke`.
"""
import asyncio
import collections
import json
import logging
import os
import socket
import time

log = logging.getLogger("walkie.mod")

POLL_HOLD = 25
SESSION_STALE = 35      # seconds with no poll before a session counts as gone
MAX_BODY = 1 << 20
MAX_LOGIN = 40          # characters of a login's name the phone shows


class Session:
    """One live Claude Code session, as its mod reports it."""

    def __init__(self, instance):
        self.instance = instance
        self.session_id = ""
        self.cwd = ""
        self.last_seen = time.monotonic()
        self.orders = collections.deque()
        self.waiter = None
        self.poll_id = 0
        self.front_desk = False     # takes spoken prompts for another session, which does the work
        self.nonce = ""             # the session's own challenge, which the phone signs to choose it
        self.prompted_at = 0        # when it last took a prompt, in ms since the epoch, 0 if never
        self.login = ""             # a short name for its login, such as "default" or "personal"

    def describe(self, now_ms, home=None):
        """What the phone shows. Two sessions in folders of the same name differ by where, and one folder open
        on two logins differs by login. The mod sends only a folder it may name, so its parents may be named too."""
        cwd = self.cwd.rstrip("/")
        name = os.path.basename(cwd) or self.cwd
        where = os.path.dirname(cwd) if name != self.cwd else ""
        home = (home if home is not None else os.path.expanduser("~")).rstrip("/")
        if home and (where == home or where.startswith(home + "/")):
            where = "~" + where[len(home):]
        ago = max(0, round((now_ms - self.prompted_at) / 1000)) if self.prompted_at else None
        return {"id": self.instance, "name": name, "where": where, "login": self.login,
                "session": self.session_id[:8], "nonce": self.nonce, "ago": ago}

    def wake(self):
        if self.waiter and not self.waiter.done():
            self.waiter.set_result(None)

    def send(self, order):
        self.orders.append(order)
        self.wake()

    def forget_prompts(self):
        """The phone let this session go, so nothing it said may still arrive. A tap may."""
        self.orders = collections.deque(order for order in self.orders if "abort" in order)


class ModLink:
    def __init__(self, path, conversation):
        self.path = path
        self.conversation = conversation
        self.sessions = {}
        self.server = None

    async def start(self):
        if os.path.exists(self.path):
            if _in_use(self.path):
                raise RuntimeError("Another gateway is already running on %s." % self.path)
            os.unlink(self.path)
        old_umask = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(self._handle, self.path)
        finally:
            os.umask(old_umask)

    async def stop(self):
        if self.server:
            self.server.close()
        for session in self.sessions.values():
            session.wake()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def prune(self):
        """Forget sessions that stopped polling. Returns True if the list changed."""
        now = time.monotonic()
        gone = [k for k, s in self.sessions.items()
                if now - s.last_seen > SESSION_STALE and not (s.waiter and not s.waiter.done())]
        for key in gone:
            del self.sessions[key]
        return bool(gone)

    async def _handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = head.decode("latin-1").split("\r\n")
            method, path = lines[0].split(" ")[:2]
            length = 0
            for line in lines[1:]:
                name, _, value = line.partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            if method != "POST" or length > MAX_BODY:
                await _respond(writer, 400, {})
                return
            body = json.loads(await asyncio.wait_for(reader.readexactly(length), 5)) if length else {}
            if path == "/next":
                await self._poll(body, reader, writer)
            elif path == "/revoke":
                self.conversation.revoke_phone()
                await _respond(writer, 200, {})
            elif path == "/event":
                self._events(body)
                await _respond(writer, 200, {})
            else:
                await _respond(writer, 404, {})
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
                ValueError, KeyError, TypeError, AttributeError, ConnectionError, OSError) as e:
            log.info("a mod request failed: %s", type(e).__name__)
        finally:
            writer.close()

    def _session(self, body):
        instance = str(body["instance"])
        session = self.sessions.get(instance)
        changed = session is None
        if changed:
            session = self.sessions[instance] = Session(instance)
        session.last_seen = time.monotonic()
        session_id, cwd = str(body.get("session") or ""), str(body.get("cwd") or "")
        changed = changed or session_id != session.session_id or cwd != session.cwd
        session.session_id, session.cwd = session_id, cwd
        if changed:
            self.conversation.sessions_changed()
        return session

    async def _poll(self, body, reader, writer):
        session = self._session(body)
        session.front_desk = body.get("role") == "front-desk"
        nonce, prompted_at = str(body.get("nonce") or ""), body.get("promptedAt") or 0
        login = str(body.get("login") or "")[:MAX_LOGIN]
        if not isinstance(prompted_at, (int, float)):
            prompted_at = 0
        if nonce != session.nonce or prompted_at != session.prompted_at or login != session.login:
            session.nonce, session.prompted_at, session.login = nonce, prompted_at, login
            self.conversation.sessions_changed()
        session.poll_id += 1
        mine = session.poll_id
        session.wake()  # an older poll answers now, and takes nothing
        if not session.orders and bool(body.get("chosen")) == self.conversation.is_chosen(session):
            session.waiter = asyncio.get_running_loop().create_future()
            closed = asyncio.ensure_future(reader.read(1))  # end of file: this poll died
            await asyncio.wait({session.waiter, closed}, timeout=POLL_HOLD,
                               return_when=asyncio.FIRST_COMPLETED)
            alive = not closed.done()
            closed.cancel()
            if mine == session.poll_id:
                session.waiter = None
            if not alive:
                return
        session.last_seen = time.monotonic()
        order = session.orders.popleft() if mine == session.poll_id and session.orders else None
        answer = dict(order or {})
        answer["chosen"] = self.conversation.is_chosen(session)
        run = self.conversation.chosen_run(session)
        if run:
            answer["run"] = run  # the mod keeps its grant only while answers name the run the phone signed
        try:
            await _respond(writer, 200, answer)
        except (ConnectionError, OSError):
            if order:
                session.orders.appendleft(order)
                session.wake()
            raise

    def _events(self, body):
        session = self._session(body)
        for event in body.get("events", []):
            self.conversation.mod_event(session, event)


async def _respond(writer, status, obj):
    body = json.dumps(obj).encode()
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}[status]
    writer.write(("HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                  "Connection: close\r\n\r\n" % (status, reason, len(body))).encode() + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def _in_use(path):
    probe = socket.socket(socket.AF_UNIX)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()
