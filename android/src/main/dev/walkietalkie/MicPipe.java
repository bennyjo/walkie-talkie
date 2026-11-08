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

/** The DJI receiver to Opus to the Mac. Sound goes out only while the route is the USB input. */
final class MicPipe implements Runnable {
    private static final String TAG = "walkie";
    private static final int RATE = 16000;
    private static final int FRAME_SAMPLES = RATE / 50; // 20 ms
    private static final int BIT_RATE = 24000;
    private static final long CODEC_WAIT_US = 10000;
    private static final int ZERO_FRAMES = 50;          // a second of exact zeros: Android mutes the mic

    private final Context context;
    private final GatewayLink link;
    private volatile boolean running;
    private volatile boolean open;
    private volatile boolean allZeros;
    private volatile AudioRecord record;
    private Thread thread;

    MicPipe(Context context, GatewayLink link) {
        this.context = context;
        this.link = link;
    }

    void start(AudioDeviceInfo usb) {
        running = true;
        AudioRecord r = null;
        try {
            // VOICE_RECOGNITION, never VOICE_COMMUNICATION: call mode would tie the mic to the earbuds.
            r = new AudioRecord.Builder()
                    .setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(RATE)
                            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                    .setBufferSizeInBytes(RATE)
                    .setPrivacySensitive(true)
                    .build();
            r.setPreferredDevice(usb);
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
        if (r != null) {
            r.stop();
            r.release();
        }
    }

    void watchRoute(AudioRouting.OnRoutingChangedListener watcher, Handler handler) {
        AudioRecord r = record;
        if (r != null) r.addOnRoutingChangedListener(watcher, handler);
    }

    void prefer(AudioDeviceInfo usb) {
        AudioRecord r = record;
        if (r != null) r.setPreferredDevice(usb);
    }

    /** Off while the audio route isn't the USB input, so a phone in a pocket can't send its own mic. */
    void setOpen(boolean open) {
        this.open = open;
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
            if (encoder != null) {
                encoder.stop();
                encoder.release();
            }
        }
    }

    private void capture(MediaCodec encoder) {
        short[] samples = new short[FRAME_SAMPLES];
        byte[] packet = new byte[4096];
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        long written = 0;
        int zeroFrames = 0;
        while (running) {
            int got = Variant.readMic(context, record, samples);
            if (got <= 0) {
                link.skipMic();
                SystemClock.sleep(20);
                continue;
            }
            boolean any = false;
            for (int i = 0; i < got && !any; i++) any = samples[i] != 0;
            zeroFrames = any ? 0 : zeroFrames + 1;
            allZeros = zeroFrames >= ZERO_FRAMES;

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
