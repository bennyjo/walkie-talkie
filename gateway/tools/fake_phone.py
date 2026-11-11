#!/usr/bin/env python3
"""Stands in for the phone: speaks a WAV file to the gateway in real time, and records what comes back.

For testing on the Mac, with no phone. Pair once with --pair CODE --key-file FILE, which makes a
software key in place of the phone's hardware key, saves it in FILE, and prints the token. Each run
then starts as the app does: it lists the sessions, asks for a challenge, signs the chosen session
with that key, and logs in with the signature. Each --say file is one turn. It prints when each thing
arrived, in seconds after that clip ended. With --confirm, it signs any read-back or spoken switch the
gateway asks to confirm, as a fingerprint in the app would.

To talk over a reply, --talk-over names a clip to speak, --talk-after seconds into the reply. Like
the app, it sends a hold just after the voice starts, and stops playing what it received. --leak-db
plays the reply back into the mic at that level, as earbuds leaking into a collar mic would.
--false-hold-after sends a hold with no voice, as a cough would, to see the reply resume.
"""
import argparse
import asyncio
import base64
import json
import os
import secrets
import time
import wave

import av
import numpy as np
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from walkie_gateway import security
from walkie_gateway.phone import (
    CONTROL,
    HEADER,
    MIC,
    MIC_HEADER,
    SPEECH,
    SPEECH_HEADER,
    control,
    frame,
)
from walkie_gateway.speech import OpusDecoder, Speech


async def read_frame(reader):
    length, kind = HEADER.unpack(await reader.readexactly(HEADER.size))
    return kind, await reader.readexactly(length)


LOUDNESS_OF_REPLY = 0.12    # a Kokoro reply's level while speaking, as a share of full scale


def load_wav(path):
    with wave.open(path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, \
            "the clip must be 16 kHz mono 16-bit"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def opus_encoder():
    codec = av.CodecContext.create("libopus", "w")
    codec.rate = 16000
    codec.layout = "mono"
    codec.format = "s16"
    codec.bit_rate = 24000
    codec.options = {"application": "voip", "frame_duration": "20"}
    codec.open()
    return codec


def load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), None)


def sign(key, message):
    return base64.b64encode(key.sign(message.encode(), ec.ECDSA(hashes.SHA256()))).decode()


class FakePhone:
    def __init__(self, host, port, token, key=None, confirm=False):
        self.host, self.port, self.token, self.key, self.confirm = host, port, token, key, confirm
        self.run = secrets.token_hex(16)
        self.nonce = None
        self.nonce_ready = asyncio.Event()
        self.seq = 0
        self.pts = 0
        self.encoder = opus_encoder()
        self.events = []        # (time, what)
        self.speech = []        # (time, gen, packet)
        self.sessions = None
        self.cut_gen = 0
        self.on_sessions = asyncio.Event()
        self.leak_gain = None   # the reply goes back into the mic through this gain
        self.leak = np.zeros(0, dtype=np.int16)
        self.leak_decoder = None
        self.held = False       # as the app does: what arrives during a hold doesn't play
        self.cuts = []
        self.resumes = []

    async def connect(self, approve=None):
        """Logs in for this run. With approve, the login carries the fingerprint's signature."""
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        kind, payload = await read_frame(self.reader)
        server_nonce = json.loads(payload)["nonce"]
        client_nonce = security.new_nonce()
        auth = {
            "t": "auth", "nonce": client_nonce, "run": self.run, "down": 0,
            "mac": security.phone_mac(self.token, server_nonce, client_nonce),
        }
        if approve:
            auth["approve"] = approve
        self.writer.write(control(auth))
        await self.writer.drain()
        kind, payload = await read_frame(self.reader)
        answer = json.loads(payload)
        if answer.get("mac") != security.gateway_mac(self.token, server_nonce, client_nonce):
            raise SystemExit("the gateway failed to prove it holds the token")
        self.task = asyncio.ensure_future(self._reader())
        return answer

    async def start(self, session):
        """What Start does: a fresh challenge on the list's connection, then a new run that carries the signature."""
        self.on_sessions.clear()
        self.send({"t": "nonce"})
        await asyncio.wait_for(self.nonce_ready.wait(), 5)
        message = security.start_message(self.nonce, self.run, session["id"], session["nonce"])
        approve = {"session": session["id"], "modNonce": session["nonce"], "nonce": self.nonce, "sig": sign(self.key, message)}
        self.task.cancel()
        self.writer.close()
        return await self.connect(approve)

    async def _reader(self):
        while True:
            kind, payload = await read_frame(self.reader)
            now = time.monotonic()
            if kind == SPEECH:
                seq, gen = SPEECH_HEADER.unpack_from(payload)
                if gen >= self.cut_gen:
                    if not self.speech:
                        self.send({"t": "played", "rtt": 0})
                    self.speech.append((now, gen, payload[SPEECH_HEADER.size:]))
                    self._leak(payload[SPEECH_HEADER.size:])
            elif kind == CONTROL:
                message = json.loads(payload)
                if message["t"] == "sessions":
                    self.sessions = message
                    self.on_sessions.set()
                elif message["t"] == "cut":
                    if message["gen"] > self.cut_gen:
                        self.cuts.append(now)
                    self.cut_gen = message["gen"]
                    self.held = False
                elif message["t"] == "resume":
                    self.resumes.append(now)
                    self.held = False
                elif message["t"] == "nonce":
                    self.nonce = message["nonce"]
                    self.nonce_ready.set()
                elif message["t"] == "ended":
                    print("ended:", message.get("why"), flush=True)
                elif message["t"] == "confirm" and message.get("nonce"):
                    print("asked to confirm:", message["text"], flush=True)
                    if self.confirm:
                        target = message.get("switch")
                        signed = (security.start_message(message["nonce"], self.run, target["id"], target["modNonce"])
                                  if target else security.confirm_message(message["nonce"], message["text"]))
                        self.send({"t": "confirmed", "nonce": message["nonce"], "sig": sign(self.key, signed)})
                self.events.append((now, message["t"] + ":" + str(message.get("kind", ""))))

    def _leak(self, packet):
        if self.leak_gain is None or self.held:
            return
        if self.leak_decoder is None:
            self.leak_decoder = OpusDecoder()
        heard = self.leak_decoder.decode(packet)
        self.leak = np.concatenate((self.leak, (heard * self.leak_gain).astype(np.int16)))

    def hold(self, hold_id=1):
        self.held = True    # what already reached the mic's queue still plays, like Bluetooth's tail
        self.send({"t": "hold", "id": hold_id})

    async def send_mic(self, samples):
        """Real time: one 20 ms packet every 20 ms."""
        started = time.monotonic()
        step = 320
        for i in range(0, len(samples) - step + 1, step):
            chunk = samples[i:i + step]
            if len(self.leak):
                leaked = self.leak[:step]
                self.leak = self.leak[step:]
                chunk = chunk.copy()
                chunk[:len(leaked)] = np.clip(chunk[:len(leaked)].astype(np.int32) + leaked, -32768, 32767)
            piece = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
            piece.rate = 16000
            piece.pts = self.pts
            self.pts += step
            for packet in self.encoder.encode(piece):
                self.seq += 1
                self.writer.write(frame(MIC, MIC_HEADER.pack(self.seq) + bytes(packet)))
            await self.writer.drain()
            wait = started + (i + step) / 16000 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)

    def send(self, obj):
        self.writer.write(control(obj))


