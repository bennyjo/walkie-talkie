package dev.walkietalkie;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;

/** Plain Java, with no Android and no test library: android/build.sh --test runs it. Also covers Wav. */
public final class MicChoiceTest {
    private static int checks;

    // AudioDeviceInfo's types.
    private static final int SPEAKER = 2;
    private static final int SCO = 7;
    private static final int A2DP = 8;
    private static final int USB = 11;
    private static final int PHONE_MIC = 15;
    private static final int BLE = 26;

    private static final int[] NONE = {};
    private static final int[] PHONE_ONLY_IN = {PHONE_MIC};
    private static final int[] PHONE_ONLY_OUT = {SPEAKER};
    private static final int[] WITH_USB_IN = {PHONE_MIC, USB};
    private static final int[] WITH_HEADSET_IN = {PHONE_MIC, SCO};
    private static final int[] WITH_EARBUDS_OUT = {SPEAKER, A2DP};
    private static final int[] HEADSET_CALL = {1, SPEAKER, SCO}; // earpiece, speaker, the headset

    public static void main(String[] args) {
        usbMicIsTheDefault();
        onlyConnectedMicsAreOffered();
        usbMicSendsOnlyTheUsbRouteAndPlaysOnlyInHeadphones();
        headsetMicTakesCallModeOnlyWithAHeadset();
        headsetMicPrefersLeAudio();
        phoneMicWithHeadphonesKeepsMediaAndTalkOver();
        phoneMicWithoutHeadphonesPlaysOnTheSpeakerWithoutTalkOver();
        aRouteThatOutlivesItsDeviceDoesNotCount();
        wavIsReadResampledAndScaled();
        wavWithAnUnfinishedHeaderReadsToTheEnd();
        notWavIsRefused();
        System.out.println("MicChoiceTest: " + checks + " checks passed");
    }

    private static void usbMicIsTheDefault() {
        check(MicChoice.DEFAULT == MicChoice.USB, "USB is the default");
        check(MicChoice.valid(-1) == MicChoice.USB && MicChoice.valid(9) == MicChoice.USB, "an unknown choice is USB");
        check(MicChoice.valid(MicChoice.PHONE) == MicChoice.PHONE, "a known choice stays");
    }

    private static void onlyConnectedMicsAreOffered() {
        int[] phone = {MicChoice.PHONE};
        check(Arrays.equals(MicChoice.offered(PHONE_ONLY_IN, new int[] {1, SPEAKER}), phone),
                "with nothing connected, only the phone's mic");
        check(Arrays.equals(MicChoice.offered(WITH_HEADSET_IN, HEADSET_CALL),
                new int[] {MicChoice.HEADSET, MicChoice.PHONE}), "headphones with a mic and no USB mic");
        check(Arrays.equals(MicChoice.offered(PHONE_ONLY_IN, new int[] {1, SPEAKER, BLE}),
                new int[] {MicChoice.HEADSET, MicChoice.PHONE}), "LE Audio headphones count as a headset");
        check(Arrays.equals(MicChoice.offered(WITH_USB_IN, new int[] {1, SPEAKER}),
                new int[] {MicChoice.USB, MicChoice.PHONE}), "a USB mic with headphones that have no mic");
        check(Arrays.equals(MicChoice.offered(new int[] {PHONE_MIC, 22}, HEADSET_CALL),
                new int[] {MicChoice.USB, MicChoice.HEADSET, MicChoice.PHONE}), "a USB headset and headphones: all three");
        check(MicChoice.offered(NONE, NONE).length == 1, "the phone's mic is offered even unlisted");
    }

