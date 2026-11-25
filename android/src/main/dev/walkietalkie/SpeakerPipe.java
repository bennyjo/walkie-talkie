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
 * Opus from the Mac to the headphones, or to the phone's speaker when the phone's mic is chosen and no
 * headphones are there. A silent stream stays open between replies, so Bluetooth is already awake and the
 * first syllable isn't clipped. Normal media playback, except with the headphones' mic, which takes call mode.
 * While muted, replies are silenced here, and the app's own tones and notices still play, on whatever output
 * there is, so a call that can't work is never silent.
 */
final class SpeakerPipe implements Runnable {
    interface Listener {
        /** The first frame of speech after silence went to the audio system. */
        void onSpeechStarted();

        /** From the speaker thread, every loop: whether a reply is sounding. */
        void onSounding(boolean sounding);
    }

    private static final String TAG = "walkie";
    private static final int RATE = 48000;      // what Android's Opus decoder always gives
    private static final int FRAME_SAMPLES = RATE / 50;
    private static final int BUFFER_BYTES = RATE / 5; // 100 ms of 16-bit mono
    private static final long CODEC_WAIT_US = 5000;
    private static final short[] SILENCE = new short[FRAME_SAMPLES];
    private static final long SOUNDING_TAIL_MS = OwnSound.TAIL_MS;
    private static final long MAX_HOLD_MS = 8000;       // if the gateway never answers a hold

    private final Listener listener;
    private final ConcurrentLinkedQueue<short[]> speech = new ConcurrentLinkedQueue<>();
    private final ConcurrentLinkedQueue<short[]> tones = new ConcurrentLinkedQueue<>();
    private final OwnSound own = new OwnSound();
    private volatile boolean running;
    private volatile boolean flushRequested;
    private volatile int dropBelow;
    private volatile int lastGeneration;
    private volatile boolean held;
    private volatile int holdId;
    private volatile long heldAt;
    private volatile long lastSpeechAt;
    private volatile boolean muted;
    private volatile AudioTrack track;
    private MediaCodec decoder;
    private short[] quiet = new short[FRAME_SAMPLES];
    private Thread thread;
    private long inputTimeUs;

    private final short[] tapTone = tone(440, 0.07, 0.12);
    private final short[] warnTone = concat(tone(300, 0.15, 0.2), new short[RATE / 10], tone(300, 0.15, 0.2));
    // 0.18 s, the gateway's NOTICE_TONE_SECONDS. Made here, so no reply's words can make it.
    private final short[] noticeTone = concat(tone(660, 0.06, 0.12), tone(990, 0.06, 0.12), new short[RATE * 6 / 100]);

    SpeakerPipe(Listener listener) {
        this.listener = listener;
    }

    /** With call true, the stream is a call's, which plays on the call's device. Otherwise it's media, on output. */
    void start(boolean call, AudioDeviceInfo output) throws IOException {
        try {
            openDecoder();
            track = openTrack(call, output);
        } catch (IOException | RuntimeException e) {
            if (decoder != null) decoder.release();
            decoder = null;
            throw e;
        }
        startThread();
    }

    /** A new stream, for a change between media and call: what's queued, and what's cut, carry over. */
    void reopen(boolean call, AudioDeviceInfo output) {
        stopThread();
        AudioTrack old = track;
        track = null;
        if (old != null) {
            old.stop();
            old.release();
        }
        track = openTrack(call, output);
        startThread();
    }

    private void startThread() {
        running = true;
        thread = new Thread(this, "walkie-speaker");
        thread.setPriority(Thread.MAX_PRIORITY);
        thread.start();
    }

    private void stopThread() {
        running = false;
        if (thread != null) {
            try {
                thread.join(2000);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
    }

    private static AudioTrack openTrack(boolean call, AudioDeviceInfo output) {
        AudioTrack t = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(call ? AudioAttributes.USAGE_VOICE_COMMUNICATION : AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(BUFFER_BYTES)
                .build();
        try {
            t.setPreferredDevice(output);
            t.play();
        } catch (RuntimeException e) {
            t.release();
            throw e;
        }
        return t;
    }

    private void openDecoder() throws IOException {
        MediaFormat format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, RATE, 1);
        format.setByteBuffer("csd-0", ByteBuffer.wrap(opusHead()));
        format.setByteBuffer("csd-1", nanoseconds(0));
        format.setByteBuffer("csd-2", nanoseconds(80000000L));
        decoder = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS);
        decoder.configure(format, null, null, 0);
        decoder.start();
    }

    void stop() {
        stopThread();
        AudioTrack t = track;
        track = null;
        if (t != null) {
            t.stop();
            t.release();
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
        AudioTrack t = track;
        if (t != null) t.addOnRoutingChangedListener(watcher, handler);
    }

    void prefer(AudioDeviceInfo output) {
        AudioTrack t = track;
        if (t != null) t.setPreferredDevice(output);
    }

    /**
     * Replies are silent unless the route is one the mic choice allows, so a reply never plays out of the
     * phone's speaker unless that's the choice. What's already buffered is dropped. Tones and notices still play.
     */
    void setMuted(boolean muted) {
        if (muted && !this.muted) flushRequested = true;
        this.muted = muted;
    }

    int routedType() {
        AudioTrack t = track;
        AudioDeviceInfo d = t == null ? null : t.getRoutedDevice();
        return d == null ? -1 : d.getType();
    }

    int underruns() {
        AudioTrack t = track;
        return t == null ? 0 : t.getUnderrunCount();
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

    /** The app's own tones and notices, which the mic must not send. */
    OwnSound own() {
        return own;
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

    /** One of the phone's own spoken notices, which plays as a tone does, muted or not. */
    void say(short[] samples) {
        tones.add(samples);
    }

    /**
     * The gateway's own notice comes next. Its tone joins the speech in order, so it plays just
     * before the notice, and a cut or a hold takes it with the speech.
     */
    synchronized void noticeTone(int generation) {
        lastGeneration = Math.max(lastGeneration, generation);
        if (generation >= dropBelow) speech.add(noticeTone);
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
        AudioTrack track = this.track;
        boolean wasSilent = true;
        while (running) {
            short[] samples = tones.poll();
            if (flushRequested) {
                flushRequested = false;
                track.pause();
                track.flush();
                track.play();
                wasSilent = true;
            }
            if (held && SystemClock.elapsedRealtime() - heldAt > MAX_HOLD_MS) held = false;
            if (samples != null) {
                own.playing(SystemClock.elapsedRealtime(), samples.length * 1000L / RATE);
            } else {
                samples = held ? null : speech.poll();
                if (samples == null) {
                    wasSilent = true;
                    samples = SILENCE;
                } else if (wasSilent) {
                    wasSilent = false;
                    listener.onSpeechStarted();
                }
                if (samples != SILENCE) lastSpeechAt = SystemClock.elapsedRealtime();
                if (muted && samples != SILENCE) samples = quiet(samples.length);
            }
            track.write(samples, 0, samples.length, AudioTrack.WRITE_BLOCKING);
            listener.onSounding(sounding());
        }
        listener.onSounding(false);
    }

    /** Silence as long as a muted reply's frame, which still takes its time, so a muted reply keeps its pace. */
    private short[] quiet(int length) {
        if (quiet.length != length) quiet = new short[length];
        return quiet;
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
