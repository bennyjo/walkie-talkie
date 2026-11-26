package dev.walkietalkie;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * Plain Java, so android/build.sh --test can run it. Turns the WAV file Android's text-to-speech writes into
 * the speaker's samples: mono, 48 kHz, and no louder than the speaker's own tones need.
 */
final class Wav {
    static final int RATE = 48000;
    private static final double PEAK = 0.4; // of full scale: a spoken notice shouldn't startle

    private Wav() {}

    /** The samples, or null if this isn't 16-bit PCM WAV. */
    static short[] toSpeaker(byte[] file) {
        ByteBuffer b = ByteBuffer.wrap(file).order(ByteOrder.LITTLE_ENDIAN);
        if (file.length < 12 || b.getInt(0) != 0x46464952 || b.getInt(8) != 0x45564157) return null; // RIFF, WAVE
        int rate = 0;
        int channels = 0;
        int at = 12;
        while (at + 8 <= file.length) {
            int id = b.getInt(at);
            int size = b.getInt(at + 4);
            int body = at + 8;
            if (id == 0x20746d66) { // "fmt "
                if (size < 16 || body + 16 > file.length) return null;
                int format = b.getShort(body) & 0xffff;
                channels = b.getShort(body + 2) & 0xffff;
                rate = b.getInt(body + 4);
                int bits = b.getShort(body + 14) & 0xffff;
                if (format != 1 || bits != 16 || channels < 1 || rate <= 0) return null;
            } else if (id == 0x61746164) { // "data"
                if (rate == 0) return null;
                // A writer that couldn't go back to fix the header leaves the size wrong: read to the end.
                int length = size <= 0 || body + size > file.length ? file.length - body : size;
                return resample(mono(b, body, length / (2 * channels), channels), rate);
            }
            if (size < 0) return null;
            at = body + size + (size & 1);
        }
        return null;
    }

    private static short[] mono(ByteBuffer b, int start, int frames, int channels) {
        short[] out = new short[frames];
        for (int i = 0; i < frames; i++) out[i] = b.getShort(start + 2 * i * channels);
        return out;
    }

    /** Linear interpolation to 48 kHz, scaled so the loudest sample sits at PEAK. */
    static short[] resample(short[] in, int rate) {
        if (in.length == 0) return in;
        int peak = 1;
        for (short s : in) peak = Math.max(peak, Math.abs((int) s));
        double gain = PEAK * 32767 / peak;
        int length = (int) ((long) in.length * RATE / rate);
        short[] out = new short[length];
        for (int i = 0; i < length; i++) {
            double x = (double) i * rate / RATE;
            int j = (int) x;
            double next = j + 1 < in.length ? in[j + 1] : in[j];
            out[i] = (short) Math.round(gain * (in[j] + (x - j) * (next - in[j])));
        }
        return out;
    }
}
