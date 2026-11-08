package dev.walkietalkie;

import android.content.Context;
import android.media.AudioRecord;

/** The build that goes on a phone: every test allowance is off, and compiled out. */
final class Variant {
    static final boolean TEST = false;

    static int readMic(Context context, AudioRecord record, short[] samples) {
        return record.read(samples, 0, samples.length);
    }

    static boolean anyRoute(Context context) {
        return false;
    }

    private Variant() { }
}
