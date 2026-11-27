package dev.walkietalkie;

/**
 * Whether a fingerprint prompt is up, and which permission request it would answer. Each prompt has a
 * serial number that its callbacks check, because the app can close a prompt itself, and then Android
 * may never call back: a prompt the app closed counts as closed at once, and anything it sends later
 * does nothing. Plain Java, so android/build.sh --test runs it.
 */
final class FingerprintPrompt {
    private int serial;
    private boolean open;
    private String request; // the challenge of the permission request it would answer, or null for another kind

    /** A prompt opens, for this permission request or null. Returns the serial its callbacks pass back. */
    synchronized int open(String request) {
        serial++;
        open = true;
        this.request = request;
        return serial;
    }

    synchronized boolean asking() {
        return open;
    }

    /** A callback of this prompt ended it. False when it's no longer the prompt that's up: then it does nothing. */
    synchronized boolean ended(int prompt) {
        if (!open || prompt != serial) return false;
        open = false;
        request = null;
        return true;
    }

    /**
     * Whether the app closes the prompt now, because the gateway withdrew the request it would answer. A card
     * that's only hidden, such as behind a switch question, leaves its request on offer, and the prompt stays.
     */
    synchronized boolean closeIfWithdrawn(PermissionCard card) {
        if (!open || request == null || card.offers(request)) return false;
        open = false;
        request = null;
        serial++; // so a late callback of the closed prompt matches nothing
        return true;
    }
}
