"""The owner's voiceprint, for the speaker check.

`walkie-gateway enroll` makes it at the Mac: the owner reads PASSAGE, one part at a time, into the
collar mic with its receiver plugged into the Mac. The six parts' voiceprints are averaged, and each
must score at least MIN_AGREEMENT against the average, so a cough or a second voice can't spoil it.
The recordings stay in memory and are gone when the command ends.

The voiceprint lives in the gateway's private folder, next to the sha256 of the model that made it,
because voiceprints from two models can't be compared. It never goes to the phone or a session.
It's biometric data, so revoking the phone leaves it alone, and `enroll --remove` deletes it.
"""
import hashlib
import json
import os
import threading
import time

import av
import av.logging
import numpy as np

from . import security
from .speech import SPEAKER_MODEL, SpeakerCheck
from .turns import RATE

FILE = "voiceprint.json"
MIN_AGREEMENT = 0.6     # each part against the average of all six
MIN_PART_SECONDS = 2.0  # anything shorter was cut off before it was read
MIN_PEAK = 330          # about -40 dB under full scale: quieter than this, the input heard nothing
PASSAGE = (
    ("quietly", "Walkie Talkie, this is my voice. I'm reading this so the gateway can learn how I sound, "
                "close to the microphone and far from it, quietly and out loud."),
    ("normally", "Yes. No. Go. Stop. Cancel. Pause it. Start. Read it again. Switch to the main session."),
    ("normally", "Please check the build logs, and tell me which test failed and what the error says."),
    ("loudly", "The quick brown fox jumps over the lazy dog, while five boxing wizards jump quickly."),
    ("normally", "On Thursday the twelfth of March, at a quarter past seven, it was cold and windy by the water."),
    ("normally", "If something looks risky, ask me first, and wait until I say yes before you carry on."),
)


class VoiceprintError(RuntimeError):
    pass


def path(state_dir):
    return os.path.join(state_dir, FILE)


