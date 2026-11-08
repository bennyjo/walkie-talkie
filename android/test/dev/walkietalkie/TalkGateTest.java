package dev.walkietalkie;

import java.util.Random;

/** Plain Java, with no Android and no test library: android/build.sh --test runs it. */
public final class TalkGateTest {
    private static final int FRAME = 320;
    private static final double RATE = 16000;

    private static int checks;

    public static void main(String[] args) {
        floorSettlesAndAVoiceOverAReplyTrips();
        aCollarLevelVoiceTripsWithinAFewFrames();
        nothingTripsWhileNoReplySounds();
        steadyEarbudLeakNeverTrips();
        loudLeakIsLearnedAfterAFalseTripAndAVoiceStillBeatsIt();
        oneFalseAlarmCannotDeafenTheGate();
        aClickDoesNotTrip();
        rumbleDoesNotTrip();
        System.out.println("TalkGateTest: " + checks + " checks passed");
    }

    private static void floorSettlesAndAVoiceOverAReplyTrips() {
        TalkGate gate = settled(-60);
        check(tripsWithin(gate, -30, 10, 1) >= 0, "a voice 30 dB over the floor trips");
    }

    private static void aCollarLevelVoiceTripsWithinAFewFrames() {
        TalkGate gate = settled(-60);
        int frames = tripsWithin(gate, -38, 10, 2);
        check(frames >= 0 && frames <= 4, "a -38 dBFS voice trips within 80 ms, took frames: " + frames);
    }

    private static void nothingTripsWhileNoReplySounds() {
        TalkGate gate = settled(-60);
        for (int i = 0; i < 100; i++) check(!gate.feed(voiced(-20, i), FRAME, false), "no reply, no trip");
    }

    private static void steadyEarbudLeakNeverTrips() {
        for (double leak : new double[] {-65, -60, -55, -52}) {
            TalkGate gate = settled(-60);
            int trips = 0;
            for (int i = 0; i < 50 * 60; i++) {
                short[] frame = mix(speechLike(leak, i), noise(-60, i));
                if (gate.feed(frame, FRAME, true)) trips++;
            }
            check(trips == 0, "a minute of leak at " + leak + " dBFS trips nothing, trips: " + trips);
        }
    }

    private static void loudLeakIsLearnedAfterAFalseTripAndAVoiceStillBeatsIt() {
        TalkGate gate = settled(-60);
        int trips = 0;
        for (int i = 0; i < 50 * 30; i++) {
            if (gate.feed(mix(speechLike(-45, i), noise(-60, i)), FRAME, true)) {
                trips++;
                gate.wasLeak();
            }
        }
        check(trips <= 3, "loud leak is learned within three trips, trips: " + trips);
        check(tripsWithin(gate, -30, 10, 3) >= 0, "a close voice still beats leak the gate has learned");
    }

    private static void oneFalseAlarmCannotDeafenTheGate() {
        TalkGate gate = settled(-60);
        check(tripsWithin(gate, -10, 10, 4) >= 0, "a shout trips");
        gate.wasLeak();
        check(tripsWithin(gate, -30, 10, 5) >= 0, "a normal voice still trips after one bad lesson");
    }

    private static void aClickDoesNotTrip() {
        TalkGate gate = settled(-60);
        check(!gate.feed(voiced(-10, 0), FRAME, true), "a loud frame");
        check(!gate.feed(noise(-60, 1), FRAME, true), "its tail");
        check(!gate.feed(noise(-60, 2), FRAME, true), "then quiet");
        check(!gate.feed(voiced(-10, 3), FRAME, true), "the count starts again");
        check(!gate.feed(voiced(-10, 4), FRAME, true), "and has to reach three");
    }

    private static void rumbleDoesNotTrip() {
        TalkGate gate = settled(-60);
        for (int i = 0; i < 200; i++) {
            short[] frame = new short[FRAME];
            for (int n = 0; n < FRAME; n++) {
                frame[n] = (short) (3000 * Math.sin(2 * Math.PI * 40 * (i * FRAME + n) / RATE));
            }
            check(!gate.feed(frame, FRAME, true), "footsteps and wind are under the speech band");
        }
    }

    private static TalkGate settled(double floorDb) {
        TalkGate gate = new TalkGate();
        for (int i = 0; i < 300; i++) gate.feed(noise(floorDb, i), FRAME, false);
        return gate;
    }

    private static int tripsWithin(TalkGate gate, double db, int frames, int salt) {
        for (int i = 0; i < frames; i++) {
            if (gate.feed(voiced(db, i + salt * 1000), FRAME, true)) return i + 1;
        }
        return -1;
    }

    /** A steady vowel: harmonics of 120 Hz. */
    private static short[] voiced(double db, int frame) {
        double[] out = new double[FRAME];
        double power = 0;
        for (int n = 0; n < FRAME; n++) {
            double t = (frame * FRAME + n) / RATE;
            for (int k = 1; k <= 24; k++) out[n] += Math.sin(2 * Math.PI * 120 * k * t) / k;
            power += out[n] * out[n];
        }
        return scaled(out, power, db);
    }

    /** Speech's rhythm: syllables four times a second, so the leak comes and goes as a reply's would. */
    private static short[] speechLike(double db, int frame) {
        boolean sounding = frame % 12 < 8;
        return sounding ? voiced(db, frame) : new short[FRAME];
    }

    private static short[] noise(double db, int frame) {
        Random random = new Random(frame);
        double[] out = new double[FRAME];
        double power = 0;
        for (int n = 0; n < FRAME; n++) {
            out[n] = random.nextGaussian();
            power += out[n] * out[n];
        }
        return scaled(out, power, db);
    }

    private static short[] scaled(double[] samples, double power, double db) {
        double gain = Math.pow(10, db / 20) * 32768 / Math.sqrt(power / samples.length);
        short[] out = new short[samples.length];
        for (int n = 0; n < samples.length; n++) {
            out[n] = (short) Math.max(-32768, Math.min(32767, samples[n] * gain));
        }
        return out;
    }

    private static short[] mix(short[] a, short[] b) {
        short[] out = new short[a.length];
        for (int n = 0; n < a.length; n++) out[n] = (short) Math.max(-32768, Math.min(32767, a[n] + b[n]));
        return out;
    }

    private static void check(boolean ok, String what) {
        checks++;
        if (!ok) throw new AssertionError(what);
    }

    private TalkGateTest() { }
}
