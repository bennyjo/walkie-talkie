package dev.walkietalkie;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.media.AudioDeviceCallback;
import android.media.AudioDeviceInfo;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
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
import java.util.List;
import java.util.Objects;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Runs the conversation with the phone locked. Started from a tap in MainActivity, because a
 * microphone service started from the background gets no mic access. It can't restart itself after
 * Android kills it, for the same reason, so it's not sticky: a tap on the app brings it back.
 *
 * It starts only with a fingerprint's signature for one session, and it stops for good when the
 * gateway ends that run. The next call takes a new fingerprint, like a banking app's next login.
 * A switch to another session, spoken or tapped in the app's list, keeps the run, and takes a
 * fingerprint in the app too.
 */
public class TalkService extends Service implements GatewayLink.Listener, SpeakerPipe.Listener, MicPipe.Listener {
    static final String ACTION_START = "dev.walkietalkie.START";
    static final String ACTION_STOP = "dev.walkietalkie.STOP";
    static final String ACTION_VOICE = "dev.walkietalkie.VOICE";
    static final String ACTION_CONFIRMED = "dev.walkietalkie.CONFIRMED";
    static final String ACTION_DECLINED = "dev.walkietalkie.DECLINED";
    static final String ACTION_SWITCH = "dev.walkietalkie.SWITCH";
    static final String ACTION_MIC = "dev.walkietalkie.MIC";
    static final String ACTION_TAP = "dev.walkietalkie.TAP";
    static final String EXTRA_VOICE = "voice";
    static final String EXTRA_RUN = "run";
    static final String EXTRA_APPROVE = "approve";
    static final String EXTRA_NAME = "name";
    static final String EXTRA_NONCE = "nonce";
    static final String EXTRA_SIG = "sig";
    static final String EXTRA_SESSION = "session";

