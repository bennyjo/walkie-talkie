package dev.walkietalkie;

/**
 * Replaces a pairing without ever leaving the phone with none. The new key is made beside the old one,
 * and only a pairing the Mac accepted takes the old one's place. Anything that fails on the way leaves
 * the old pairing and its key as they were. Plain Java, so android/build.sh --test runs it.
 */
final class PairSwap {
    /** Where the pairing and its key live. */
    interface Vault {
        /** Makes a new key beside the current one and returns its public half. */
        String stage() throws Exception;

        /** Saves the host and token, makes the staged key the current one, and drops the old key. */
        void commit(String host, String token) throws Exception;

        /** Drops the staged key. The current pairing and key stay. */
        void discard();
    }

    /** The Mac's side: turns the new key's public half and the typed code into the token. */
    interface Mac {
        String pair(String publicKey) throws Exception;
    }

    private PairSwap() { }

    static void run(Vault vault, String host, Mac mac) throws Exception {
        String publicKey = vault.stage();
        try {
            vault.commit(host, mac.pair(publicKey));
        } catch (Exception | Error e) {
            vault.discard();
            throw e;
        }
    }
}
