package dev.walkietalkie;

/**
 * Whether the pairing form shows. With no pairing it always does, and there is nothing to go back to.
 * With one, the form shows only after Pair again, and Cancel or Back returns to the paired screen.
 * While a Pair attempt is in flight, Cancel and Back stay on the form: the Mac may already have
 * replaced its key for this phone, so the attempt's result has to land.
 * Plain Java, so android/build.sh --test runs it.
 */
final class PairScreen {
    private boolean again;
    private boolean pairing;

    boolean formShown(boolean paired) {
        return !paired || again;
    }

    /** Cancel shows only where there is a pairing to return to. */
    boolean cancelShown(boolean paired) {
        return paired && again;
    }

    /** Cancel works only while no Pair attempt is in flight. */
    boolean cancelEnabled() {
        return !pairing;
    }

    void pairAgain() {
        again = true;
    }

    /** Returns true when it closed the form, false while a Pair attempt is in flight. */
    boolean cancel() {
        if (pairing) return false;
        again = false;
        return true;
    }

    /**
     * The system Back action. Returns true when it closed the form. Back belongs to the form whenever
     * Cancel shows, so while a Pair attempt is in flight it does nothing.
     */
    boolean back(boolean paired) {
        return cancelShown(paired) && cancel();
    }

    /** A Pair attempt has started. */
    void pairing() {
        pairing = true;
    }

    /** The Pair attempt failed, and the old pairing stays. */
    void failed() {
        pairing = false;
    }

    /** A new pairing has taken over: the form is done. */
    void paired() {
        pairing = false;
        again = false;
    }
}
