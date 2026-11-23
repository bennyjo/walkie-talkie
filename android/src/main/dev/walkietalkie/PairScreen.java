package dev.walkietalkie;

/**
 * Whether the pairing form shows. With no pairing it always does, and there is nothing to go back to.
 * With one, the form shows only after Pair again, and Cancel or Back returns to the paired screen.
 * Plain Java, so android/build.sh --test runs it.
 */
final class PairScreen {
    private boolean again;

    boolean formShown(boolean paired) {
        return !paired || again;
    }

    /** Cancel shows only where there is a pairing to return to. */
    boolean cancelShown(boolean paired) {
        return paired && again;
    }

    void pairAgain() {
        again = true;
    }

    void cancel() {
        again = false;
    }

    /** The system Back action. Returns true when it closed the form, false when it's for the system to handle. */
    boolean back(boolean paired) {
        if (!cancelShown(paired)) return false;
        again = false;
        return true;
    }

    /** A new pairing has taken over: the form is done. */
    void paired() {
        again = false;
    }
}
