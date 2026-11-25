package dev.walkietalkie;

import android.content.Context;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.AudioRecordingConfiguration;
import android.media.AudioRouting;
import android.media.MediaCodec;
import android.media.MediaFormat;
import android.media.MediaRecorder;
import android.os.Handler;
import android.os.SystemClock;
import android.util.Log;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * The chosen mic to Opus to the Mac. Sound goes out only while the route is the input the mic choice allows.
 * It captures both channels and keeps the louder: a USB receiver with one transmitter can leave the other
 * silent, and Android's mix down to mono would then halve the voice.
 */
final class MicPipe implements Runnable {
    interface Listener {
        /** From the capture thread: a voice over the reply. */
        void onTalkOver();

        /** From the capture thread, every frame: whether someone is speaking into the mic. */
        void onSpeaking(boolean speaking);
    }

    private static final String TAG = "walkie";
    private static final int RATE = 16000;
    private static final int FRAME_SAMPLES = RATE / 50; // 20 ms
    private static final int BIT_RATE = 24000;
    private static final long CODEC_WAIT_US = 10000;
    private static final int ZERO_FRAMES = 50;          // a second of exact zeros: Android mutes the mic

    private final Context context;
    private final GatewayLink link;
    private final SpeakerPipe speaker;
    private final Listener listener;
    private final TalkGate gate = new TalkGate();
    private volatile double level = -120;
    private volatile double floor;
    private volatile double leak;
    private volatile int channels;
    private volatile boolean running;
    private volatile boolean open;
    private volatile boolean talkOver = true;
    private volatile boolean allZeros;
    private volatile AudioRecord record;
    private Thread thread;

    MicPipe(Context context, GatewayLink link, SpeakerPipe speaker, Listener listener) {
        this.context = context;
        this.link = link;
        this.speaker = speaker;
        this.listener = listener;
    }