    private static void usbMicSendsOnlyTheUsbRouteAndPlaysOnlyInHeadphones() {
        MicChoice.Plan plan = MicChoice.plan(MicChoice.USB, WITH_USB_IN, WITH_EARBUDS_OUT, HEADSET_CALL);
        check(!plan.call && plan.talkOver && !plan.onSpeaker(), "USB is media, with talk-over");
        check(MicChoice.allowed(plan.mics, USB, WITH_USB_IN), "the USB route is sent");
        check(!MicChoice.allowed(plan.mics, PHONE_MIC, WITH_USB_IN), "the phone's mic isn't sent");
        check(MicChoice.allowed(plan.outputs, A2DP, WITH_EARBUDS_OUT), "replies play in the earbuds");
        check(!MicChoice.allowed(plan.outputs, SPEAKER, WITH_EARBUDS_OUT), "never on the speaker");

        MicChoice.Plan bare = MicChoice.plan(MicChoice.USB, PHONE_ONLY_IN, PHONE_ONLY_OUT, NONE);
        check(!MicChoice.allowed(bare.mics, PHONE_MIC, PHONE_ONLY_IN), "no USB mic: nothing is sent");
        check(!MicChoice.allowed(bare.outputs, SPEAKER, PHONE_ONLY_OUT), "no earbuds: replies are silent");
    }

    private static void headsetMicTakesCallModeOnlyWithAHeadset() {
        MicChoice.Plan plan = MicChoice.plan(MicChoice.HEADSET, WITH_HEADSET_IN, WITH_EARBUDS_OUT, HEADSET_CALL);
        check(plan.call && plan.callDevice == SCO && plan.talkOver, "a headset takes call mode");
        check(MicChoice.allowed(plan.mics, SCO, WITH_HEADSET_IN), "the headset's mic is sent");
        check(!MicChoice.allowed(plan.mics, PHONE_MIC, WITH_HEADSET_IN), "a call on the phone's mic isn't sent");
        check(MicChoice.allowed(plan.outputs, SCO, new int[] {SPEAKER, A2DP, SCO}), "replies play on the call");
        check(!MicChoice.allowed(plan.outputs, SPEAKER, new int[] {SPEAKER, SCO}), "a call on the speaker is silent");

        MicChoice.Plan none = MicChoice.plan(MicChoice.HEADSET, PHONE_ONLY_IN, WITH_EARBUDS_OUT, new int[] {1, SPEAKER});
        check(!none.call, "no headset to call through: no call mode");
        check(!MicChoice.allowed(none.mics, PHONE_MIC, PHONE_ONLY_IN), "and nothing is sent");
        check(MicChoice.allowed(none.outputs, A2DP, WITH_EARBUDS_OUT), "replies still reach headphones with no mic");
    }

    private static void headsetMicPrefersLeAudio() {
        MicChoice.Plan plan = MicChoice.plan(MicChoice.HEADSET, new int[] {PHONE_MIC, BLE, SCO},
                new int[] {SPEAKER, BLE}, new int[] {SPEAKER, SCO, BLE});
        check(plan.call && plan.callDevice == BLE, "LE Audio is the call's device when there is one");
        check(!MicChoice.allowed(plan.mics, SCO, new int[] {BLE, SCO}), "only the chosen call route counts");
    }

    private static void phoneMicWithHeadphonesKeepsMediaAndTalkOver() {
        MicChoice.Plan plan = MicChoice.plan(MicChoice.PHONE, WITH_HEADSET_IN, WITH_EARBUDS_OUT, HEADSET_CALL);
        check(!plan.call && plan.talkOver && !plan.onSpeaker(), "the phone's mic with headphones is media, with talk-over");
        check(MicChoice.allowed(plan.mics, PHONE_MIC, WITH_HEADSET_IN), "the phone's mic is sent");
        check(!MicChoice.allowed(plan.mics, SCO, WITH_HEADSET_IN), "the headset's mic isn't");
        check(MicChoice.allowed(plan.outputs, A2DP, WITH_EARBUDS_OUT), "replies play in the headphones");
        check(!MicChoice.allowed(plan.outputs, SPEAKER, WITH_EARBUDS_OUT), "not on the speaker while they're there");
    }

