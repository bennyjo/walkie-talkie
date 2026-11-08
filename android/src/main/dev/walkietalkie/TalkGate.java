package dev.walkietalkie;

/**
 * Decides, on the phone and with no round trip, that someone is talking over the reply. A clipped-on
 * mic hears its wearer far louder than it hears in-ear earbuds, so a voice has to beat both the
 * background and the loudest the earbuds have been heard to leak. Being wrong usually costs a moment
 * of silence: the gateway checks for real speech, and has the reply resume if there's none. Its words
 * can still count as the owner's, which cuts the reply, so the gate learns from those too.
 */
final class TalkGate {
    private static final double RATE = 16000;
    private static final double LOW_HERTZ = 200;   // the same speech band the gateway measures
    private static final double HIGH_HERTZ = 4000;
    private static final int FRAMES_PER_SECOND = 50;
    private static final int BACKGROUND_FRAMES = 4 * FRAMES_PER_SECOND;
    private static final int TRIP_FRAMES = 3;           // 60 ms: longer than a click
    private static final double FLOOR_MARGIN_DB = 12;
    private static final double LEAK_MARGIN_DB = 8;
    private static final double MIN_DB = -55;
    private static final double LEAK_RISE_DB = 0.25;    // per frame: a voice's onset outruns this
    private static final double LEAK_FALL_DB = 0.01;    // per frame, while a reply sounds
    private static final double LEAK_LESSON_DB = 6;     // the most one false alarm can teach
    private static final double SILENT_DB = -120;

    private final Biquad high = Biquad.highPass(LOW_HERTZ);
    private final Biquad higher = Biquad.highPass(LOW_HERTZ);  // twice, because footsteps are strong
    private final Biquad low = Biquad.lowPass(HIGH_HERTZ);
    private final double[] background = new double[BACKGROUND_FRAMES];
    private int backgroundAt;
    private int backgroundFilled;
    private double leak = SILENT_DB;
    private double level = SILENT_DB;
    private double tripLevel = SILENT_DB;
    private int run;

    /** One frame of the mic. True when this frame makes someone's voice certain enough to act on. */
    boolean feed(short[] samples, int count, boolean replySounding) {
        level = measure(samples, count);
        double threshold = threshold();
        background[backgroundAt] = level;
        backgroundAt = (backgroundAt + 1) % BACKGROUND_FRAMES;
        backgroundFilled = Math.min(backgroundFilled + 1, BACKGROUND_FRAMES);
        if (!replySounding) {
            run = 0;
            return false;
        }
        if (level > threshold) {
            if (++run < TRIP_FRAMES) return false;
            run = 0;
            tripLevel = level;
            return true;
        }
        run = 0;
        leak = Math.max(leak - LEAK_FALL_DB, Math.min(level, leak + LEAK_RISE_DB));
        return false;
    }

    /** The gateway heard speech that may have been the earbuds. Capped, in case it was the owner. */
    void wasLeak() {
        double assumed = threshold() - LEAK_MARGIN_DB;
        leak = Math.max(leak, Math.min(tripLevel, assumed + LEAK_LESSON_DB));
    }

    double level() {
        return level;
    }

    double floor() {
        if (backgroundFilled == 0) return SILENT_DB;
        double quietest = 0;
        for (int i = 0; i < backgroundFilled; i++) quietest = Math.min(quietest, background[i]);
        return quietest;
    }

    double leak() {
        return leak;
    }

    private double threshold() {
        return Math.max(MIN_DB, Math.max(floor() + FLOOR_MARGIN_DB, leak + LEAK_MARGIN_DB));
    }

    private double measure(short[] samples, int count) {
        double total = 0;
        for (int i = 0; i < count; i++) {
            double out = low.next(higher.next(high.next(samples[i])));
            total += out * out;
        }
        return total > 0 ? 10 * Math.log10(total / count / (32768.0 * 32768.0)) : SILENT_DB;
    }

    /** A second-order filter, from the Audio EQ Cookbook. */
    private static final class Biquad {
        private static final double Q = Math.sqrt(0.5);
        private final double b0;
        private final double b1;
        private final double b2;
        private final double a1;
        private final double a2;
        private double x1;
        private double x2;
        private double y1;
        private double y2;

        private Biquad(double b0, double b1, double b2, double a0, double a1, double a2) {
            this.b0 = b0 / a0;
            this.b1 = b1 / a0;
            this.b2 = b2 / a0;
            this.a1 = a1 / a0;
            this.a2 = a2 / a0;
        }

        static Biquad highPass(double hertz) {
            double w = 2 * Math.PI * hertz / RATE;
            double alpha = Math.sin(w) / (2 * Q);
            double c = Math.cos(w);
            return new Biquad((1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + alpha, -2 * c, 1 - alpha);
        }

        static Biquad lowPass(double hertz) {
            double w = 2 * Math.PI * hertz / RATE;
            double alpha = Math.sin(w) / (2 * Q);
            double c = Math.cos(w);
            return new Biquad((1 - c) / 2, 1 - c, (1 - c) / 2, 1 + alpha, -2 * c, 1 - alpha);
        }

        double next(double x) {
            double y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
            x2 = x1;
            x1 = x;
            y2 = y1;
            y1 = y;
            return y;
        }
    }
}
