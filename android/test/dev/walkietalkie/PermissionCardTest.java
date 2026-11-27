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
        System.out.println("PermissionCardTest: " + checks + " checks passed");
    }

    private static void signedMessage() {
        check(PermissionCard.message("0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210", "allow",
                "Allow Bash?\nls -l build").equals(EXAMPLE), "the signed message matches the fixed example");
    }

    private static void answerWaitsForTheWithdrawal() {
        PermissionCard card = new PermissionCard();
        card.offer("n1");
        check(card.shown() && card.canAnswer(true, "n1"), "an offer shows, and can be answered");
        check(card.answer("n1"), "the answer is taken");
        check(card.shown(), "the card stays until the gateway withdraws the request");
        check(card.sending() && !card.canAnswer(true, "n1"), "both buttons wait while the answer is on its way");
        check(!card.answer("n1"), "a second answer to the same request is refused");
        card.offer("n1");
        check(card.sending(), "the same offer again, after a reconnect, keeps the answer");
        card.offer(null);
        check(!card.shown() && !card.sending(), "the withdrawal ends the card and the answer");
    }

    private static void answerGivenWhileTheLinkIsDown() {
        PermissionCard card = new PermissionCard();
        card.offer("n1");
        check(!card.canAnswer(false, "n1"), "no answer while the link is down");
        check(card.canAnswer(true, "n1"), "the answer opens again when the link is back");
    }

    private static void tapOnARequestTheCardDoesntShow() {
        PermissionCard card = new PermissionCard();
        card.offer("n2");
        check(!card.canAnswer(true, "n1"), "a tap on the card drawn for an earlier request answers nothing");
        check(!card.canAnswer(true, null), "a tap with no card drawn answers nothing");
        check(!card.answer("n1"), "an answer for a request no longer offered is refused");
    }

    private static void aNewOfferEndsAKeptAnswer() {
        PermissionCard card = new PermissionCard();
        card.offer("n1");
        card.answer("n1");
        card.offer("n2");
        check(!card.sending() && card.canAnswer(true, "n2"), "a new request starts with nothing answered");
    }

    private static void whatTellsTheLinkAnAnswerArrived() {
        check(PermissionCard.withdraws("n1", null), "a withdrawal means the answer arrived");
        check(PermissionCard.withdraws("n1", "n2"), "a new offer means the old answer is done with");
        check(!PermissionCard.withdraws("n1", "n1"), "the same offer again means it's still waiting, so keep it");
    }
}