    private static final String TAG = "walkie";
    private static final String CHANNEL = "talk";
    private static final String CONFIRM_CHANNEL = "confirm";
    private static final int NOTIFICATION = 1;
    private static final int CONFIRM_NOTIFICATION = 2;
    private static final long STOP_AFTER_TONE_MS = 700;
    private static final long MAX_RUN_MS = 4 * 60 * 60 * 1000L; // a forgotten microphone stops by itself
    private static final long CHECK_EVERY_MS = 1000;
    private static final long TAP_DEBOUNCE_MS = 300;
    private static final long ROUTE_SETTLE_MS = 2500; // a headset's call link takes a moment to come up
    private static final String SETTINGS = "settings";
    private static final String MIC = "mic";

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
        volatile String run;        // this run, which a fingerprint signs again to switch sessions
        volatile int pairingDays = -1;
        volatile JSONObject confirm; // a read-back waiting for a fingerprint, or null
        volatile boolean pairingOver; // the Mac wants this phone to pair again
        volatile boolean replying;   // a reply is sounding in the earbuds
        volatile boolean hearing;    // the mic hears someone speak
        volatile String problem;     // what stops the call from working, such as no USB mic, as sentences, or null
        volatile boolean onSpeaker;  // replies play on the phone's speaker, so talking over them is off
    }

    static final Status status = new Status();

    private final Handler main = new Handler(Looper.getMainLooper());
    private AudioManager audio;
    private PowerManager.WakeLock wake;
    private MediaSession session;
    private GatewayLink link;
    private MicPipe mic;
    private SpeakerPipe speaker;
    private Ducker ducker;
    private PhoneVoice voice;
    private boolean started;
    private MicChoice.Plan plan;
    private boolean inCall;          // this app put Android in call mode, for the headphones' mic
    private long settleUntil;        // a problem is announced only once the routes have had time to settle
    private String announced;        // the spoken notice for the problem now, once it has played
    private String shownText = "";
    private long lastTap;
    private String tappedSwitch; // a session tapped in the open app, whose fingerprint needs no notification

    private final AudioDeviceCallback deviceWatch = new AudioDeviceCallback() {
        @Override public void onAudioDevicesAdded(AudioDeviceInfo[] devices) {
            applyRoutes();
        }
        @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] devices) {
            // Lost earbuds end the call at once, with no wait. A phone taken from the wearer mid-call must not
            // keep working, and a new call needs the fingerprint. Never in the emulator's build, which has no earbuds.
            // In call mode, the headphones' call route counts too, and their media route may go while it stays.
            int[] removed = types(devices);
            boolean lost = EarbudRoutes.anyEarbuds(removed)
                    || inCall && MicChoice.first(MicChoice.HEADSET_ROUTES, removed) >= 0;
            boolean callStays = inCall && MicChoice.first(MicChoice.HEADSET_ROUTES, types(outputs())) >= 0;
            if (started && !Variant.anyRoute(TalkService.this) && lost && !callStays) {
                Log.i(TAG, "the earbuds are gone, so the call ends");
                hangUp("The earbuds disconnected, so the call ended. Tap a session to connect again");
                return;
            }
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
        } else if (ACTION_SWITCH.equals(action)) {
            if (started) askSwitch(intent.getStringExtra(EXTRA_SESSION));
        } else if (ACTION_MIC.equals(action)) {
            if (started) applyRoutes();
        } else if (ACTION_TAP.equals(action)) {
            if (started) tapped();
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

        link = new GatewayLink(this, pairing, this, run, approve);
        ducker = new Ducker(new Ducker.Focus() {
            // May-duck focus makes Android lower other apps' audio, such as music, and restore it on abandon.
            private final AudioFocusRequest request = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .build();

            @Override public void request() {
                audio.requestAudioFocus(request);
            }

            @Override public void abandon() {
                audio.abandonAudioFocusRequest(request);
            }
        }, SystemClock::elapsedRealtime);
        speaker = new SpeakerPipe(this);
        mic = new MicPipe(this, link, speaker, this);
        plan = newPlan();
        settleUntil = SystemClock.elapsedRealtime() + ROUTE_SETTLE_MS;
        try {
            callMode();
            speaker.start(plan.call, output());
            mic.start(plan.call, input());
        } catch (IOException | RuntimeException e) {
            Log.e(TAG, "couldn't start audio: " + e.getClass().getSimpleName());
            ducker.close();
            speaker.stop();
            leaveCallMode();
            status.why = "Couldn't start audio";
            stopSelf();
            return;
        }
        voice = new PhoneVoice(this, speaker);
        started = true;
        status.running = true;
        status.why = null;
        status.sessions = null;
        status.voices = null;
        status.confirm = null;
        status.session = intent.getStringExtra(EXTRA_NAME);
        status.run = run;
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
        status.run = null;
        status.replying = false;
        status.hearing = false;
        status.problem = null;
        status.onSpeaker = false;
        getSystemService(NotificationManager.class).cancel(CONFIRM_NOTIFICATION);
        if (voice != null) voice.close();
        if (mic != null) mic.stop();
        if (speaker != null) speaker.stop();
        leaveCallMode();
        if (ducker != null) ducker.close();
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
        if (started) tapped();
        return true;
    }

    /** An earbud tap, or a tap on the red dot in the app: the reply stops, as does the turn. */
    private void tapped() {
        long now = SystemClock.elapsedRealtime();
        if (now - lastTap <= TAP_DEBOUNCE_MS) return;
        lastTap = now;
        status.taps++;
        tap(speaker.cut());
        Log.i(TAG, "tap");
    }

    // --- routes ------------------------------------------------------------------

    /** The mic chosen in the app. USB unless another was chosen. */
    static int micChoice(Context context) {
        return MicChoice.valid(settings(context).getInt(MIC, MicChoice.DEFAULT));
    }

    /** The mics the app offers now: those whose device is connected. The phone's mic always is. */
    static int[] micsOffered(Context context) {
        AudioManager audio = context.getSystemService(AudioManager.class);
        return MicChoice.offered(types(audio.getDevices(AudioManager.GET_DEVICES_INPUTS)),
                types(audio.getAvailableCommunicationDevices().toArray(new AudioDeviceInfo[0])));
    }

    /** Saves the choice, and a call on now moves to it. */
    static void chooseMic(Context context, int choice) {
        settings(context).edit().putInt(MIC, MicChoice.valid(choice)).apply();
        if (status.running) context.startService(new Intent(context, TalkService.class).setAction(ACTION_MIC));
    }

    private static SharedPreferences settings(Context context) {
        return context.getSharedPreferences(SETTINGS, MODE_PRIVATE);
    }

    private AudioDeviceInfo[] inputs() {
        return audio.getDevices(AudioManager.GET_DEVICES_INPUTS);
    }

    private AudioDeviceInfo[] outputs() {
        return audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS);
    }

    private static int[] types(AudioDeviceInfo[] devices) {
        int[] types = new int[devices.length];
        for (int i = 0; i < devices.length; i++) types[i] = devices[i].getType();
        return types;
    }

    /** The first device of the first wanted type that's present, or null. */
    private static AudioDeviceInfo find(AudioDeviceInfo[] devices, int[] wanted) {
        for (int type : wanted) {
            for (AudioDeviceInfo device : devices) {
                if (device.getType() == type) return device;
            }
        }
        return null;
    }

    private MicChoice.Plan newPlan() {
        List<AudioDeviceInfo> call = audio.getAvailableCommunicationDevices();
        return MicChoice.plan(micChoice(this), types(inputs()), types(outputs()),
                types(call.toArray(new AudioDeviceInfo[0])));
    }

    /** The input to prefer. In call mode, the call's device decides. */
    private AudioDeviceInfo input() {
        return plan.call ? null : find(inputs(), plan.mics);
    }

    /** The output to prefer. In call mode, the call's device decides. */
    private AudioDeviceInfo output() {
        return plan.call ? null : find(outputs(), plan.outputs);
    }

    /** Puts Android in call mode, with the headphones as the call's device, only for the headphones' mic. */
    private void callMode() {
        if (!plan.call) {
            leaveCallMode();
            return;
        }
        AudioDeviceInfo device = null;
        for (AudioDeviceInfo d : audio.getAvailableCommunicationDevices()) {
            if (d.getType() == plan.callDevice) device = d;
        }
        if (device == null) return; // gone since the plan: checkRoutes finds the mic isn't there
        if (!inCall) audio.setMode(AudioManager.MODE_IN_COMMUNICATION);
        inCall = true;
        AudioDeviceInfo now = audio.getCommunicationDevice();
        if ((now == null || now.getId() != device.getId()) && !audio.setCommunicationDevice(device)) {
            Log.i(TAG, "Android wouldn't take the headphones as the call's device");
        }
    }

    private void leaveCallMode() {
        if (!inCall) return;
        inCall = false;
        audio.clearCommunicationDevice();
        audio.setMode(AudioManager.MODE_NORMAL);
    }

    /**
     * Follows the mic choice and the devices connected now. A change between media and call mode needs new
     * streams, because a stream's kind is fixed when it's made.
     */
    private void applyRoutes() {
        if (!started) return;
        MicChoice.Plan next = newPlan();
        boolean rebuild = next.call != plan.call;
        if (next.choice != plan.choice || next.call != plan.call || next.onSpeaker() != plan.onSpeaker()) {
            settleUntil = SystemClock.elapsedRealtime() + ROUTE_SETTLE_MS;
        }
        plan = next;
        if (rebuild) {
            try {
                mic.stop();
                callMode();
                speaker.reopen(plan.call, output());
                mic = new MicPipe(this, link, speaker, this);
                mic.start(plan.call, input());
            } catch (RuntimeException e) {
                Log.e(TAG, "couldn't move the audio: " + e.getClass().getSimpleName());
                end("Couldn't move the audio to that mic");
                return;
            }
            mic.watchRoute(routeWatch, main);
            speaker.watchRoute(routeWatch, main);
        } else {
            callMode();
            mic.prefer(input());
            speaker.prefer(output());
        }
        checkRoutes();
    }

    /**
     * The privacy rules: only the chosen mic is sent, and a reply plays only on the output the choice allows:
     * the phone's speaker only with the phone's mic and no headphones. A call that can't hear you, or that you
     * can't hear, says so out loud, on whatever output there is.
     */
    private void checkRoutes() {
        int in = mic.routedType();
        int out = speaker.routedType();
        boolean anyRoute = Variant.anyRoute(this);
        boolean micOk = anyRoute || MicChoice.allowed(plan.mics, in, types(inputs()));
        boolean soundOk = anyRoute || MicChoice.allowed(plan.outputs, out, types(outputs()));
        boolean silenced = !anyRoute && (mic.silenced() || (micOk && mic.allZeros()));
        mic.setOpen(micOk);
        mic.setTalkOver(plan.talkOver);
        speaker.setMuted(!soundOk);

        String text;
        String spoken = null;
        if (!micOk) {
            text = noMicText(plan.choice);
            spoken = noMicSpoken(plan.choice);
        } else if (silenced) {
            text = "Android has silenced the mic. Another app may have taken it.";
            spoken = "Another app has the mic, so I can't hear you.";
        } else if (!soundOk) {
            boolean phone = plan.choice == MicChoice.PHONE;
            text = phone ? "Replies are silent. They play only in Bluetooth headphones, or on the phone's speaker."
                    : "Connect your headphones, or choose the phone's mic for its speaker. Replies are silent until then.";
            spoken = phone ? "Replies are silent for now." : "No headphones, so replies are silent.";
        } else {
            text = link.connected() ? listeningText(plan) : "Connecting to the Mac";
        }
        announce(spoken);
        status.problem = micOk && !silenced && soundOk ? null : text;
        status.onSpeaker = plan.onSpeaker();
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

    /** The warning tone, then the spoken notice, once for each problem, and only once the routes have settled. */
    private void announce(String spoken) {
        if (Objects.equals(spoken, announced)) return;
        if (spoken == null) {
            announced = null;
            voice.cancel();
        } else if (SystemClock.elapsedRealtime() >= settleUntil) {
            announced = spoken;
            speaker.warnTone();
            voice.say(spoken);
        }
    }

    private static String noMicText(int choice) {
        switch (choice) {
            case MicChoice.HEADSET:
                return "Connect headphones with a mic, or choose another mic. The mic is off until then.";
            case MicChoice.PHONE:
                return "The phone's mic isn't available. The mic is off until it is.";
            default:
                return "Connect your USB mic, or choose another mic. The mic is off until then.";
        }
    }

    private static String noMicSpoken(int choice) {
        switch (choice) {
            case MicChoice.HEADSET:
                return "No headphones' mic, so I can't hear you.";
            case MicChoice.PHONE:
                return "No phone mic, so I can't hear you.";
            default:
                return "No USB mic, so I can't hear you.";
        }
    }

    private static String listeningText(MicChoice.Plan plan) {
        switch (plan.choice) {
            case MicChoice.HEADSET:
                return "Listening on the headphones' mic";
            case MicChoice.PHONE:
                return plan.onSpeaker() ? "Listening on the phone's mic and speaker" : "Listening on the phone's mic";
            default:
                return "Listening on the USB mic";
        }
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
                .setColor(getColor(R.color.accent))
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

    /** A session tapped in the list. The gateway answers with a confirm for its fingerprint, as for a spoken switch. */
    private void askSwitch(String id) {
        tappedSwitch = id;
        try {
            send(new JSONObject().put("id", id), "switch");
        } catch (JSONException e) {
            Log.e(TAG, "bad session id");
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
                tappedSwitch = null;
                showChosen(message);
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
            case "notice":
                speaker.noticeTone(message.optInt("gen"));
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

    /** After a switch, the screen names the session the gateway now routes to. */
    private static void showChosen(JSONObject sessions) {
        JSONArray list = sessions.optJSONArray("list");
        if (list == null || sessions.isNull("chosen")) return;
        String chosen = sessions.optString("chosen");
        for (int i = 0; i < list.length(); i++) {
            JSONObject row = list.optJSONObject(i);
            if (row != null && chosen.equals(row.optString("id"))) status.session = MainActivity.rowName(row);
        }
    }

    private static String refusedText(String why) {
        switch (why) {
            case "gone":
                return "That session has gone. Choose another";
            case "late":
                return "That took too long. Tap the session again";
            case "changed":
                return "That session changed a moment ago. Tap it again";
            case "signature":
                return "Your Mac didn't accept this phone's fingerprint. Pair again";
            default:
                return "The call ended while the phone was away. Tap a session to connect again";
        }
    }

    private static String endedText(String why) {
        switch (why) {
            case "idle":
                return "Ended after 30 minutes with nothing said. Tap a session to connect again";
            case "gone":
                return "That session has gone. Choose another";
            case "closed":
                return "That session ended the call. Tap a session to connect again";
            default:
                return "The call ended. Tap a session to connect again";
        }
    }

    /** The call ends now: no tone, because the earbuds are the only place one could be heard. onDestroy releases the run. */
    private void hangUp(String why) {
        if (!started) return;
        status.why = why;
        stopSelf();
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
        JSONObject target = confirm.optJSONObject("switch");
        boolean tapped = target != null && target.optString("id").equals(tappedSwitch);
        tappedSwitch = null;
        if (tapped) return; // the app is open, and already asking
        nm.createNotificationChannel(new NotificationChannel(CONFIRM_CHANNEL, "Confirmations", NotificationManager.IMPORTANCE_HIGH));
        PendingIntent open = PendingIntent.getActivity(this, 2, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        nm.notify(CONFIRM_NOTIFICATION, new Notification.Builder(this, CONFIRM_CHANNEL)
                .setSmallIcon(R.drawable.ic_notification)
                .setColor(getColor(R.color.accent))
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

    @Override public void onSounding(boolean sounding) {
        status.replying = sounding;
        ducker.setReply(sounding);
    }

    @Override public void onSpeaking(boolean speaking) {
        status.hearing = speaking;
        ducker.setSpeech(speaking);
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