async def pair(host, port, code, key_file):
    key = ec.generate_private_key(ec.SECP256R1())
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    reader, writer = await asyncio.open_connection(host, port)
    await read_frame(reader)
    writer.write(control({"t": "pair", "code": code, "key": public}))
    await writer.drain()
    _, payload = await read_frame(reader)
    print(json.loads(payload)["token"])


def decode_speech(packets):
    codec = av.CodecContext.create("libopus", "r")
    codec.layout = "mono"
    out = []
    for packet in packets:
        for f in codec.decode(av.Packet(packet)):
            out.append(f.to_ndarray().reshape(-1))
    audio = np.concatenate(out) if out else np.zeros(0, dtype=np.int16)
    if audio.dtype != np.int16:
        audio = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    return audio


def to_mic_rate(audio):
    frame_in = av.AudioFrame.from_ndarray(audio.reshape(1, -1), format="s16", layout="mono")
    frame_in.rate = 48000
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    return np.concatenate([f.to_ndarray().reshape(-1) for f in resampler.resample(frame_in)])


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--pair")
    parser.add_argument("--token-file")
    parser.add_argument("--key-file", required=True, help="the stand-in for the phone's hardware key")
    parser.add_argument("--confirm", action="store_true",
                        help="confirm read-backs and spoken switches, as a fingerprint in the app would")
    parser.add_argument("--session", help="pick the session whose name holds this text")
    parser.add_argument("--say", action="append", default=[], help="a 16 kHz mono WAV to speak; repeatable")
    parser.add_argument("--tap-after", type=float, help="seconds after the first reply audio to tap")
    parser.add_argument("--talk-over", help="a 16 kHz mono WAV to speak over the reply")
    parser.add_argument("--talk-after", type=float, default=1, help="seconds into the reply to talk over it")
    parser.add_argument("--leak-db", type=float, help="play the reply back into the mic at about this level")
    parser.add_argument("--false-hold-after", type=float, help="seconds into the reply to send a hold with no voice")
    parser.add_argument("--voice", help="choose this voice first")
    parser.add_argument("--wait", type=float, default=30, help="seconds to wait for each reply")
    parser.add_argument("--quiet-after", type=float, default=1.5, help="reply ended after this much quiet")
    parser.add_argument("--gap", type=float, default=2, help="seconds of quiet between clips")
    parser.add_argument("--save-reply", help="save the last reply's audio as a 48 kHz WAV")
    parser.add_argument("--models", help="transcribe each reply with the speech models in this folder")
    args = parser.parse_args()

    if args.pair:
        await pair(args.host, args.port, args.pair, args.key_file)
        return
    speech = Speech(args.models) if args.models else None
    phone = FakePhone(args.host, args.port, open(args.token_file).read().strip(), load_key(args.key_file), args.confirm)
    await phone.connect()
    await asyncio.wait_for(phone.on_sessions.wait(), 10)
    deadline = time.monotonic() + 20
    pick = None
    while time.monotonic() < deadline and not pick:
        for s in phone.sessions["list"]:
            if not args.session or args.session in s["name"]:
                pick = s
        await asyncio.sleep(0.2)
    if not pick:
        raise SystemExit("no live session: %s" % json.dumps(phone.sessions))
    await asyncio.sleep(1)  # as a person would: the list may still change, as a run this login replaced ends
    pick = next((s for s in phone.sessions["list"] if s["id"] == pick["id"]), pick)
    answer = await phone.start(pick)
    if not answer.get("approved"):
        raise SystemExit("the gateway refused the start: %s" % answer.get("refused"))
    print("started with", pick["name"], flush=True)
    if args.voice:
        phone.send({"t": "voice", "id": args.voice})
    if args.leak_db is not None:
        phone.leak_gain = 10 ** (args.leak_db / 20) / LOUDNESS_OF_REPLY

    silence = np.zeros(16000, dtype=np.int16)
    await phone.send_mic(silence)
    results = []
    for path in args.say:
        phone.speech.clear()
        phone.events.clear()
        clip = load_wav(path)
        await phone.send_mic(clip)
        spoke = time.monotonic()
        feeder = asyncio.ensure_future(phone.send_mic(np.zeros(16000 * int(args.wait + 5), dtype=np.int16)))
        end = spoke + args.wait
        tapped = False
        talked = False
        false_held = None
        while time.monotonic() < end:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            if phone.speech:
                if args.tap_after is not None and not tapped and now > phone.speech[0][0] + args.tap_after:
                    phone.send({"t": "tap"})
                    tapped = time.monotonic()
                    print("tapped at %.2f" % (tapped - spoke), flush=True)
                if args.false_hold_after is not None and false_held is None \
                        and now > phone.speech[0][0] + args.false_hold_after:
                    false_held = now
                    phone.hold()
                    await asyncio.sleep(0)
                if args.talk_over and not talked and now > phone.speech[0][0] + args.talk_after:
                    talked = now
                    feeder.cancel()
                    voice = asyncio.ensure_future(phone.send_mic(load_wav(args.talk_over)))
                    await asyncio.sleep(0.06)   # the phone's gate needs three 20 ms frames
                    phone.hold()
                    await voice
                    feeder = asyncio.ensure_future(phone.send_mic(np.zeros(16000 * int(args.wait + 5), dtype=np.int16)))
                    end = time.monotonic() + args.wait
                waiting_for_answer = talked and not (phone.cuts and phone.speech[-1][1] >= phone.cut_gen
                                                     and phone.speech[-1][0] > phone.cuts[-1])
                if now > phone.speech[-1][0] + args.quiet_after and not waiting_for_answer:
                    break
        feeder.cancel()
        first = phone.speech[0][0] if phone.speech else None
        result = {
            "clip": os.path.basename(path),
            "clip_ended_at": round(spoke, 3),   # on this Mac's monotonic clock, like the gateway's last_voice_at
            "first_audio_s": round(first - spoke, 3) if first else None,
            "reply_audio_s": round(len(phone.speech) * 0.02, 2),
            "events": sorted({what for _, what in phone.events}),
        }
        if talked:
            result["cut_after_talk_s"] = next((round(t - talked, 3) for t in phone.cuts if t > talked), None)
            gens = sorted({gen for _, gen, _ in phone.speech})
            result["reply_generations"] = gens
            result["answer_after_cut_s"] = next((round(t - phone.cuts[-1], 3) for t, gen, _ in phone.speech
                                                  if phone.cuts and gen >= phone.cut_gen and t > phone.cuts[-1]), None)
        if false_held:
            result["resume_after_hold_s"] = next((round(t - false_held, 3) for t in phone.resumes if t > false_held), None)
            result["audio_after_resume_s"] = round(sum(0.02 for t, _, _ in phone.speech if t > false_held), 2)
            result["cut"] = any(t > false_held for t in phone.cuts)
        if args.leak_db is not None:
            result["cuts"] = len(phone.cuts)
        if speech and phone.speech:
            result["heard"] = speech.transcribe(to_mic_rate(decode_speech([p for _, _, p in phone.speech])))
        results.append(result)
        print(json.dumps(result), flush=True)
        await phone.send_mic(np.zeros(int(16000 * args.gap), dtype=np.int16))
    if args.save_reply and phone.speech:
        audio = decode_speech([p for _, _, p in phone.speech])
        with wave.open(args.save_reply, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(audio.tobytes())
    phone.task.cancel()
    phone.writer.close()


if __name__ == "__main__":
    asyncio.run(main())
