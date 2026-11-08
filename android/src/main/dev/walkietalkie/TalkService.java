package dev.walkietalkie;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioDeviceCallback;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.media.AudioRouting;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Log;
import android.view.KeyEvent;
import java.io.IOException;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Runs the conversation with the phone locked. Started from a tap in MainActivity, because a
 * microphone service started from the background gets no mic access. It can't restart itself after
 * Android kills it, for the same reason, so it's not sticky: a tap on the app brings it back.
 *
 * It starts only with a fingerprint's signature for one session, and it stops for good when the
 * gateway ends that run. The next Start takes a new fingerprint, like a banking app's next login.
 */
public class TalkService extends Service implements GatewayLink.Listener, SpeakerPipe.Listener, MicPipe.Listener {
    static final String ACTION_START = "dev.walkietalkie.START";
    static final String ACTION_STOP = "dev.walkietalkie.STOP";
    static final String ACTION_VOICE = "dev.walkietalkie.VOICE";
    static final String ACTION_CONFIRMED = "dev.walkietalkie.CONFIRMED";
    static final String ACTION_DECLINED = "dev.walkietalkie.DECLINED";
    static final String EXTRA_VOICE = "voice";
    static final String EXTRA_RUN = "run";
    static final String EXTRA_APPROVE = "approve";
    static final String EXTRA_NAME = "name";
    static final String EXTRA_NONCE = "nonce";
    static final String EXTRA_SIG = "sig";

    private static final String TAG = "walkie";
    private static final String CHANNEL = "talk";
    private static final String CONFIRM_CHANNEL = "confirm";
    private static final int NOTIFICATION = 1;
    private static final int CONFIRM_NOTIFICATION = 2;
    private static final long STOP_AFTER_TONE_MS = 700;
    private static final long MAX_RUN_MS = 4 * 60 * 60 * 1000L; // a forgotten microphone stops by itself
    private static final long CHECK_EVERY_MS = 1000;
    private static final long TAP_DEBOUNCE_MS = 300;

    /** What the screen shows. Written here, read by MainActivity. */
    static final class Status {
        volatile boolean running;
        volatile boolean connected;
        volatile String why;
        volatile int micRoute = -1;
        volatile int soundRoute = -1;
        volatile int audioMode;
        volatile boolean micSilenced;
        volatile long roundTripMs;
        volatile int taps;
        volatile int underruns;
        volatile JSONObject sessions;
        volatile JSONObject voices;
        volatile double micLevel;
        volatile double micFloor;
        volatile double micLeak;
        volatile int micChannels;
        volatile int talkOvers;
        volatile int resumedTalkOvers;
        volatile String session;    // the name of the session this run talks to
        volatile int pairingDays = -1;
        volatile JSONObject confirm; // a read-back waiting for a fingerprint, or null
        volatile boolean pairingOver; // the Mac wants this phone to pair again
    }

    static final Status status = new Status();

    private final Handler main = new Handler(Looper.getMainLooper());
    private AudioManager audio;
    private PowerManager.WakeLock wake;
    private MediaSession session;
    private GatewayLink link;
    private MicPipe mic;
    private SpeakerPipe speaker;
    private boolean started;
    private boolean micProblem;
    private String shownText = "";
    private long lastTap;

