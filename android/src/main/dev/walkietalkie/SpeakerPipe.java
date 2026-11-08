package dev.walkietalkie;

import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioRouting;
import android.media.AudioTrack;
import android.media.MediaCodec;
import android.media.MediaFormat;
import android.os.Handler;
import android.os.SystemClock;
import android.util.Log;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ConcurrentLinkedQueue;

/**
 * Opus from the Mac to the earbuds. A silent stream stays open between replies, so Bluetooth is
 * already awake and the first syllable isn't clipped. Normal media playback, never call mode.
 */
final class SpeakerPipe implements Runnable {
    interface Listener {
        /** The first frame of speech after silence went to the audio system. */
        void onSpeechStarted();
    }

    private static final String TAG = "walkie";
    private static final int RATE = 48000;      // what Android's Opus decoder always gives
    private static final int FRAME_SAMPLES = RATE / 50;
    private static final int BUFFER_BYTES = RATE / 5; // 100 ms of 16-bit mono
    private static final long CODEC_WAIT_US = 5000;
    private static final short[] SILENCE = new short[FRAME_SAMPLES];
    private static final long SOUNDING_TAIL_MS = 500;   // what's written can reach the earbuds this much later
    private static final long MAX_HOLD_MS = 8000;       // if the gateway never answers a hold

    private final Listener listener;
    private final ConcurrentLinkedQueue<short[]> speech = new ConcurrentLinkedQueue<>();
    private final ConcurrentLinkedQueue<short[]> tones = new ConcurrentLinkedQueue<>();
    private volatile boolean running;
    private volatile boolean flushRequested;
    private volatile int dropBelow;
    private volatile int lastGeneration;
    private volatile boolean held;
    private volatile int holdId;
    private volatile long heldAt;
    private volatile long lastSpeechAt;
    private AudioTrack track;
    private MediaCodec decoder;
    private Thread thread;
    private long inputTimeUs;

    private final short[] tapTone = tone(440, 0.07, 0.12);
    private final short[] warnTone = concat(tone(300, 0.15, 0.2), new short[RATE / 10], tone(300, 0.15, 0.2));

    SpeakerPipe(Listener listener) {
        this.listener = listener;
    }

    void start(AudioDeviceInfo earbuds) throws IOException {
        try {
            open(earbuds);
        } catch (IOException | RuntimeException e) {
            if (track != null) track.release();
            if (decoder != null) decoder.release();
            track = null;
            decoder = null;
            throw e;
        }
        running = true;
        thread = new Thread(this, "walkie-speaker");
        thread.setPriority(Thread.MAX_PRIORITY);
        thread.start();
    }

