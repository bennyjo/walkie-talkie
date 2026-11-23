#!/usr/bin/env python3
"""Reads the speaker check's lines from gateway logs, to set its thresholds from your own scores.

    uv run --project gateway python gateway/tools/speaker_scores.py ~/walkie-gateway.log

For each length of speech the thresholds tell apart, it prints how many messages were scored, their
median, and the share that each threshold would have refused. Pick the highest threshold that refuses
about 2% or less of your messages of 1.5 s or more. A threshold lower than the default lets more
strangers through. Unchecked words are included, because they're mostly yours too.
"""
import json
import statistics
import sys

BANDS = (("under 1.5 s", 0, 1.5), ("1.5 s to 3 s", 1.5, 3.0), ("3 s or more", 3.0, float("inf")))
THRESHOLDS = (0.30, 0.35, 0.40, 0.42, 0.45, 0.50, 0.55, 0.60)


def scores(lines):
    """(seconds of speech, score) from each speaker line with a score."""
    found = []
    for line in lines:
        if " speaker {" not in line:
            continue
        entry = json.loads(line.split(" speaker ", 1)[1])
        if entry.get("score") is not None:
            found.append((entry["seconds"], entry["score"]))
    return found


def main(paths):
    found = []
    for path in paths:
        with open(path) as f:
            found += scores(f)
    if not found:
        sys.exit("No speaker lines with a score. Enroll, run the gateway, and save what it logs.")
    print("%-13s %5s %6s  %s" % ("speech", "count", "median", "  ".join("%.2f" % t for t in THRESHOLDS)))
    for name, low, high in BANDS:
        band = [score for seconds, score in found if low <= seconds < high]
        if band:
            refused = ["%3.0f%%" % (100 * sum(score < t for score in band) / len(band)) for t in THRESHOLDS]
            print("%-13s %5d %6.2f  %s" % (name, len(band), statistics.median(band), "  ".join(refused)))
    print("Each column is the share of messages a threshold would refuse. Defaults: 0.35, 0.42, 0.50.")


if __name__ == "__main__":
    main(sys.argv[1:] or ["/dev/stdin"])