    private final AudioDeviceCallback deviceWatch = new AudioDeviceCallback() {
        @Override public void onAudioDevicesAdded(AudioDeviceInfo[] devices) {
            applyRoutes();
        }
        @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] devices) {
            applyRoutes();
        }
    };

    private final AudioRouting.OnRoutingChangedListener routeWatch = router -> {
        if (started) checkRoutes();
    };

    private final Runnable check = new Runnable() {
        @Override public void run() {
            if (!started) return;
            checkRoutes();
            main.postDelayed(this, CHECK_EVERY_MS);
        }
    };

    @Override public IBinder onBind(Intent intent) {
        return null;
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            stopSelf();
        } else if (ACTION_VOICE.equals(action)) {
            if (started) chooseVoice(intent.getStringExtra(EXTRA_VOICE));
        } else if (ACTION_CONFIRMED.equals(action) || ACTION_DECLINED.equals(action)) {
            if (started) answerConfirm(intent, ACTION_CONFIRMED.equals(action));
        } else if (ACTION_START.equals(action) && !started) {
            begin(intent);
        }
        return START_NOT_STICKY;
    }

    private void begin(Intent intent) {
        Pairing pairing = Pairing.load(this);
        startForeground(NOTIFICATION, notification("Starting"), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
                | ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        JSONObject approve;
        try {
            approve = new JSONObject(String.valueOf(intent.getStringExtra(EXTRA_APPROVE)));
        } catch (JSONException e) {
            approve = null;
        }
        String run = intent.getStringExtra(EXTRA_RUN);
        if (pairing == null || approve == null || run == null) {
            stopSelf();
            return;
        }
        audio = getSystemService(AudioManager.class);
        wake = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "walkie:talk");
        wake.acquire(MAX_RUN_MS);
        session = new MediaSession(this, "walkie");
        session.setCallback(new MediaSession.Callback() {
            @Override public boolean onMediaButtonEvent(Intent event) {
                return onEarbudButton(event);
            }
        });
        session.setPlaybackState(new PlaybackState.Builder()
                .setState(PlaybackState.STATE_PLAYING, 0, 1f)
                .setActions(PlaybackState.ACTION_PLAY_PAUSE).build());
        session.setActive(true);

        link = new GatewayLink(pairing, this, run, approve);
        speaker = new SpeakerPipe(this);
        mic = new MicPipe(this, link, speaker, this);
        try {
            speaker.start(earbuds());
            mic.start(usbInput());
        } catch (IOException | RuntimeException e) {
            Log.e(TAG, "couldn't start audio: " + e.getClass().getSimpleName());
            status.why = "Couldn't start audio";
            stopSelf();
            return;
        }
        started = true;
        status.running = true;
        status.why = null;
        status.sessions = null;
        status.voices = null;
        status.confirm = null;
        status.session = intent.getStringExtra(EXTRA_NAME);
        link.start();
        audio.registerAudioDeviceCallback(deviceWatch, main);
        mic.watchRoute(routeWatch, main);
        speaker.watchRoute(routeWatch, main);
        main.post(check);
        main.postDelayed(this::stopSelf, MAX_RUN_MS);
    }

    @Override public void onDestroy() {
        main.removeCallbacksAndMessages(null);
        status.running = false;
        status.connected = false;
        status.confirm = null;
        getSystemService(NotificationManager.class).cancel(CONFIRM_NOTIFICATION);
        if (mic != null) mic.stop();
        if (speaker != null) speaker.stop();
        if (started) {
            audio.unregisterAudioDeviceCallback(deviceWatch);
            send(new JSONObject(), "release");
            link.stop();
        }
        started = false;
        if (session != null) session.release();
        if (wake != null && wake.isHeld()) wake.release();
        super.onDestroy();
    }

    // --- the earbud tap ----------------------------------------------------------

    private boolean onEarbudButton(Intent event) {
        KeyEvent key = event.getParcelableExtra(Intent.EXTRA_KEY_EVENT, KeyEvent.class);
        if (key == null || key.getAction() != KeyEvent.ACTION_DOWN || key.getRepeatCount() != 0) return true;
        switch (key.getKeyCode()) {
            case KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE:
            case KeyEvent.KEYCODE_MEDIA_PLAY:
            case KeyEvent.KEYCODE_MEDIA_PAUSE:
            case KeyEvent.KEYCODE_MEDIA_STOP:
            case KeyEvent.KEYCODE_HEADSETHOOK:
                break;
            default:
                return true;
        }
        long now = SystemClock.elapsedRealtime();
        if (started && now - lastTap > TAP_DEBOUNCE_MS) {
            lastTap = now;
            status.taps++;
            tap(speaker.cut());
            Log.i(TAG, "earbud tap");
        }
        return true;
    }

    // --- routes ------------------------------------------------------------------

    private AudioDeviceInfo usbInput() {
        return find(AudioManager.GET_DEVICES_INPUTS, AudioDeviceInfo.TYPE_USB_DEVICE, AudioDeviceInfo.TYPE_USB_HEADSET);
    }

    private AudioDeviceInfo earbuds() {
        return find(AudioManager.GET_DEVICES_OUTPUTS, AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, AudioDeviceInfo.TYPE_BLE_HEADSET);
    }

    private AudioDeviceInfo find(int direction, int... types) {
        AudioManager am = getSystemService(AudioManager.class);
        for (int type : types) {
            for (AudioDeviceInfo device : am.getDevices(direction)) {
                if (device.getType() == type) return device;
            }
        }
        return null;
    }

    private void applyRoutes() {
        if (!started) return;
        mic.prefer(usbInput());
        speaker.prefer(earbuds());
        checkRoutes();
    }

    /** The two privacy rules: the phone's own mic is never sent, and its speaker never plays a reply. */
    private void checkRoutes() {
        int in = mic.routedType();
        int out = speaker.routedType();
        boolean anyRoute = Variant.anyRoute(this);
        // A stream's route can lag behind a device's removal, so the device must also still be there.
        boolean micOk = anyRoute || usbInput() != null
                && (in == AudioDeviceInfo.TYPE_USB_DEVICE || in == AudioDeviceInfo.TYPE_USB_HEADSET);
        boolean soundOk = anyRoute || earbuds() != null
                && (out == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP || out == AudioDeviceInfo.TYPE_BLE_HEADSET);
        boolean silenced = !anyRoute && (mic.silenced() || (micOk && mic.allZeros()));
        mic.setOpen(micOk);
        speaker.setMuted(!soundOk);
        boolean problem = !micOk || silenced;
        if (problem && !micProblem) speaker.warnTone();
        micProblem = problem;

        String text;
        if (!micOk) {
            text = "Plug in the mic's USB receiver. The mic is off until then.";
        } else if (silenced) {
            text = "Android has silenced the mic. Another app may have taken it.";
        } else if (!soundOk) {
            text = "Connect the earbuds. Replies are silent until then.";
        } else {
            text = link.connected() ? "Listening on the USB mic" : "Connecting to the Mac";
        }
        if (!text.equals(shownText)) {
            shownText = text;
            getSystemService(NotificationManager.class).notify(NOTIFICATION, notification(text));
        }

        status.micRoute = in;
        status.soundRoute = out;
        status.audioMode = audio.getMode();
        status.micSilenced = silenced;
        status.roundTripMs = link.roundTripMs();
        status.underruns = speaker.underruns();
        status.micLevel = mic.takeLevel();
        status.micFloor = mic.floor();
        status.micLeak = mic.leak();
        status.micChannels = mic.channels();
    }

    private Notification notification(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Conversation", NotificationManager.IMPORTANCE_LOW));
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, TalkService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle("Walkie Talkie")
                .setContentText(text)
                .setContentIntent(open)
                .setOngoing(true)
                .setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE)
                .addAction(new Notification.Action.Builder(null, "Stop", stop).build())
                .build();
    }

    // --- the link ----------------------------------------------------------------

    private void chooseVoice(String voice) {
        try {
            send(new JSONObject().put("id", voice), "voice");
        } catch (JSONException e) {
            Log.e(TAG, "bad voice id");
        }
    }

    /** From the capture thread: silence now, then the gateway decides whether it was a voice. */
    @Override public void onTalkOver() {
        status.talkOvers++;
        try {
            send(new JSONObject().put("id", speaker.hold()), "hold");
        } catch (JSONException e) {
            Log.e(TAG, "bad message");
        }
    }

    private void tap(int generation) {
        try {
            send(new JSONObject().put("gen", generation), "tap");
        } catch (JSONException e) {
            Log.e(TAG, "bad message");
        }
    }

    private void send(JSONObject message, String kind) {
        try {
            link.sendControl(message.put("t", kind));
        } catch (JSONException e) {
            Log.e(TAG, "bad message");
        }
    }

    @Override public void onSpeech(int generation, byte[] packet, int offset, int length) {
        speaker.decode(generation, packet, offset, length);
    }

    @Override public void onMessage(JSONObject message) {
        switch (message.optString("t")) {
            case "sessions":
                status.sessions = message;
                break;
            case "ended":
                end(endedText(message.optString("why")));
                break;
            case "confirm":
                showConfirm(message.isNull("nonce") ? null : message);
                break;
            case "voices":
                status.voices = message;
                break;
            case "tone":
                speaker.warnTone();
                break;
            case "resume":
                speaker.resume(message.optInt("id"));
                if (message.optBoolean("leak")) mic.wasLeak();
                status.resumedTalkOvers++;
                break;
            case "leak":
                mic.wasLeak();
                break;
            case "cut":
                speaker.cutTo(message.optInt("gen"));
                break;
            default:
                break;
        }
    }

    @Override public boolean onLogin(JSONObject answer) {
        if (answer.optBoolean("expired")) {
            status.pairingOver = true;
            main.post(() -> end("This pairing is over 30 days old. Pair again, with a new code from the Mac"));
            return false;
        }
        status.pairingDays = answer.optInt("days", -1);
        if (answer.optBoolean("approved")) return true;
        String why = answer.optString("refused");
        main.post(() -> end(refusedText(why)));
        return false;
    }

    private static String refusedText(String why) {
        switch (why) {
            case "gone":
                return "That session has gone. Choose another, and tap Start";
            case "late":
                return "That took too long. Tap Start again";
            case "changed":
                return "That session changed a moment ago. Tap Start again";
            case "signature":
                return "Your Mac didn't accept this phone's fingerprint. Pair again";
            default:
                return "The call ended while the phone was away. Tap Start to connect again";
        }
    }

    private static String endedText(String why) {
        switch (why) {
            case "idle":
                return "Ended after 30 minutes with nothing said. Tap Start to connect again";
            case "gone":
                return "That session has gone. Choose another, and tap Start";
            case "closed":
                return "That session ended the call. Tap Start to connect again";
            default:
                return "The call ended. Tap Start to connect again";
        }
    }

    /** The run is over. The tone tells a phone in a pocket, and the screen says why. */
    private void end(String why) {
        if (!started) return;
        status.why = why;
        speaker.warnTone();
        main.postDelayed(this::stopSelf, STOP_AFTER_TONE_MS);
    }

    /** A spoken yes needs the fingerprint in the app. The notification says only that something waits. */
    private void showConfirm(JSONObject confirm) {
        status.confirm = confirm;
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (confirm == null) {
            nm.cancel(CONFIRM_NOTIFICATION);
            return;
        }
        nm.createNotificationChannel(new NotificationChannel(CONFIRM_CHANNEL, "Confirmations", NotificationManager.IMPORTANCE_HIGH));
        PendingIntent open = PendingIntent.getActivity(this, 2, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        nm.notify(CONFIRM_NOTIFICATION, new Notification.Builder(this, CONFIRM_CHANNEL)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle("Walkie Talkie")
                .setContentText("Something waits for your fingerprint.")
                .setVisibility(Notification.VISIBILITY_PRIVATE)
                .setContentIntent(open)
                .setAutoCancel(true)
                .build());
    }

    private void answerConfirm(Intent intent, boolean confirmed) {
        JSONObject confirm = status.confirm;
        String nonce = intent.getStringExtra(EXTRA_NONCE);
        if (confirm == null || nonce == null || !nonce.equals(confirm.optString("nonce"))) return;
        showConfirm(null);
        try {
            JSONObject message = new JSONObject().put("nonce", nonce);
            if (confirmed) message.put("sig", intent.getStringExtra(EXTRA_SIG));
            send(message, confirmed ? "confirmed" : "declined");
        } catch (JSONException e) {
            Log.e(TAG, "bad message");
        }
    }

    @Override public void onConnection(boolean connected, String why) {
        // The phone may be locked in a pocket, so this is the only sign that the link went down.
        if (status.connected && !connected) speaker.warnTone();
        status.connected = connected;
        status.why = why;
        if (!connected) speaker.releaseHold(); // nobody can answer it now
        Log.i(TAG, connected ? "connected" : "disconnected");
    }

    @Override public void onGatewayReset() {
        speaker.resetGenerations();
    }

    @Override public void onSpeechStarted() {
        try {
            send(new JSONObject().put("rtt", link.roundTripMs()), "played");
        } catch (JSONException e) {
            Log.e(TAG, "bad message");
        }
    }

    /** Only after a fingerprint: approve carries its signature over the run and the session. */
    static void start(Context context, String run, JSONObject approve, String name) {
        status.why = null;
        status.pairingOver = false;
        status.running = true; // from now on, so the screen opens no list connection, which would end this run
        context.startForegroundService(new Intent(context, TalkService.class).setAction(ACTION_START)
                .putExtra(EXTRA_RUN, run).putExtra(EXTRA_APPROVE, approve.toString()).putExtra(EXTRA_NAME, name));
    }
}
