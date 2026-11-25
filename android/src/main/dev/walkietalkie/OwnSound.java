package dev.walkietalkie;

import java.util.Arrays;

/**
 * The phone's own sounds: its tones and spoken notices. The mic hears them as it hears a voice, so while one
 * may be reaching it, the mic sends silence of the same length, and the gateway sees no gap in the mic. A spoken
 * notice is silenced on any output. A tone only on the phone's speaker: in headphones it can't reach the mic,
 * and what you say straight after a tap is kept whole.
 */
final class OwnSound {
    static final long TAIL_MS = 500; // what's written can reach the speaker this much later

    private volatile long until = Long.MIN_VALUE;

    /** From the speaker thread, as it writes a sound this many milliseconds long to the output of this type. */
    void playing(long now, long ms, boolean spoken, int outputType) {
        if (spoken || outputType == MicChoice.BUILTIN_SPEAKER) until = Math.max(until, now + ms + TAIL_MS);
    }

    boolean sounding(long now) {
        return now < until;
    }

    /** Replaces the frame with silence while one of the phone's own sounds may be in it. True if it did. */
    boolean silence(short[] samples, int count, long now) {
        if (!sounding(now)) return false;
        Arrays.fill(samples, 0, count, (short) 0);
        return true;
    }
}
