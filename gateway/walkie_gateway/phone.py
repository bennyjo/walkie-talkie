"""The phone's connection: framing, login, and resuming after a dropped connection.

Each frame is a 4-byte big-endian payload length, a 1-byte type, and the payload:

    CONTROL  JSON text
    MIC      4-byte sequence number, then one Opus packet (phone to gateway)
    SPEECH   4-byte sequence number, 2-byte generation, then one Opus packet (gateway to phone)

Login, right after connecting:

    gateway  {"t": "challenge", "nonce": S}
    phone    {"t": "auth", "nonce": C, "mac": M, "run": R, "down": D, "approve": A}
    gateway  {"t": "ok", "mac": G, "up": U, "approved": B, "days": N}

M and G are HMACs of the two nonces under the shared token, so each side proves
it holds the token and neither sends it. The phone sends no audio until G checks
out. R names one run of the phone's service. D is the last SPEECH sequence the
phone got, and U is the last MIC sequence the gateway got, 0 if none yet, or -1
if it doesn't know this run. Each side then resends what the other missed. N is
how many days the pairing has left.

The token alone gets the list of sessions, and nothing else: no audio goes either
way, and no session is chosen. A run starts only with a fingerprint on the phone:

    phone    {"t": "nonce"}
    gateway  {"t": "nonce", "nonce": K}       good once, for APPROVE_SECONDS

The phone's secure hardware then signs security.start_message(K, R, session,
its nonce) after a fingerprint, and the phone logs in for that run with
A = {"session": ..., "modNonce": ..., "nonce": K, "sig": ...}. B says whether R is
an approved run. A refused approval also carries "refused": late, signature, gone, or
changed, when the session's own challenge moved on since the list the phone signed from. An approved run
ends at Stop, after the phone has been gone PHONE_LOST_SECONDS, after 30 minutes
of nothing said, or when the session goes, and the gateway then sends
{"t": "ended", "why": ...}. A run that has ended can't be resumed: the next Start
takes a new fingerprint.

A pairing older than security.PAIRING_DAYS gets {"t": "ok", ..., "expired": true}
after the proofs, and nothing more.

Other CONTROL messages, gateway to phone: "sessions", "voices", "tone", "pong",
"confirm", "ended", "cut" with the generation to drop below, "resume" with a hold's
id and a "leak" flag, and "leak". That flag and that message say the sound behind
the hold may have been the earbuds. Phone to gateway: "release", "tap", "played",
"voice", "confirmed", "declined", "ping", and "hold" with an id, which says the
phone silenced a reply because it heard a voice over it.

To pair instead: phone sends {"t": "pair", "code": C, "key": K}, with K its public
key, and the gateway answers {"t": "paired", "token": T} and closes.
"""
import asyncio
import collections
import json
import logging
import struct
import time

from . import security

log = logging.getLogger("walkie.phone")

HEADER = struct.Struct(">IB")
CONTROL, MIC, SPEECH = 1, 2, 3
MIC_HEADER = struct.Struct(">I")
SPEECH_HEADER = struct.Struct(">IH")
MAX_FRAME_BEFORE_LOGIN = 2048
APPROVE_SECONDS = 60    # from asking for a challenge to logging in with its signature
MAX_FRAME = 16384
LOGIN_SECONDS = 5
IDLE_SECONDS = 30       # the phone pings every 5 s, so silence this long means it's gone
MAX_UNAUTHENTICATED = 4
MAX_WRITE_BUFFER = 1 << 20
RESUME_FRAMES = 1500    # 30 s of speech
MIC_GAP_FRAMES = 5      # 100 ms: microphone frames missing beyond this mean words may be missing too
# A dropped connection, or a frame that isn't what it should be: either way, this connection is over.
BAD_CONNECTION = (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError,
                  ValueError, KeyError, TypeError, AttributeError, struct.error)


def frame(kind, payload):
    return HEADER.pack(len(payload), kind) + payload


def control(obj):
    return frame(CONTROL, json.dumps(obj, separators=(",", ":")).encode())


async def read_frame(reader, limit):
    length, kind = HEADER.unpack(await reader.readexactly(HEADER.size))
    if length > limit:
        raise ValueError("frame too long")
    return kind, await reader.readexactly(length)


