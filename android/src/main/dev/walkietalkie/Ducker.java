package dev.walkietalkie;

/**
 * Holds audio focus, so Android lowers other apps' audio, for as long as a reply sounds or someone
 * speaks into the mic. It takes focus on the first of the two and gives it back when both have ended.
 * Called from the speaker and mic threads, and closed from the service.
 */
final class Ducker {
    /** Transient may-duck audio focus. */
    interface Focus {
        void request();

        void abandon();
    }

    private final Focus focus;
    private boolean reply;
    private boolean speech;
    private boolean held;
    private boolean closed;

    Ducker(Focus focus) {
        this.focus = focus;
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
        reply = false;
        speech = false;
        apply();
    }

    private void apply() {
        boolean want = !closed && (reply || speech);
        if (want == held) return;
        held = want;
        if (want) focus.request();
        else focus.abandon();
    }
}