    private static void phoneMicWithoutHeadphonesPlaysOnTheSpeakerWithoutTalkOver() {
        MicChoice.Plan plan = MicChoice.plan(MicChoice.PHONE, PHONE_ONLY_IN, PHONE_ONLY_OUT, new int[] {1, SPEAKER});
        check(!plan.call && plan.onSpeaker() && !plan.talkOver, "phone only: speaker, and no talk-over");
        check(MicChoice.allowed(plan.mics, PHONE_MIC, PHONE_ONLY_IN), "the phone's mic is sent");
        check(MicChoice.allowed(plan.outputs, SPEAKER, PHONE_ONLY_OUT), "replies play on the speaker");
        check(!MicChoice.allowed(plan.outputs, 1, new int[] {1, SPEAKER}), "not on the earpiece");
    }

    private static void aRouteThatOutlivesItsDeviceDoesNotCount() {
        MicChoice.Plan plan = MicChoice.plan(MicChoice.USB, WITH_USB_IN, WITH_EARBUDS_OUT, NONE);
        check(!MicChoice.allowed(plan.mics, USB, PHONE_ONLY_IN), "a USB route with the USB mic gone isn't sent");
        check(!MicChoice.allowed(plan.outputs, A2DP, PHONE_ONLY_OUT), "an earbud route with the earbuds gone is silent");
    }

    private static void wavIsReadResampledAndScaled() {
        short[] in = new short[2400]; // 0.1 s at 24 kHz
        for (int i = 0; i < in.length; i++) in[i] = (short) (i % 2 == 0 ? 20000 : -20000);
        short[] out = Wav.toSpeaker(wav(24000, 2, in, false));
        check(out != null && out.length == 4800, "24 kHz becomes 48 kHz");
        int peak = 0;
        for (short s : out) peak = Math.max(peak, Math.abs((int) s));
        check(Math.abs(peak - 0.4 * 32767) < 2, "the loudest sample sits at the notice's level");
    }

    private static void wavWithAnUnfinishedHeaderReadsToTheEnd() {
        short[] in = new short[1600];
        in[10] = 1000;
        short[] out = Wav.toSpeaker(wav(16000, 1, in, true));
        check(out != null && out.length == 4800, "a data size of 0 reads to the end");
    }

    private static void notWavIsRefused() {
        check(Wav.toSpeaker(new byte[40]) == null, "zeros aren't WAV");
        check(Wav.toSpeaker("RIFF....WAVEdata".getBytes(java.nio.charset.StandardCharsets.US_ASCII)) == null,
                "data before fmt is refused");
    }

    /** A WAV file of one channel's samples, written as {@code channels} identical channels. */
    private static byte[] wav(int rate, int channels, short[] samples, boolean unfinished) {
        ByteBuffer data = ByteBuffer.allocate(samples.length * 2 * channels).order(ByteOrder.LITTLE_ENDIAN);
        for (short s : samples) {
            for (int c = 0; c < channels; c++) data.putShort(s);
        }
        ByteBuffer head = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN);
        head.put("RIFF".getBytes(java.nio.charset.StandardCharsets.US_ASCII)).putInt(36 + data.capacity())
                .put("WAVE".getBytes(java.nio.charset.StandardCharsets.US_ASCII))
                .put("fmt ".getBytes(java.nio.charset.StandardCharsets.US_ASCII)).putInt(16)
                .putShort((short) 1).putShort((short) channels).putInt(rate).putInt(rate * 2 * channels)
                .putShort((short) (2 * channels)).putShort((short) 16)
                .put("data".getBytes(java.nio.charset.StandardCharsets.US_ASCII)).putInt(unfinished ? 0 : data.capacity());
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(head.array(), 0, 44);
        out.write(data.array(), 0, data.capacity());
        return out.toByteArray();
    }

    private static void check(boolean ok, String what) {
        checks++;
        if (!ok) throw new AssertionError(what);
    }
}
