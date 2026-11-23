package dev.walkietalkie;

/** Plain Java, with no Android and no test library: android/build.sh --test runs it. */
public final class DuckerTest {
    private static int checks;

    private static final class Fake implements Ducker.Focus {
        int requests;
        int abandons;

        @Override public void request() {
            requests++;
        }

        @Override public void abandon() {
            abandons++;
        }

        boolean held() {
            return requests > abandons;
        }
    }

    private static final class Clock {
        long now;
    }

    private final Fake f = new Fake();
    private final Clock clock = new Clock();
    private final Ducker d = new Ducker(f, () -> clock.now);

    /** One frame of silence on both threads, {@code ms} after the last. */
    private void quietFor(long ms) {
        clock.now += ms;
        d.setReply(false);
        d.setSpeech(false);
    }

    public static void main(String[] args) {
        new DuckerTest().nothingIsRequestedWhileQuiet();
        new DuckerTest().aReplyHoldsFocusOnceAndReleasesItAfterTheHold();
        new DuckerTest().speechHoldsFocusOnItsOwn();
        new DuckerTest().focusSurvivesOneEndingWhileTheOtherContinues();
        new DuckerTest().repeatedSignalsDoNotRequestAgain();
        new DuckerTest().aGapShorterThanTheHoldKeepsFocus();
        new DuckerTest().aGapLongerThanTheHoldReleasesFocus();
        new DuckerTest().closeReleasesWhateverIsHeldAndStaysReleased();
        new DuckerTest().closeDuringTheHoldReleasesAtOnce();
        new DuckerTest().closeWhileQuietDoesNothing();
        System.out.println("DuckerTest: " + checks + " checks passed");
    }

    private void nothingIsRequestedWhileQuiet() {
        quietFor(0);
        quietFor(Ducker.HOLD_MS * 2);
        check(f.requests == 0 && f.abandons == 0, "quiet asks for nothing");
    }

    private void aReplyHoldsFocusOnceAndReleasesItAfterTheHold() {
        d.setReply(true);
        check(f.requests == 1 && f.held(), "a reply takes focus");
        quietFor(0);
        check(f.held(), "the end of the reply keeps focus for the hold");
        quietFor(Ducker.HOLD_MS);
        check(f.abandons == 1 && !f.held(), "the hold ending gives it back");
    }

    private void speechHoldsFocusOnItsOwn() {
        d.setSpeech(true);
        check(f.held(), "speech takes focus");
        quietFor(0);
        quietFor(Ducker.HOLD_MS);
        check(!f.held() && f.requests == 1 && f.abandons == 1, "the end of speech gives it back after the hold");
    }

    private void focusSurvivesOneEndingWhileTheOtherContinues() {
        d.setReply(true);
        d.setSpeech(true);
        for (int i = 0; i < 10; i++) {
            clock.now += Ducker.HOLD_MS;
            d.setReply(false);
            d.setSpeech(true);
        }
        check(f.held() && f.requests == 1, "speech long after the end of the reply keeps focus, with no second request");
        quietFor(0);
        quietFor(Ducker.HOLD_MS);
        check(!f.held() && f.abandons == 1, "focus goes when both have been quiet for the hold");
    }

    private void repeatedSignalsDoNotRequestAgain() {
        for (int i = 0; i < 100; i++) d.setSpeech(true);
        check(f.requests == 1, "100 frames of speech make one request, requests: " + f.requests);
        for (int i = 0; i < 100; i++) quietFor(Ducker.HOLD_MS);
        check(f.abandons == 1, "and one release, abandons: " + f.abandons);
    }

    private void aGapShorterThanTheHoldKeepsFocus() {
        d.setSpeech(true);
        for (long t = 0; t < Ducker.HOLD_MS - 20; t += 20) quietFor(20);
        check(f.held(), "a pause just under the hold between speech and the reply keeps focus");
        clock.now += 20;
        d.setReply(true);
        check(f.held() && f.requests == 1 && f.abandons == 0, "the reply arrives with no release and no second request");
    }

    private void aGapLongerThanTheHoldReleasesFocus() {
        d.setReply(true);
        for (long t = 0; t < Ducker.HOLD_MS; t += 20) quietFor(20);
        check(!f.held() && f.abandons == 1, "a pause of the full hold gives focus back");
        d.setReply(true);
        check(f.held() && f.requests == 2, "the next reply takes it again");
    }

    private void closeReleasesWhateverIsHeldAndStaysReleased() {
        d.setReply(true);
        d.setSpeech(true);
        d.close();
        check(!f.held() && f.abandons == 1, "close gives focus back mid reply");
        d.setReply(true);
        d.setSpeech(true);
        check(!f.held() && f.requests == 1, "a late signal after close takes nothing");
    }

    private void closeDuringTheHoldReleasesAtOnce() {
        d.setReply(true);
        quietFor(0);
        d.close();
        check(!f.held() && f.abandons == 1, "close during the hold gives focus back without waiting");
    }

    private void closeWhileQuietDoesNothing() {
        d.close();
        check(f.requests == 0 && f.abandons == 0, "close with nothing held abandons nothing");
    }

    private static void check(boolean ok, String what) {
        checks++;
        if (!ok) throw new AssertionError(what);
    }
}