def model_sha256(models_dir):
    model = os.path.join(models_dir, SPEAKER_MODEL)
    try:
        with open(model, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except FileNotFoundError:
        raise FileNotFoundError(
            "Missing %s. Run gateway/fetch-models.sh, which downloads the speaker model." % model) from None


def load(state_dir, models_dir):
    """The owner's voiceprint, or None if nobody has enrolled. One from another model, or unreadable, is an error."""
    where = path(state_dir)
    try:
        with open(where) as f:
            saved = json.load(f)
        vector = np.asarray(saved["voiceprint"], dtype=np.float32)
        made_with = saved["model_sha256"]
        if vector.ndim != 1 or not len(vector) or not np.isfinite(vector).all():
            raise ValueError("it holds no voiceprint")
    except FileNotFoundError:
        return None
    except (OSError, KeyError, TypeError, ValueError) as e:
        raise VoiceprintError("Can't read the voiceprint in %s: %s. Enroll again with walkie-gateway enroll, "
                              "or delete it with walkie-gateway enroll --remove." % (where, e)) from e
    if made_with != model_sha256(models_dir):
        raise VoiceprintError("The voiceprint in %s was made with another speaker model. Enroll again with "
                              "walkie-gateway enroll, or delete it with walkie-gateway enroll --remove." % where)
    return vector / (np.linalg.norm(vector) or 1.0)


def save(state_dir, vector, sha256):
    security.write_private(path(state_dir), json.dumps({
        "model": SPEAKER_MODEL,
        "model_sha256": sha256,
        "voiceprint": [round(float(x), 6) for x in vector],
    }) + "\n")


def remove(state_dir):
    """True if there was a voiceprint to delete."""
    try:
        os.unlink(path(state_dir))
        return True
    except FileNotFoundError:
        return False


def combine(parts):
    """The average of the parts' unit voiceprints, as a unit vector, and how each part scores against it."""
    vector = np.mean(parts, axis=0)
    vector = vector / (np.linalg.norm(vector) or 1.0)
    return vector, [float(part @ vector) for part in parts]


def part_problem(samples):
    """Why a recording can't be a part of the passage, or None."""
    if len(samples) < MIN_PART_SECONDS * RATE:
        return "That was too short. Press Enter only after you've read the whole part."
    if np.abs(samples.astype(np.int32)).max() < MIN_PEAK:
        return ("That input heard nothing. Check that you chose the collar mic's receiver, and that this terminal "
                "may use the microphone, in System Settings, Privacy & Security, Microphone.")
    return None


def enroll(state_dir, models_dir, record, say=print, checker=None):
    """Records the passage with record(number, how, text) -> int16 samples, and saves the voiceprint.

    A part that's too short, silent, or unlike the rest is read again.
    """
    sha256 = model_sha256(models_dir)
    checker = checker or SpeakerCheck(models_dir)
    say("Read each part at your usual pace, wearing the collar mic as on a walk. Only your voice should be heard.")
    parts = [None] * len(PASSAGE)
    todo = list(range(len(PASSAGE)))
    while todo:
        for i in todo:
            how, text = PASSAGE[i]
            while True:
                samples = record(i + 1, how, text)
                problem = part_problem(samples)
                if not problem:
                    break
                say(problem)
            parts[i] = checker.embed(samples)
        vector, agreement = combine(parts)
        todo = [i for i, score in enumerate(agreement) if score < MIN_AGREEMENT]
        for i in todo:
            say("Part %d doesn't sound like the others (%.2f, and it needs %.2f). Read it again, with no other voice "
                "or noise near the mic." % (i + 1, agreement[i], MIN_AGREEMENT))
    security.private_dir(state_dir)
    save(state_dir, vector, sha256)
    say("Saved your voiceprint in %s. Each part scored %.2f to %.2f against it. The recordings were kept only in "
        "memory, and they're gone." % (path(state_dir), min(agreement), max(agreement)))
    say("Restart the gateway to use it. Until you add --enforce-speaker, it only logs how each message scores.")
    return vector


# --- recording at the Mac ----------------------------------------------------------

def audio_inputs():
    """The Mac's audio inputs, as (number, name)."""
    av.logging.set_level(av.logging.INFO)
    with av.logging.Capture(True) as logs:
        try:
            av.open("", format="avfoundation", options={"list_devices": "true"})
        except OSError:
            pass  # listing the devices is all it does: it opens nothing
    inputs, audio = [], False
    for _, _, line in logs:
        line = line.strip()
        if line.startswith("AVFoundation"):
            audio = "audio devices" in line
        elif audio and line.startswith("[") and "]" in line:
            number, name = line[1:].split("]", 1)
            inputs.append((number, name.strip()))
    return inputs


def record_until(device, stop, started):
    """int16 samples at the microphone's rate from a Mac audio input, until stop is set. started is set once it's open."""
    container = av.open(":" + device, format="avfoundation")
    started.set()
    try:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        pieces = []
        while not stop.is_set():
            try:
                packet = next(container.demux(stream))
            except BlockingIOError:  # nothing captured yet
                time.sleep(0.01)
                continue
            for frame in packet.decode():
                pieces += [out.to_ndarray().reshape(-1) for out in resampler.resample(frame)]
    finally:
        container.close()
    return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16)


def recorder(device, ask=input, say=print):
    """record() for enroll(): shows the part, and records from Enter to Enter."""
    def record(number, how, text):
        say("\nPart %d of %d, read %s:\n\n  %s\n" % (number, len(PASSAGE), how, text))
        ask("Press Enter, then read it. ")
        stop, started = threading.Event(), threading.Event()
        result = {}

        def run():
            try:
                result["samples"] = record_until(device, stop, started)
            except Exception as e:  # shown to the owner below
                result["error"] = e
            started.set()
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        started.wait()  # so the first words aren't lost while the input opens
        if "error" not in result:
            ask("Recording. Press Enter when you've finished. ")
        stop.set()
        worker.join()
        if "error" in result:
            raise VoiceprintError("Can't record from input %s: %s" % (device, result["error"]))
        return result["samples"]
    return record


def choose_input(ask=input, say=print):
    inputs = audio_inputs()
    if not inputs:
        raise VoiceprintError("This Mac has no audio input. Plug in the collar mic's receiver.")
    say("Audio inputs on this Mac:")
    for number, name in inputs:
        say("  %s  %s" % (number, name))
    numbers = [number for number, _ in inputs]
    while True:
        chosen = ask("Which is the collar mic's receiver? Type its number: ").strip()
        if chosen in numbers:
            return chosen
        say("That isn't one of the numbers above.")
