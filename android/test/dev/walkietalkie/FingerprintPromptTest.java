package dev.walkietalkie;

/** Plain Java, with no Android and no test library: android/build.sh --test runs it. */
public final class FingerprintPromptTest {
    private static int checks;

    private static void check(boolean condition, String what) {
        checks++;
        if (!condition) throw new AssertionError(what);
    }

    public static void main(String[] args) {
        theRequestWentWhileThePromptWasOpen();
        theCancelGotNoCallback();
        theCardIsHiddenButStillOnOffer();
        aPromptForSomethingElse();
        System.out.println("FingerprintPromptTest: " + checks + " checks passed");
    }

    private static void theRequestWentWhileThePromptWasOpen() {
        PermissionCard card = new PermissionCard();
        card.offer("n1");
        FingerprintPrompt prompt = new FingerprintPrompt();
        int serial = prompt.open("n1");
        check(!prompt.closeIfWithdrawn(card), "a prompt for a request still on offer stays up");
        card.offer(null);
        check(prompt.closeIfWithdrawn(card), "the app closes the prompt once the gateway withdraws its request");
        check(!prompt.asking(), "and it counts as closed at once");
        check(!prompt.closeIfWithdrawn(card), "it closes once");
        check(!prompt.ended(serial), "a late success or error of that prompt does nothing");
    }

    // After a touch, Android may never call back for a prompt the app cancelled.
    private static void theCancelGotNoCallback() {
        PermissionCard card = new PermissionCard();
        card.offer("n1");
        FingerprintPrompt prompt = new FingerprintPrompt();
        int first = prompt.open("n1");
        card.offer("n2");
        prompt.closeIfWithdrawn(card);
        check(!prompt.asking(), "with no callback at all, a new prompt can still open");
        int second = prompt.open("n2");
        check(second != first && prompt.asking(), "the new prompt is up");
        check(!prompt.ended(first), "the old prompt's late callback can't end the new one");
        check(prompt.asking(), "so the new one is still up");
        check(prompt.ended(second), "the new one's own callback ends it");
        check(!prompt.asking(), "and then nothing is up");
    }

    private static void theCardIsHiddenButStillOnOffer() {
        PermissionCard card = new PermissionCard();
        card.offer("n1");
        FingerprintPrompt prompt = new FingerprintPrompt();
        prompt.open("n1");
        // A switch question hides the card, and the gateway still offers the request.
        check(!prompt.closeIfWithdrawn(card), "a hidden card's prompt isn't closed, and nothing says it was answered");
        check(prompt.asking(), "it stays up");
    }

    private static void aPromptForSomethingElse() {
        PermissionCard card = new PermissionCard();
        FingerprintPrompt prompt = new FingerprintPrompt();
        int serial = prompt.open(null);
        check(!prompt.closeIfWithdrawn(card), "a Start or switch prompt never closes for a permission");
        check(prompt.ended(serial) && !prompt.asking(), "its own callback ends it");
    }
}
