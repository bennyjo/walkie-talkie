package dev.walkietalkie;

/** Plain Java, with no Android and no test library: android/build.sh --test runs it. */
public final class PermissionCardTest {
    private static int checks;

    // The same fixed example the gateway's and the mod's tests check, so the three can't drift apart.
    static final String EXAMPLE = "walkie-permission|0123456789abcdef0123456789abcdef|fedcba9876543210fedcba9876543210"
            + "|allow|de6f9c5a83c5800a7ae1db3f2af48c7ed0b1e932913f12ae8760726df14b1787";

    private static void check(boolean condition, String what) {
        checks++;
        if (!condition) throw new AssertionError(what);
    }

    public static void main(String[] args) {
        signedMessage();
        answerWaitsForTheWithdrawal();
        answerGivenWhileTheLinkIsDown();
        tapOnARequestTheCardDoesntShow();
        aNewOfferEndsAKeptAnswer();
        whatTellsTheLinkAnAnswerArrived();
        onlyACommandDrawnWholeCanBeAnswered();
        System.out.println("PermissionCardTest: " + checks + " checks passed");
    }

    private static void signedMessage() {
        check(PermissionCard.message("0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210", "allow",
                "Allow Bash?\nls -l build").equals(EXAMPLE), "the signed message matches the fixed example");
    }

    private static void answerWaitsForTheWithdrawal() {
        PermissionCard card = new PermissionCard();
        card.offer("n1");
        check(card.shown() && card.canAnswer(true, "n1", true), "an offer shows, and can be answered");
        check(card.answer("n1"), "the answer is taken");
        check(card.shown(), "the card stays until the gateway withdraws the request");
        check(card.sending() && !card.canAnswer(true, "n1", true), "both buttons wait while the answer is on its way");
        check(!card.answer("n1"), "a second answer to the same request is refused");
        card.offer("n1");
        check(card.sending(), "the same offer again, after a reconnect, keeps the answer");
        card.offer(null);
        check(!card.shown() && !card.sending(), "the withdrawal ends the card and the answer");
    }

    private static void answerGivenWhileTheLinkIsDown() {
        PermissionCard card = new PermissionCard();
        card.offer("n1");
        check(!card.canAnswer(false, "n1", true), "no answer while the link is down");
        check(card.canAnswer(true, "n1", true), "the answer opens again when the link is back");
    }

    private static void tapOnARequestTheCardDoesntShow() {
        PermissionCard card = new PermissionCard();
        card.offer("n2");
        check(!card.canAnswer(true, "n1", true), "a tap on the card drawn for an earlier request answers nothing");
        check(!card.canAnswer(true, null, true), "a tap with no card drawn answers nothing");
        check(!card.answer("n1"), "an answer for a request no longer offered is refused");
    }

    private static void aNewOfferEndsAKeptAnswer() {
        PermissionCard card = new PermissionCard();
        card.offer("n1");
        card.answer("n1");
        card.offer("n2");
        check(!card.sending() && card.canAnswer(true, "n2", true), "a new request starts with nothing answered");
    }

    // The review's pairs: each draws the same on a card that wraps, so only an unwrapped card may be answered.
    private static void onlyACommandDrawnWholeCanBeAnswered() {
        String withSpace = "rm -rf /Users/benny/ tmp_build_artifacts_cache_2026";
        String withoutSpace = "rm -rf /Users/benny/tmp_build_artifacts_cache_2026";
        check(!PermissionCard.drawnWhole(withSpace, 2), "a line wrapped at its space can't be answered");
        check(!PermissionCard.drawnWhole(withoutSpace, 2), "nor its twin, wrapped after the slash");
        check(PermissionCard.drawnWhole(withSpace, 1), "the same line drawn in one row can");
        // The final review's command: a line break draws just like the wrap above, but deletes the home folder.
        check(!PermissionCard.drawnWhole("rm -rf /Users/benny/\ntmp_build_artifacts_cache_2026", 2),
                "a command with a line break can't be answered, though each line fits its row");
        check(!PermissionCard.drawnWhole("echo one\necho two", 2), "nor any two lines in two rows");
        PermissionCard card = new PermissionCard();
        card.offer("n1");
        check(!card.canAnswer(true, "n1", false), "both buttons stay off for a card that wrapped a line");
        check(card.canAnswer(true, "n1", true), "and come on once it drew whole");
        check(card.offers("n1") && !card.offers("n2") && !card.offers(null), "only the offered request is offered");
    }

    private static void whatTellsTheLinkAnAnswerArrived() {
        check(PermissionCard.withdraws("n1", null), "a withdrawal means the answer arrived");
        check(PermissionCard.withdraws("n1", "n2"), "a new offer means the old answer is done with");
        check(!PermissionCard.withdraws("n1", "n1"), "the same offer again means it's still waiting, so keep it");
    }
}
