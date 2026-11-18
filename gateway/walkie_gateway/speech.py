"""Local speech: Silero for voice detection, Parakeet to transcribe, Kokoro to speak, Opus to carry it.

Every model runs on this Mac. Nothing here opens a network connection.
"""
import logging
import os

import av
import numpy as np
import sherpa_onnx

from .turns import RATE as MIC_RATE
from .turns import WINDOW

PARAKEET = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
KOKORO = "kokoro-en-v0_19"
VOICE_RATE = 24000      # Kokoro's output
OPUS_FRAME = 480        # 20 ms at the voice's rate
OPUS_BITRATE = 24000
RESET_AFTER_WINDOWS = 16  # about half a second with no voice: Silero starts afresh
SILENT = 0.01           # samples quieter than this at either end of a sentence are trimmed
TAIL_SECONDS = 0.12     # the pause kept after every unit, which is all a sentence cut at a clause gets
# How fast the voice talks, and the silence between a reply's sentences and between its paragraphs, list
# items, and lines, in seconds. The owner sets them in the talk file's front matter: (default, lowest, highest).
PACE = {
    "speed": (0.9, 0.5, 2.0),
    "sentence_pause": (0.4, 0.0, 3.0),
    "paragraph_pause": (0.9, 0.0, 5.0),
}
# The pinned Kokoro model's speakers. A voice's place here is its speaker ID in that model.
SPEAKERS = (
    ("af", "Bella and Sarah, American"),
    ("af_bella", "Bella, American"),
    ("af_nicole", "Nicole, American"),
    ("af_sarah", "Sarah, American"),
    ("af_sky", "Sky, American"),
    ("am_adam", "Adam, American"),
    ("am_michael", "Michael, American"),
    ("bf_emma", "Emma, British"),
    ("bf_isabella", "Isabella, British"),
    ("bm_george", "George, British"),
    ("bm_lewis", "Lewis, British"),
)
# The gateway's own notices, such as "Paused.", speak in this voice only. A reply can't choose it, so a reply
# that says those words never sounds like the gateway.
NOTICE_VOICE = "am_michael"
VOICES = tuple(speaker for speaker in SPEAKERS if speaker[0] != NOTICE_VOICE)  # what a reply can speak in
DEFAULT_VOICE = VOICES[0][0]


log = logging.getLogger("walkie.speech")


def default_models_dir():
    return os.environ.get("WALKIE_TALKIE_MODELS") or os.path.expanduser(
        "~/.local/share/walkie-talkie/models")


def trim_silence(samples):
    """Kokoro opens every sentence with 0.6 s of silence, which would delay every first word."""
    sounding = np.flatnonzero(np.abs(samples) > SILENT)
    if not len(sounding):
        return samples[:0]
    return samples[sounding[0]:sounding[-1] + int(TAIL_SECONDS * VOICE_RATE)]


def talk_file():
    """The owner's talk file. The mod reads its text as the spoken-style rules, and the gateway its front matter."""
    return os.path.expanduser("~/.config/walkie-talkie/talk.md")


def read_pace(text):
    """The pace from a talk file's front matter: each setting it doesn't give, or gives wrong, keeps its default."""
    pace = {key: default for key, (default, _, _) in PACE.items()}
    lines = text.lstrip("\ufeff").splitlines()
    end = next((i for i, line in enumerate(lines) if i and line.strip() == "---"), None)
    if not lines or lines[0].strip() != "---" or end is None:
        return pace  # no front matter, as the mod reads it too
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        key = key.strip().lower().replace("-", "_").replace(" ", "_")
        if key not in PACE:
            continue
        _, low, high = PACE[key]
        try:
            number = float(value.split("#")[0].strip().rstrip("s").strip())
        except ValueError:
            number = None
        if number is not None and low <= number <= high:
            pace[key] = number
        else:
            log.warning("the talk file's %s must be a number from %s to %s, so it stays at %s",
                        key, low, high, pace[key])
    return pace


class Pace:
    """The talk file's pace, read again whenever the file changes, so an edit holds from the next sentence on."""

    def __init__(self, path):
        self.path = path
        self.seen = None
        self.pace = read_pace("")

    def current(self):
        try:
            info = os.stat(self.path) if self.path else None
        except OSError:
            info = None
        seen = info and (info.st_mtime_ns, info.st_size, info.st_ino)
        if seen != self.seen:
            self.seen = seen
            try:
                with open(self.path, encoding="utf-8", errors="replace") as f:
                    text = f.read(65536)
            except (OSError, TypeError):
                text = ""
            self.pace = read_pace(text)
        return self.pace


