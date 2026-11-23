package dev.walkietalkie;

import java.util.function.LongSupplier;

/**
 * Holds audio focus, so Android lowers other apps' audio, for as long as a reply sounds or someone
 * speaks into the mic. It takes focus on the first of the two and gives it back once both have been
 * quiet for {@link #HOLD_MS}, so a pause between sentences or before a reply doesn't bring the music up.
 * Called every frame from the speaker and mic threads, and closed from the service.
 */
final class Ducker {
    static final long HOLD_MS = 2000;

    /** Transient may-duck audio focus. */
    interface Focus {
        void request();

        void abandon();
    }

    private final Focus focus;
    private final LongSupplier clock;
    private boolean reply;
    private boolean speech;
    private long activeAt;
    private boolean held;
    private boolean closed;

    Ducker(Focus focus, LongSupplier clock) {
        this.focus = focus;
        this.clock = clock;
    }

    synchronized void setReply(boolean sounding) {
        reply = sounding;
        apply();
    }

    synchronized void setSpeech(boolean speaking) {
        speech = speaking;
        apply();
    }

    /** Gives focus back for good, whatever the state, so nothing stays lowered. */
    synchronized void close() {
        closed = true;
        if (held) {
            held = false;
            focus.abandon();
        }
    }

    private void apply() {
        if (closed) return;
        long now = clock.getAsLong();
        if (reply || speech) activeAt = now;
        boolean want = reply || speech || (held && now - activeAt < HOLD_MS);
        if (want == held) return;
        held = want;
        if (want) focus.request();
        else focus.abandon();
    }
}
