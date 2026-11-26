package dev.walkietalkie;

/** Plain Java, with no Android and no test library: android/build.sh --test runs it. */
public final class OwnSoundTest {
    private static final int FRAME = 320;
    private static final int EARBUDS = 8;  // AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
    private static final int SPEAKER = 2;  // AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
    private static final long TAP_TICK_MS = 70;

    private static int checks;

    public static void main(String[] args) {
        nothingIsSilencedBeforeAnySound();
        aNoticeGoesOutAsSilenceUntilItsTailEnds();
        aLaterShortSoundDoesNotCutAnEarlierLongOne();
        silenceKeepsTheFrameLength();
        speechRightAfterATapInEarbudsIsKeptWhole();
        aTapTickOnThePhonesSpeakerIsSilenced();
        aNoticeInEarbudsIsStillSilenced();
        System.out.println("OwnSoundTest: " + checks + " checks passed");
    }

    private static void nothingIsSilencedBeforeAnySound() {
        OwnSound own = new OwnSound();
        short[] frame = voice();
        check(!own.silence(frame, FRAME, 1000), "with no sound of its own, the mic is untouched");
        check(frame[1] != 0, "the voice is still there");
    }

    private static void aNoticeGoesOutAsSilenceUntilItsTailEnds() {
        OwnSound own = new OwnSound();
        own.playing(10_000, 2_000, true, EARBUDS);
        for (long now : new long[] {10_000, 11_000, 12_000, 12_000 + OwnSound.TAIL_MS - 1}) {
            short[] frame = voice();
            check(own.silence(frame, FRAME, now), "silenced at " + now);
            check(allZero(frame, FRAME), "every sample is silence at " + now);
        }
        short[] after = voice();
        check(!own.silence(after, FRAME, 12_000 + OwnSound.TAIL_MS), "the mic is back once the tail ends");
        check(after[1] != 0, "and the voice after it goes out");
    }

    private static void aLaterShortSoundDoesNotCutAnEarlierLongOne() {
        OwnSound own = new OwnSound();
        own.playing(0, 3_000, true, EARBUDS);
        own.playing(100, 70, false, SPEAKER);
        check(own.sounding(3_000), "the long notice still counts");
    }

    private static void silenceKeepsTheFrameLength() {
        OwnSound own = new OwnSound();
        own.playing(0, 1_000, true, SPEAKER);
        short[] frame = voice();
        own.silence(frame, FRAME / 2, 0);
        check(allZero(frame, FRAME / 2), "the samples read are silence");
        check(frame[FRAME / 2 + 1] != 0, "nothing past them is touched");
    }

    private static void speechRightAfterATapInEarbudsIsKeptWhole() {
        OwnSound own = new OwnSound();
        own.playing(5_000, TAP_TICK_MS, false, EARBUDS);
        for (long now = 5_000; now < 5_000 + TAP_TICK_MS + OwnSound.TAIL_MS; now += 20) {
            short[] frame = voice();
            check(!own.silence(frame, FRAME, now), "speech after a tap in earbuds goes out at " + now);
            check(frame[1] != 0, "and it's whole at " + now);
        }
    }

    private static void aTapTickOnThePhonesSpeakerIsSilenced() {
        OwnSound own = new OwnSound();
        own.playing(5_000, TAP_TICK_MS, false, SPEAKER);
        short[] frame = voice();
        check(own.silence(frame, FRAME, 5_200), "a tick on the phone's speaker is silenced");
        check(!own.sounding(5_000 + TAP_TICK_MS + OwnSound.TAIL_MS), "only for the tick and its tail");
    }

    private static void aNoticeInEarbudsIsStillSilenced() {
        OwnSound own = new OwnSound();
        own.playing(0, 2_000, true, EARBUDS);
        check(own.silence(voice(), FRAME, 1_000), "a spoken notice is silenced on any output");
    }

    private static short[] voice() {
        short[] frame = new short[FRAME];
        for (int i = 0; i < FRAME; i++) frame[i] = (short) (8000 * Math.sin(2 * Math.PI * 300 * i / 16000.0));
        return frame;
    }

    private static boolean allZero(short[] frame, int count) {
        for (int i = 0; i < count; i++) if (frame[i] != 0) return false;
        return true;
    }

    private static void check(boolean ok, String what) {
        checks++;
        if (!ok) throw new AssertionError(what);
    }
}
