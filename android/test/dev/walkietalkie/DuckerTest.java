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

    public static void main(String[] args) {
        nothingIsRequestedWhileQuiet();
        aReplyHoldsFocusOnceAndReleasesItAtTheEnd();
        speechHoldsFocusOnItsOwn();
        focusSurvivesOneEndingWhileTheOtherContinues();
        repeatedSignalsDoNotRequestAgain();
        closeReleasesWhateverIsHeldAndStaysReleased();
        closeWhileQuietDoesNothing();
        System.out.println("DuckerTest: " + checks + " checks passed");
    }

    private static void nothingIsRequestedWhileQuiet() {
        Fake f = new Fake();
        Ducker d = new Ducker(f);
        d.setReply(false);
        d.setSpeech(false);
        check(f.requests == 0 && f.abandons == 0, "quiet asks for nothing");
    }

    private static void aReplyHoldsFocusOnceAndReleasesItAtTheEnd() {
        Fake f = new Fake();
        Ducker d = new Ducker(f);
        d.setReply(true);
        check(f.requests == 1 && f.held(), "a reply takes focus");
        d.setReply(false);
        check(f.abandons == 1 && !f.held(), "the end of the reply gives it back");
    }

    private static void speechHoldsFocusOnItsOwn() {
        Fake f = new Fake();
        Ducker d = new Ducker(f);
        d.setSpeech(true);
        check(f.held(), "speech takes focus");
        d.setSpeech(false);
        check(!f.held() && f.requests == 1 && f.abandons == 1, "the end of speech gives it back");
    }

    private static void focusSurvivesOneEndingWhileTheOtherContinues() {
        Fake f = new Fake();
        Ducker d = new Ducker(f);
        d.setReply(true);
        d.setSpeech(true);
        d.setReply(false);
        check(f.held() && f.requests == 1, "speech over the end of the reply keeps focus, with no second request");
        d.setSpeech(false);
        check(!f.held() && f.abandons == 1, "focus goes when both have ended");
    }

    private static void repeatedSignalsDoNotRequestAgain() {
        Fake f = new Fake();
        Ducker d = new Ducker(f);
        for (int i = 0; i < 100; i++) d.setSpeech(true);
        check(f.requests == 1, "100 frames of speech make one request, requests: " + f.requests);
        for (int i = 0; i < 100; i++) d.setSpeech(false);
        check(f.abandons == 1, "and one release, abandons: " + f.abandons);
    }

    private static void closeReleasesWhateverIsHeldAndStaysReleased() {
        Fake f = new Fake();
        Ducker d = new Ducker(f);
        d.setReply(true);
        d.setSpeech(true);
        d.close();
        check(!f.held() && f.abandons == 1, "close gives focus back mid reply");
        d.setReply(true);
        d.setSpeech(true);
        check(!f.held() && f.requests == 1, "a late signal after close takes nothing");
    }

    private static void closeWhileQuietDoesNothing() {
        Fake f = new Fake();
        new Ducker(f).close();
        check(f.requests == 0 && f.abandons == 0, "close with nothing held abandons nothing");
    }

    private static void check(boolean ok, String what) {
        checks++;
        if (!ok) throw new AssertionError(what);
    }
}
