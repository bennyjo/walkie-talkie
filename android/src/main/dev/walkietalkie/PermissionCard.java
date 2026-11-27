package dev.walkietalkie;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

/**
 * The permission card's state: the request the gateway offers, and the answer given here. The card stays
 * until the gateway withdraws the request, which it does once it has the answer, so an answer sent into a
 * dropped link is kept and sent again, and the card can't go while the request still waits. Plain Java,
 * so android/build.sh --test runs it.
 */
final class PermissionCard {
    private String offered;  // the challenge of the request the gateway offers, or null
    private String answered; // the challenge an answer was signed over, until the gateway withdraws it

    /** The gateway offers a request, or withdraws it with null. An answer for another challenge is done with. */
    synchronized void offer(String nonce) {
        if (nonce == null || !nonce.equals(answered)) answered = null;
        offered = nonce;
    }

    /** A fingerprint signed an answer to this challenge. False when it isn't the offer, or one was given. */
    synchronized boolean answer(String nonce) {
        if (nonce == null || !nonce.equals(offered) || answered != null) return false;
        answered = nonce;
        return true;
    }

    synchronized boolean shown() {
        return offered != null;
    }

    /** Whether this challenge is the request the gateway offers now. */
    synchronized boolean offers(String nonce) {
        return nonce != null && nonce.equals(offered);
    }

    /** An answer is on its way, so both buttons wait. */
    synchronized boolean sending() {
        return answered != null;
    }

    /**
     * Whether a tap may answer: the link is up, nothing has been answered yet, the request on the card is
     * still the one offered, so a tap can't sign a request that came in after the card was drawn, and the
     * card drew every line of it whole.
     */
    synchronized boolean canAnswer(boolean connected, String drawn, boolean whole) {
        return connected && whole && offered != null && answered == null && offered.equals(drawn);
    }

    /**
     * Whether these words drew in as many rows as they have lines. A wrapped line can hide a space where it
     * breaks, so two commands that run differently can draw the same, and then only the keyboard answers.
     */
    static boolean drawnWhole(String words, int rows) {
        return rows == words.split("\n", -1).length;
    }

    /** Whether a control from the gateway, offering this challenge or null, means a kept answer arrived. */
    static boolean withdraws(String answer, String controlNonce) {
        return controlNonce == null || !controlNonce.equals(answer);
    }

    /** What the fingerprint signs to answer a request: as security.permission_message in the gateway. */
    static String message(String nonce, String request, String decision, String text) {
        byte[] digest;
        try {
            digest = MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e); // every Java platform has SHA-256
        }
        StringBuilder hex = new StringBuilder();
        for (byte b : digest) hex.append(String.format("%02x", b & 0xff));
        return "walkie-permission|" + nonce + "|" + request + "|" + decision + "|" + hex;
    }
}