    private void open(AudioDeviceInfo earbuds) throws IOException {
        track = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(BUFFER_BYTES)
                .build();
        track.setPreferredDevice(earbuds);
        track.play();

        MediaFormat format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, RATE, 1);
        format.setByteBuffer("csd-0", ByteBuffer.wrap(opusHead()));
        format.setByteBuffer("csd-1", nanoseconds(0));
        format.setByteBuffer("csd-2", nanoseconds(80000000L));
        decoder = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS);
        decoder.configure(format, null, null, 0);
        decoder.start();
    }

    void stop() {
        running = false;
        if (thread != null) {
            try {
                thread.join(2000);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
        if (track != null) {
            track.stop();
            track.release();
        }
        synchronized (this) {
            if (decoder != null) {
                decoder.stop();
                decoder.release();
                decoder = null;
            }
        }
    }

    void watchRoute(AudioRouting.OnRoutingChangedListener watcher, Handler handler) {
        track.addOnRoutingChangedListener(watcher, handler);
    }

    void prefer(AudioDeviceInfo earbuds) {
        if (track != null) track.setPreferredDevice(earbuds);
    }

    /** Silent unless the route is the earbuds, so a reply never plays out of the phone's speaker. */
    void setMuted(boolean muted) {
        if (track != null) track.setVolume(muted ? 0f : 1f);
    }

    int routedType() {
        AudioDeviceInfo d = track == null ? null : track.getRoutedDevice();
        return d == null ? -1 : d.getType();
    }

    int underruns() {
        return track == null ? 0 : track.getUnderrunCount();
    }

    /** The earbud tap: silence now, with no round trip, then a small tick. Returns the generation cut to. */
    int cut() {
        int generation = Math.max(dropBelow, lastGeneration) + 1;
        dropBelow = generation;
        silenceNow();
        held = false;
        tones.add(tapTone);
        return generation;
    }

    /** The gateway's word on what's cut. The echo of a tap changes nothing, so it can't clip the tick. */
    void cutTo(int generation) {
        if (generation > Math.max(dropBelow, lastGeneration)) {
            dropBelow = generation;
            silenceNow();
            held = false;
        }
    }

    /**
     * Someone may be talking over the reply: silence now, with no round trip, keeping what's still
     * to play in case they weren't. Returns this hold's id. Bluetooth still plays what it already has.
     */
    int hold() {
        heldAt = SystemClock.elapsedRealtime();
        held = true;
        flushRequested = true;
        return ++holdId;
    }

    /** The gateway found no speech, so the reply carries on. A later hold isn't ended by an old answer. */
    void resume(int id) {
        if (id == holdId) held = false;
    }

    void releaseHold() {
        held = false;
    }

    /** A reply can be reaching the earbuds, and so the mic, right now. */
    boolean sounding() {
        return !held && SystemClock.elapsedRealtime() - lastSpeechAt < SOUNDING_TAIL_MS;
    }

    /** The gateway restarted, so its generations count from the start again. */
    void resetGenerations() {
        dropBelow = 0;
        lastGeneration = 0;
    }

    private void silenceNow() {
        speech.clear();
        flushRequested = true;
    }

    void warnTone() {
        tones.add(warnTone);
    }

    /** One packet from the network thread. */
    synchronized void decode(int generation, byte[] packet, int offset, int length) {
        lastGeneration = Math.max(lastGeneration, generation);
        if (generation < dropBelow || decoder == null) return;
        try {
            int input = decoder.dequeueInputBuffer(CODEC_WAIT_US);
            if (input >= 0) {
                ByteBuffer buffer = decoder.getInputBuffer(input);
                buffer.clear();
                buffer.put(packet, offset, length);
                decoder.queueInputBuffer(input, 0, length, inputTimeUs, 0);
                inputTimeUs += 20000;
            }
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            int output;
            while ((output = decoder.dequeueOutputBuffer(info, CODEC_WAIT_US)) != MediaCodec.INFO_TRY_AGAIN_LATER) {
                if (output < 0) continue; // format or buffers changed
                if (info.size > 0) {
                    ByteBuffer out = decoder.getOutputBuffer(output);
                    out.position(info.offset);
                    short[] samples = new short[info.size / 2];
                    out.order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().get(samples);
                    if (generation >= dropBelow) speech.add(samples);
                }
                decoder.releaseOutputBuffer(output, false);
            }
        } catch (IllegalStateException e) {
            Log.e(TAG, "the decoder failed: " + e.getClass().getSimpleName());
        }
    }

    @Override public void run() {
        boolean wasSilent = true;
        while (running) {
            if (flushRequested) {
                flushRequested = false;
                track.pause();
                track.flush();
                track.play();
                wasSilent = true;
            }
            if (held && SystemClock.elapsedRealtime() - heldAt > MAX_HOLD_MS) held = false;
            short[] samples = tones.poll();
            if (samples == null) {
                samples = held ? null : speech.poll();
                if (samples == null) {
                    wasSilent = true;
                    samples = SILENCE;
                } else if (wasSilent) {
                    wasSilent = false;
                    listener.onSpeechStarted();
                }
                if (samples != SILENCE) lastSpeechAt = SystemClock.elapsedRealtime();
            }
            track.write(samples, 0, samples.length, AudioTrack.WRITE_BLOCKING);
        }
    }

    private static short[] tone(int hertz, double seconds, double level) {
        short[] out = new short[(int) (RATE * seconds)];
        int fade = RATE / 200;
        for (int i = 0; i < out.length; i++) {
            double envelope = Math.min(1.0, Math.min(i, out.length - 1 - i) / (double) fade);
            out[i] = (short) (level * 32767 * envelope * Math.sin(2 * Math.PI * hertz * i / RATE));
        }
        return out;
    }

    private static short[] concat(short[]... parts) {
        int total = 0;
        for (short[] p : parts) total += p.length;
        short[] out = new short[total];
        int at = 0;
        for (short[] p : parts) {
            System.arraycopy(p, 0, out, at, p.length);
            at += p.length;
        }
        return out;
    }

    /** The Opus identification header Android's decoder wants as csd-0. */
    private static byte[] opusHead() {
        ByteBuffer b = ByteBuffer.allocate(19).order(ByteOrder.LITTLE_ENDIAN);
        b.put("OpusHead".getBytes(StandardCharsets.US_ASCII));
        b.put((byte) 1);            // version
        b.put((byte) 1);            // channels
        b.putShort((short) 0);      // pre-skip
        b.putInt(24000);            // the original rate, for information
        b.putShort((short) 0);      // gain
        b.put((byte) 0);            // channel mapping
        return b.array();
    }

    private static ByteBuffer nanoseconds(long value) {
        ByteBuffer b = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN);
        b.putLong(value);
        b.flip();
        return b;
    }
}
