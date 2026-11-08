package dev.walkietalkie;

import android.content.Context;
import android.media.AudioRecord;
import android.os.SystemClock;
import android.util.Log;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;

/**
 * The emulator's build, which has no DJI receiver or earbuds. It accepts the emulator's address for
 * the Mac, takes its microphone from a file pushed with adb, and accepts any audio route while a
 * file named any-route exists. Without that file, the privacy guards behave as on a phone. Never
 * install this one on a phone: build.sh makes it only when asked with --debug, under its own package
 * name, so it can't replace the phone's app.
 */
final class Variant {
    static final boolean TEST = true;

    private static final String TAG = "walkie";
    private static FileInputStream testMic;

    static int readMic(Context context, AudioRecord record, short[] samples) {
        int got = readTestMic(context, samples);
        if (got > 0) return got;
        if (record == null) {
            SystemClock.sleep(20);
            Arrays.fill(samples, (short) 0);
            return samples.length;
        }
        return record.read(samples, 0, samples.length);
    }

    /** Raw 16 kHz mono 16-bit samples from a file, played once at about real time and then deleted. */
    private static int readTestMic(Context context, short[] samples) {
        File file = new File(context.getExternalFilesDir(null), "test-mic.pcm");
        try {
            if (testMic == null) {
                if (!file.exists() || file.length() == 0) return 0;
                testMic = new FileInputStream(file);
                Log.i(TAG, "test microphone started");
            }
            byte[] raw = new byte[samples.length * 2];
            int got = testMic.read(raw);
            if (got <= 0) {
                testMic.close();
                testMic = null;
                if (!file.delete()) Log.i(TAG, "couldn't delete the test microphone file");
                Log.i(TAG, "test microphone ended");
                return 0;
            }
            ByteBuffer.wrap(raw, 0, got).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().get(samples, 0, got / 2);
            SystemClock.sleep(20);
            return got / 2;
        } catch (IOException e) {
            return 0;
        }
    }

    static boolean anyRoute(Context context) {
        return new File(context.getExternalFilesDir(null), "any-route").exists();
    }

    private Variant() { }
}
