package dev.walkietalkie;

import java.io.IOException;

/** Plain Java, with no Android and no test library: android/build.sh --test runs it. */
public final class PairSwapTest {
    private static int checks;

    /** What the phone keeps: the pairing, and which keys exist. */
    private static final class Phone implements PairSwap.Vault {
        String host;
        String token;
        String keyOfPairing;
        final java.util.Set<String> keys = new java.util.HashSet<>();
        private int made;
        private String staged;
        boolean saveFails;

        Phone pairedTo(String host, String token) {
            this.host = host;
            this.token = token;
            keyOfPairing = "key-old";
            keys.add(keyOfPairing);
            return this;
        }

        boolean paired() {
            return host != null;
        }

        @Override public String stage() {
            staged = "key-new-" + (++made);
            keys.add(staged);
            return staged;
        }

        @Override public void commit(String host, String token) throws IOException {
            if (saveFails) throw new IOException("disk");
            this.host = host;
            this.token = token;
            keys.remove(keyOfPairing);
            keyOfPairing = staged;
        }

        @Override public void discard() {
            keys.remove(staged);
        }
    }

    public static void main(String[] args) throws Exception {
        pairAgainChangesNothingStored();
        cancelAndBackLeaveTheStoredPairingAlone();
        firstRunHasNoCancelAndBackIsLeftToTheSystem();
        cancelAndBackWaitForAnInFlightPair();
        aSuccessfulPairReplacesThePairingAndItsKey();
        aRefusedPairKeepsTheOldPairingAndKey();
        aFailedSaveKeepsTheOldPairingAndKey();
        aFirstPairingNeedsNothingToReplace();
        System.out.println("PairSwapTest: " + checks + " checks passed");
    }

    private static void pairAgainChangesNothingStored() {
        Phone phone = new Phone().pairedTo("100.64.0.1", "old-token");
        PairScreen screen = new PairScreen();
        check(!screen.formShown(phone.paired()), "the paired screen shows first");
        check(!screen.cancelShown(phone.paired()), "no Cancel on the paired screen");
        screen.pairAgain();
        check(screen.formShown(phone.paired()), "Pair again opens the form");
        check(screen.cancelShown(phone.paired()), "the form has Cancel when there is a pairing");
        check("100.64.0.1".equals(phone.host) && "old-token".equals(phone.token), "Pair again keeps the pairing");
        check(phone.keys.contains("key-old"), "Pair again keeps the key");
    }

    private static void cancelAndBackLeaveTheStoredPairingAlone() {
        Phone phone = new Phone().pairedTo("100.64.0.1", "old-token");
        PairScreen screen = new PairScreen();
        screen.pairAgain();
        screen.cancel();
        check(!screen.formShown(phone.paired()), "Cancel returns to the paired screen");
        check("100.64.0.1".equals(phone.host) && "old-token".equals(phone.token) && phone.keys.contains("key-old"),
                "Cancel keeps the pairing and key");
        screen.pairAgain();
        check(screen.back(phone.paired()), "Back is taken by the form");
        check(!screen.formShown(phone.paired()), "Back returns to the paired screen");
        check("100.64.0.1".equals(phone.host) && "old-token".equals(phone.token) && phone.keys.contains("key-old"),
                "Back keeps the pairing and key");
        check(!screen.back(phone.paired()), "Back on the paired screen is the system's");
    }

    private static void firstRunHasNoCancelAndBackIsLeftToTheSystem() {
        Phone phone = new Phone();
        PairScreen screen = new PairScreen();
        check(screen.formShown(phone.paired()), "first run shows the form");
        check(!screen.cancelShown(phone.paired()), "first run has no Cancel");
        check(!screen.back(phone.paired()), "first run Back is the system's");
        screen.pairAgain();
        check(!screen.cancelShown(phone.paired()), "still no Cancel with nothing to return to");
    }