class Speech:
    """The two heavy models, loaded once and kept resident. Each call blocks, so run them in a worker."""

    def __init__(self, models_dir, threads=4):
        parakeet = os.path.join(models_dir, PARAKEET)
        kokoro = os.path.join(models_dir, KOKORO)
        for path in (parakeet, kokoro, os.path.join(models_dir, "silero_vad.onnx")):
            if not os.path.exists(path):
                raise FileNotFoundError(
                    "Missing %s. Run gateway/fetch-models.sh, which downloads the speech models." % path)
        self.models_dir = models_dir
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=os.path.join(parakeet, "encoder.int8.onnx"),
            decoder=os.path.join(parakeet, "decoder.int8.onnx"),
            joiner=os.path.join(parakeet, "joiner.int8.onnx"),
            tokens=os.path.join(parakeet, "tokens.txt"),
            num_threads=threads,
            model_type="nemo_transducer",
        )
        self.tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=os.path.join(kokoro, "model.onnx"),
                    voices=os.path.join(kokoro, "voices.bin"),
                    tokens=os.path.join(kokoro, "tokens.txt"),
                    data_dir=os.path.join(kokoro, "espeak-ng-data"),
                ),
                num_threads=threads,
            ),
            max_num_sentences=1,
            silence_scale=1.0,
        ))
        self.transcribe(np.zeros(MIC_RATE, dtype=np.int16))  # the first call is the slow one
        self.synthesize("Ready.")

    def transcribe(self, samples):
        stream = self.recognizer.create_stream()
        stream.accept_waveform(MIC_RATE, samples.astype(np.float32) / 32768.0)
        self.recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def synthesize(self, text, voice=DEFAULT_VOICE, speed=1.0, gap=0.0):
        """int16 samples at VOICE_RATE. gap is the silence wanted since the unit before, whose tail holds some of it.

        The silence goes before the words, not after them, so a reply's last sentence ends as soon as it's said.
        """
        sid = next(i for i, (name, _) in enumerate(SPEAKERS) if name == voice)
        audio = self.tts.generate(text, sid=sid, speed=speed)
        samples = trim_silence(np.asarray(audio.samples, dtype=np.float32))
        lead = np.zeros(int(max(0.0, gap - TAIL_SECONDS) * VOICE_RATE), dtype=np.float32)
        samples = np.concatenate((lead, samples)) if len(samples) else samples
        return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)


class VoiceDetector:
    """Silero, which keeps state between windows. Light enough to run per window on the event loop.

    That state is cleared after a moment of quiet. Otherwise one utterance close to the mic can leave
    Silero deaf to a normal voice at collar distance, through Opus, for several utterances after it.
    """

    def __init__(self, models_dir):
        config = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=os.path.join(models_dir, "silero_vad.onnx"),
                threshold=0.5,
                min_silence_duration=0.1,
                min_speech_duration=0.05,
                window_size=WINDOW,
            ),
            sample_rate=MIC_RATE,
            num_threads=1,
        )
        # Holds one stretch of unbroken speech, up to Silero's 20 s default, so it never has to grow.
        self.vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)
        self.quiet_windows = 0

    def __call__(self, window):
        self.vad.accept_waveform(window.astype(np.float32) / 32768.0)
        while not self.vad.empty():
            self.vad.pop()  # finished segments: the turn detector keeps its own audio
        speech = self.vad.is_speech_detected()
        self.quiet_windows = 0 if speech else self.quiet_windows + 1
        if self.quiet_windows == RESET_AFTER_WINDOWS:
            self.vad.reset()
        return speech


class OpusDecoder:
    """One phone's microphone packets to 16 kHz samples."""

    def __init__(self):
        self.codec = av.CodecContext.create("libopus", "r")
        self.codec.layout = "mono"
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=MIC_RATE)

    def decode(self, packet):
        pieces = []
        try:
            for frame in self.codec.decode(av.Packet(packet)):
                for out in self.resampler.resample(frame):
                    pieces.append(out.to_ndarray().reshape(-1))
        except av.error.FFmpegError:
            pass  # one bad packet is a moment of silence, not a reason to drop the phone
        return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16)


class OpusEncoder:
    """The voice's samples to 20 ms Opus packets. Used from one worker at a time."""

    def __init__(self):
        self.codec = av.CodecContext.create("libopus", "w")
        self.codec.rate = VOICE_RATE
        self.codec.layout = "mono"
        self.codec.format = "s16"
        self.codec.bit_rate = OPUS_BITRATE
        self.codec.options = {"application": "voip", "frame_duration": "20"}
        self.codec.open()
        self.pts = 0

    def encode(self, samples):
        """Packets for samples, padded to a whole frame, plus enough silence to flush the encoder's delay."""
        padding = -len(samples) % OPUS_FRAME + 2 * OPUS_FRAME
        samples = np.concatenate((samples, np.zeros(padding, dtype=np.int16)))
        packets = []
        for start in range(0, len(samples), OPUS_FRAME):
            frame = av.AudioFrame.from_ndarray(
                samples[start:start + OPUS_FRAME].reshape(1, -1), format="s16", layout="mono")
            frame.rate = VOICE_RATE
            frame.pts = self.pts
            self.pts += OPUS_FRAME
            packets.extend(bytes(p) for p in self.codec.encode(frame))
        return packets