class PhoneServer:
    """Listens for the phone. One connection at a time: the newest login wins."""

    def __init__(self, store, conversation, pairing=None, clock=time.monotonic):
        self.store = store
        self.clock = clock
        self.conversation = conversation
        self.pairing = pairing
        self.limiter = security.FailureLimiter()
        self.server = None
        self.writer = None
        self.waiting_logins = 0
        self.run_id = None
        self.approved_run = None    # the run a fingerprint started, until it ends
        self.challenge = None       # (nonce, expires): the one challenge a fingerprint may sign
        self.up = -1
        self.sent = collections.deque(maxlen=RESUME_FRAMES)
        self.seq = 0

    async def start(self, host, port):
        security.check_bind(host)
        try:
            self.server = await asyncio.start_server(self._handle, host, port, backlog=4)
        except OSError:
            raise security.SecurityError(
                "Can't listen on %s, port %d. If Tailscale is stopped, connect it first." % (host, port)) from None
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.writer:
            self.writer.close()
        if self.server:
            self.server.close()

    # --- what the conversation calls ---------------------------------------------

    @property
    def connected(self):
        return self.writer is not None and not self.writer.is_closing()

    @property
    def approved(self):
        """A fingerprint started this run, and it hasn't ended."""
        return self.approved_run is not None and self.approved_run == self.run_id

    def end_run(self):
        """The run is over: nothing more is taken from the phone until a new fingerprint."""
        self.approved_run = None

    def send_ended(self, run, why):
        """Tells the phone its run is over, if it's still the run that ended."""
        if run == self.run_id and not self.approved:
            self.send_control({"t": "ended", "why": why})

    def revoke(self):
        """The phone is no longer trusted, starting now: its pairing goes, and so does its connection."""
        self.store.forget()
        self.pairing = None
        self.challenge = None
        self.end_run()
        log.info("phone revoked")
        if self.writer:
            self.writer.close()

    def signed(self, message, signature):
        return self.store.signed(message, signature)

    def send_control(self, obj):
        self._write(control(obj))

    def send_speech(self, gen, packet):
        self.seq += 1
        data = frame(SPEECH, SPEECH_HEADER.pack(self.seq, gen & 0xffff) + packet)
        self.sent.append((self.seq, gen, data))
        self._write(data)

    def forget_speech(self):
        """Nothing sent so far may be resent: the person cut it off."""
        self.sent.clear()

    def _write(self, data):
        if not self.connected:
            return
        transport = self.writer.transport
        if transport.get_write_buffer_size() > MAX_WRITE_BUFFER:
            log.info("the phone stopped reading, dropping the connection")
            self.writer.close()
            return
        self.writer.write(data)

    # --- the connection ----------------------------------------------------------

    async def _handle(self, reader, writer):
        peer = writer.get_extra_info("peername")[0]
        if not self._peer_may_try(peer) or self.limiter.closed() or self.waiting_logins >= MAX_UNAUTHENTICATED:
            writer.close()
            return
        self.waiting_logins += 1
        try:
            result = await asyncio.wait_for(self._login(reader, writer, peer), LOGIN_SECONDS)
        except BAD_CONNECTION:
            result = None
        finally:
            self.waiting_logins -= 1
        if result is None:
            writer.close()
            return
        run, down, grant = result
        if self.writer:
            self.writer.close()  # the newest login wins
        fresh = run != self.run_id
        if fresh:
            self.run_id, self.up = run, 0
            self.approved_run = None  # a run that another run replaced has ended
            self.sent.clear()
            self.conversation.phone_new_run()
        if grant:
            self.approved_run = run
            self.conversation.phone_approved(grant)
        self.writer = writer
        log.info("phone connected")
        resend = [] if fresh else [data for seq, _, data in self.sent if seq > down]
        for data in resend:
            self._write(data)
        self.conversation.phone_connected()
        try:
            await self._read_loop(reader)
        except BAD_CONNECTION:
            pass
        finally:
            if self.writer is writer:
                self.writer = None
                log.info("phone disconnected")
            writer.close()

    def _peer_may_try(self, peer):
        if self.pairing and self.pairing.open():
            return security.is_tailnet(peer) or security.is_loopback(peer)
        return self.store.allows(peer)

    async def _login(self, reader, writer, peer):
        nonce = security.new_nonce()
        writer.write(control({"t": "challenge", "nonce": nonce}))
        await writer.drain()
        kind, payload = await read_frame(reader, MAX_FRAME_BEFORE_LOGIN)
        if kind != CONTROL:
            raise ValueError("expected a control frame")
        hello = json.loads(payload)
        if hello.get("t") == "pair":
            await self._pair(hello, writer, peer)
            return None
        if hello.get("t") != "auth" or not self.store.paired or not self.store.allows(peer):
            self.limiter.failed()
            return None
        token = self.store.token
        client_nonce, mac = hello["nonce"], hello["mac"]
        if not isinstance(client_nonce, str) or not isinstance(mac, str) or len(client_nonce) != 32:
            raise ValueError("bad login")
        expected = security.phone_mac(token, nonce, client_nonce)
        if not security.hmac.compare_digest(mac.encode(), expected.encode()):
            self.limiter.failed()
            log.info("a login failed")
            return None
        run, down = hello["run"], int(hello["down"])
        if not isinstance(run, str) or not run:
            raise ValueError("bad run")
        answer = {"t": "ok", "mac": security.gateway_mac(token, nonce, client_nonce)}
        if self.store.expired():
            log.info("the pairing has expired")
            writer.write(control({**answer, "up": -1, "approved": False, "expired": True}))
            await writer.drain()
            return None
        grant, refused = None, None
        approved = run == self.approved_run
        if not approved and hello.get("approve") is not None:
            grant, refused = self._approval(hello["approve"], run)
            approved = grant is not None
        answer.update({"up": self.up if run == self.run_id else -1, "approved": approved,
                       "days": self.store.days_left()})
        if refused:
            answer["refused"] = refused
        writer.write(control(answer))
        await writer.drain()
        return run, down, grant

    def _approval(self, approve, run):
        """A fingerprint's signature over the one challenge, for a session that's still there. Used once."""
        challenge, self.challenge = self.challenge, None
        if not isinstance(approve, dict):
            raise ValueError("bad approval")
        nonce, session, mod_nonce, sig = (approve.get(k) for k in ("nonce", "session", "modNonce", "sig"))
        if not all(isinstance(v, str) and v for v in (nonce, session, mod_nonce, sig)):
            raise ValueError("bad approval")
        if challenge is None or challenge[0] != nonce or self.clock() > challenge[1]:
            log.info("an approval came too late")
            return None, "late"
        if not self.store.signed(security.start_message(nonce, run, session, mod_nonce), sig):
            self.limiter.failed()
            log.info("an approval's signature failed")
            return None, "signature"
        problem = self.conversation.start_problem(session, mod_nonce)
        if problem:
            return None, problem
        log.info("a fingerprint started a run")
        return {"session": session, "modNonce": mod_nonce, "nonce": nonce, "run": run, "sig": sig}, None

    async def _pair(self, hello, writer, peer):
        try:
            security.phone_key(hello.get("key"))
        except ValueError:
            self.limiter.failed()
            log.info("a pairing came without a usable key")  # the code stays good for the real phone
            return
        if not self.pairing or not self.pairing.attempt(hello.get("code")):
            self.limiter.failed()
            log.info("a pairing attempt failed")
            return
        token = self.store.pair(peer, hello["key"])
        self.limiter = security.FailureLimiter()
        writer.write(control({"t": "paired", "token": token}))
        await writer.drain()
        self.pairing = None
        print("Paired with the phone at %s." % peer, flush=True)

    async def _read_loop(self, reader):
        while True:
            kind, payload = await asyncio.wait_for(read_frame(reader, MAX_FRAME), IDLE_SECONDS)
            if kind == MIC:
                seq = MIC_HEADER.unpack_from(payload)[0]
                if self.approved and seq > self.up:
                    if seq - self.up > MIC_GAP_FRAMES:  # the phone only keeps its last 10 s to resend
                        self.conversation.mic_gap()
                    self.up = seq
                    self.conversation.mic_packet(payload[MIC_HEADER.size:])
            elif kind == CONTROL:
                message = json.loads(payload)
                if message.get("t") == "ping":
                    self.send_control({"t": "pong", "ms": message.get("ms")})
                elif message.get("t") == "nonce":
                    self.challenge = (security.new_nonce(), self.clock() + APPROVE_SECONDS)
                    self.send_control({"t": "nonce", "nonce": self.challenge[0]})
                elif self.approved:
                    self.conversation.phone_message(message)
                elif message.get("t") == "hold":  # an ended run's closing notice must not stay silenced
                    self.send_control({"t": "resume", "id": message.get("id"), "leak": False})