    private static void cancelAndBackWaitForAnInFlightPair() throws Exception {
        Phone phone = new Phone().pairedTo("100.64.0.1", "old-token");
        PairScreen screen = new PairScreen();
        screen.pairAgain();
        screen.pairing();
        boolean threw = false;
        try {
            PairSwap.run(phone, "100.64.0.2", key -> {
                check(!screen.cancelEnabled(), "Cancel is disabled while the Mac decides");
                check(!screen.cancel(), "Cancel does nothing while the Mac decides");
                check(screen.cancelShown(phone.paired()), "Back stays with the form while the Mac decides");
                check(!screen.back(phone.paired()), "Back does nothing while the Mac decides");
                check(screen.formShown(phone.paired()), "the form stays open while the Mac decides");
                throw new IOException("wrong code");
            });
        } catch (IOException e) {
            threw = true;
        }
        check(threw, "the failure reaches the screen");
        screen.failed();
        check(screen.cancelEnabled(), "a failed pair enables Cancel again");
        check(screen.formShown(phone.paired()), "a failed pair stays on the form");
        check(screen.back(phone.paired()), "after a failed pair Back closes the form");
        check(!screen.formShown(phone.paired()), "Back returns to the paired screen");
        check("100.64.0.1".equals(phone.host) && "old-token".equals(phone.token) && phone.keys.contains("key-old"),
                "the old pairing and key stay");

        screen.pairAgain();
        screen.pairing();
        PairSwap.run(phone, "100.64.0.2", key -> {
            check(!screen.cancel() && !screen.back(phone.paired()), "Cancel and Back wait for the Mac");
            return "new-token";
        });
        screen.paired();
        check("100.64.0.2".equals(phone.host) && "new-token".equals(phone.token), "the accepted pairing lands");
        check(!screen.formShown(phone.paired()) && screen.cancelEnabled(), "the form closes with Cancel enabled again");
    }

    private static void aSuccessfulPairReplacesThePairingAndItsKey() throws Exception {
        Phone phone = new Phone().pairedTo("100.64.0.1", "old-token");
        PairScreen screen = new PairScreen();
        screen.pairAgain();
        PairSwap.run(phone, "100.64.0.2", key -> {
            check(phone.keys.contains("key-old") && phone.keys.contains(key), "the old key works while the Mac decides");
            return "new-token";
        });
        screen.paired();
        check("100.64.0.2".equals(phone.host) && "new-token".equals(phone.token), "the new pairing replaces the old");
        check(!phone.keys.contains("key-old"), "the old key is gone");
        check(phone.keys.size() == 1 && phone.keys.contains(phone.keyOfPairing), "only the new pairing's key is left");
        check(!screen.formShown(phone.paired()), "a successful pair returns to the paired screen");
    }

    private static void aRefusedPairKeepsTheOldPairingAndKey() {
        Phone phone = new Phone().pairedTo("100.64.0.1", "old-token");
        boolean threw = false;
        try {
            PairSwap.run(phone, "100.64.0.2", key -> {
                throw new IOException("wrong code");
            });
        } catch (Exception e) {
            threw = true;
        }
        check(threw, "the failure reaches the screen");
        check("100.64.0.1".equals(phone.host) && "old-token".equals(phone.token), "a refused pair keeps the old pairing");
        check(phone.keys.size() == 1 && phone.keys.contains("key-old"), "a refused pair keeps the old key and drops the new");
    }

    private static void aFailedSaveKeepsTheOldPairingAndKey() {
        Phone phone = new Phone().pairedTo("100.64.0.1", "old-token");
        phone.saveFails = true;
        boolean threw = false;
        try {
            PairSwap.run(phone, "100.64.0.2", key -> "new-token");
        } catch (Exception e) {
            threw = true;
        }
        check(threw, "the save failure reaches the screen");
        check("100.64.0.1".equals(phone.host) && "old-token".equals(phone.token), "a failed save keeps the old pairing");
        check(phone.keys.size() == 1 && phone.keys.contains("key-old"), "a failed save keeps the old key and drops the new");
    }

    private static void aFirstPairingNeedsNothingToReplace() throws Exception {
        Phone phone = new Phone();
        PairSwap.run(phone, "100.64.0.2", key -> "new-token");
        check(phone.paired() && "new-token".equals(phone.token), "a first pairing is saved");
        check(phone.keys.size() == 1, "with its one key");
    }

    private static void check(boolean ok, String what) {
        checks++;
        if (!ok) throw new AssertionError(what);
    }
}
