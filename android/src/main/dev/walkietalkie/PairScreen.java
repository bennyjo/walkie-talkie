package dev.walkietalkie;

/**
 * Whether the pairing form shows. With no pairing it always does, and there is nothing to go back to.
 * With one, the form shows only after Pair again, and Cancel or Back returns to the paired screen.
 * While a Pair attempt is in flight, Pair, Cancel, and Back do nothing: the Mac may already have
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

    /** Pair and Cancel work only while no Pair attempt is in flight. */
    boolean idle() {
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

    /** Starts a Pair attempt. Returns false when one is already in flight, so no second one starts. */
    boolean pairing() {
        if (pairing) return false;
        pairing = true;
        return true;
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