    /**
     * With call true, the mic is a call's: the headphones' own, which call mode routes. Otherwise it's
     * VOICE_RECOGNITION from the preferred input. VOICE_COMMUNICATION belongs with call mode, which drops
     * Bluetooth headphones to call quality, so only the headphones' mic uses it.
     */
    void start(boolean call, AudioDeviceInfo input) {
        running = true;
        AudioRecord r = null;
        try {
            r = new AudioRecord.Builder()
                    .setAudioSource(call ? MediaRecorder.AudioSource.VOICE_COMMUNICATION
                            : MediaRecorder.AudioSource.VOICE_RECOGNITION)
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(RATE)
                            .setChannelMask(AudioFormat.CHANNEL_IN_STEREO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                    .setBufferSizeInBytes(2 * RATE)
                    .setPrivacySensitive(true)
                    .build();
            r.setPreferredDevice(input);
            r.startRecording();
            record = r;
        } catch (SecurityException | UnsupportedOperationException | IllegalStateException e) {
            if (r != null) r.release();
            if (!Variant.TEST) throw e;
            Log.i(TAG, "no audio input in this test: " + e);
        }
        thread = new Thread(this, "walkie-mic");
        thread.setPriority(Thread.MAX_PRIORITY);
        thread.start();
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
        AudioRecord r = record;
        record = null;
        if (r != null) {
            r.stop();
            r.release();
        }
    }

    void watchRoute(AudioRouting.OnRoutingChangedListener watcher, Handler handler) {
        AudioRecord r = record;
        if (r != null) r.addOnRoutingChangedListener(watcher, handler);
    }

    void prefer(AudioDeviceInfo input) {
        AudioRecord r = record;
        if (r != null) r.setPreferredDevice(input);
    }

    /** Off while the route isn't the chosen mic, so a phone in a pocket can't send a mic you didn't choose. */
    void setOpen(boolean open) {
        this.open = open;
    }

    /** Off while replies play on the phone's speaker, next to the mic, where a reply would be heard as a voice. */
    void setTalkOver(boolean talkOver) {
        this.talkOver = talkOver;
    }

    int routedType() {
        AudioRecord r = record;
        AudioDeviceInfo d = r == null ? null : r.getRoutedDevice();
        return d == null ? -1 : d.getType();
    }

    boolean silenced() {
        AudioRecord r = record;
        AudioRecordingConfiguration c = r == null ? null : r.getActiveRecordingConfiguration();
        return c != null && c.isClientSilenced();
    }

    boolean allZeros() {
        return allZeros;
    }

    /** The gateway heard speech during a hold that may have been the earbuds. */
    void wasLeak() {
        gate.wasLeak();
    }

    /** For the screen: the loudest in the speech band since the last call, in dB under full scale. */
    double takeLevel() {
        double loudest = level;
        level = -120;
        return loudest;
    }

    double floor() {
        return floor;
    }

    double leak() {
        return leak;
    }

    /** Which channels carry sound: 1 left, 2 right, 3 both. */
    int channels() {
        return channels;
    }

    @Override public void run() {
        MediaCodec encoder = null;
        try {
            MediaFormat format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, RATE, 1);
            format.setInteger(MediaFormat.KEY_BIT_RATE, BIT_RATE);
            encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS);
            encoder.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
            encoder.start();
            capture(encoder);
        } catch (IOException | IllegalStateException e) {
            Log.e(TAG, "the microphone stopped: " + e.getClass().getSimpleName());
        } finally {
            listener.onSpeaking(false);
            if (encoder != null) {
                encoder.stop();
                encoder.release();
            }
        }
    }

    private int louderChannel(short[] both, int count, short[] out) {
        long left = 0;
        long right = 0;
        for (int i = 0; i + 1 < count; i += 2) {
            left += both[i] * both[i];
            right += both[i + 1] * both[i + 1];
        }
        channels = (left > 0 ? 1 : 0) | (right > 0 ? 2 : 0);
        int from = right > left ? 1 : 0;
        int frames = count / 2;
        for (int i = 0; i < frames; i++) out[i] = both[2 * i + from];
        return frames;
    }

    private void capture(MediaCodec encoder) {
        short[] both = new short[2 * FRAME_SAMPLES];
        short[] samples = new short[FRAME_SAMPLES];
        byte[] packet = new byte[4096];
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        long written = 0;
        int zeroFrames = 0;
        while (running) {
            int gotBoth = Variant.readMic(context, record, both);
            if (gotBoth <= 0) {
                listener.onSpeaking(false);
                link.skipMic();
                SystemClock.sleep(20);
                continue;
            }
            int got = louderChannel(both, gotBoth, samples);
            boolean any = false;
            for (int i = 0; i < got && !any; i++) any = samples[i] != 0;
            zeroFrames = any ? 0 : zeroFrames + 1;
            allZeros = zeroFrames >= ZERO_FRAMES;

            // With talk-over off, on the phone's speaker, the mic hears the reply far louder than any lesson
            // could teach the gate, so while a reply sounds nothing here counts, as at the gateway.
            boolean sounding = speaker.sounding();
            boolean deaf = !talkOver && sounding;
            if (!deaf && gate.feed(samples, got, sounding) && open) listener.onTalkOver();
            listener.onSpeaking(open && !deaf && gate.speaking());
            level = Math.max(level, gate.level());
            floor = gate.floor();
            leak = gate.leak();

            int input = encoder.dequeueInputBuffer(CODEC_WAIT_US);
            if (input >= 0) {
                ByteBuffer buffer = encoder.getInputBuffer(input);
                buffer.clear();
                buffer.order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().put(samples, 0, got);
                encoder.queueInputBuffer(input, 0, got * 2, written * 1000000 / RATE, 0);
                written += got;
            } else {
                link.skipMic();
            }
            int output;
            while ((output = encoder.dequeueOutputBuffer(info, 0)) != MediaCodec.INFO_TRY_AGAIN_LATER) {
                if (output < 0) continue; // format or buffers changed
                boolean config = (info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0;
                if (!config && info.size > 0) {
                    if (open && info.size <= packet.length) {
                        ByteBuffer buffer = encoder.getOutputBuffer(output);
                        buffer.position(info.offset);
                        buffer.get(packet, 0, info.size);
                        link.sendMic(packet, info.size);
                    } else {
                        link.skipMic();
                    }
                }
                encoder.releaseOutputBuffer(output, false);
            }
        }
    }
}
