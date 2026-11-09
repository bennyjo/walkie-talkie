package dev.walkietalkie;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.text.SpannableStringBuilder;
import android.text.Spanned;
import android.text.style.ForegroundColorSpan;
import android.text.style.RelativeSizeSpan;
import android.util.TypedValue;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.IOException;
import java.security.GeneralSecurityException;
import java.security.Signature;
import java.util.Locale;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * One screen: pair with the Mac, choose a session, and tap Start. Start asks for a fingerprint, every
 * time, and the fingerprint signs that one session for that one run. A read-back that needs a yes is
 * confirmed here with a fingerprint too. Like a banking app, the screen can't be captured.
 */
public class MainActivity extends Activity {
    private static final long REFRESH_MS = 500;
    private static final int BRAND_GREEN = 0xFF1F6F5C; // white text stays readable on it, day and night
    private static final int WARN_DAYS = 3;

    private final Handler main = new Handler(Looper.getMainLooper());
    private LinearLayout pairSection;
    private LinearLayout pairedSection;
    private LinearLayout talkSection;
    private LinearLayout confirmSection;
    private TextView pairedLabel;
    private TextView pairMessage;
    private EditText addressField;
    private EditText codeField;
    private Button startButton;
    private TextView statusLine;
    private TextView sessionsHint;
    private RadioGroup sessionsGroup;
    private TextView voicesHint;
    private RadioGroup voicesGroup;
    private String shownVoices;
    private TextView confirmText;
    private TextView details;
    private String shownSessions;
    private boolean resumed;

    // The list of sessions while not talking: a link that can see them, and nothing else.
    private GatewayLink browse;
    private volatile boolean browseConnected;
    private volatile String browseWhy;
    private volatile JSONObject browseSessions;
    private volatile int browseDays = -1;
    private volatile boolean pairingOver;
    private JSONObject picked; // the row chosen, until Start
    private String message;    // what went wrong with the last Start, until the next choice
    private boolean asking;    // a fingerprint prompt is up
    private boolean startWanted; // Start was tapped, and waits for the list's connection, as after a permission dialog

    private final Runnable refresh = new Runnable() {
        @Override public void run() {
            render();
            main.postDelayed(this, REFRESH_MS);
        }
    };

    @Override protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        if (!Variant.TEST) getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(24);
        root.setPadding(pad, pad, pad, pad);

        TextView title = text(28);
        title.setText("Walkie Talkie");
        root.addView(title);

        statusLine = text(16);
        statusLine.setPadding(0, dp(8), 0, dp(24));
        root.addView(statusLine);

        pairSection = section();
        pairSection.addView(label("Pair with your Mac"));
        addressField = field("Mac's Tailscale address, 100.x.y.z",
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        codeField = field("8-digit pairing code", InputType.TYPE_CLASS_NUMBER);
        pairSection.addView(addressField);
        pairSection.addView(codeField);
        pairSection.addView(button("Pair", this::pair));
        pairMessage = text(14);
        pairMessage.setPadding(0, dp(8), 0, 0);
        pairSection.addView(pairMessage);
        root.addView(pairSection);

        confirmSection = section();
        confirmSection.addView(label("Confirm with your fingerprint"));
        confirmText = text(16);
        confirmText.setPadding(0, 0, 0, dp(8));
        confirmSection.addView(confirmText);
        Button confirmButton = button("Confirm", this::confirm);
        confirmButton.setBackgroundTintList(ColorStateList.valueOf(BRAND_GREEN));
        confirmButton.setTextColor(Color.WHITE);
        confirmSection.addView(confirmButton);
        confirmSection.addView(button("Not now", this::decline));
        root.addView(confirmSection);

        talkSection = section();
        talkSection.addView(label("Session"));
        sessionsHint = text(14);
        talkSection.addView(sessionsHint);
        sessionsGroup = new RadioGroup(this);
        talkSection.addView(sessionsGroup);
        talkSection.addView(label("Voice"));
        voicesHint = text(14);
        talkSection.addView(voicesHint);
        voicesGroup = new RadioGroup(this);
        talkSection.addView(voicesGroup);
        startButton = button("Start", this::toggle);
        startButton.setBackgroundTintList(ColorStateList.valueOf(BRAND_GREEN));
        startButton.setTextColor(Color.WHITE);
        LinearLayout.LayoutParams startSpace = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        startSpace.topMargin = dp(16);
        talkSection.addView(startButton, startSpace);
        talkSection.addView(label("Audio"));
        details = text(14);
        details.setTextIsSelectable(true);
        talkSection.addView(details);
        root.addView(talkSection);

        pairedSection = section();
        pairedLabel = text(14);
        pairedLabel.setPadding(0, 0, 0, dp(8));
        pairedSection.addView(pairedLabel);
        pairedSection.addView(button("Pair again", this::forget));
        root.addView(pairedSection);

        ScrollView scroll = new ScrollView(this);
        scroll.setFitsSystemWindows(true);
        scroll.addView(root);
        setContentView(scroll);
    }

    @Override protected void onResume() {
        super.onResume();
        resumed = true;
        main.post(refresh);
    }

    @Override protected void onPause() {
        resumed = false;
        main.removeCallbacks(refresh);
        if (!asking) stopBrowse();
        super.onPause();
    }

    // --- the list, before Start ---------------------------------------------------

    private final GatewayLink.Listener browseListener = new GatewayLink.Listener() {
        @Override public void onSpeech(int generation, byte[] packet, int offset, int length) { }

        @Override public void onMessage(JSONObject message) {
            String kind = message.optString("t");
            if (kind.equals("sessions")) browseSessions = message;
            if (kind.equals("nonce")) main.post(() -> askStart(message.optString("nonce")));
        }

        @Override public void onConnection(boolean connected, String why) {
            browseConnected = connected;
            if (why != null) browseWhy = why;
            if (connected) browseWhy = null;
        }

        @Override public void onGatewayReset() { }

        @Override public boolean onLogin(JSONObject answer) {
            if (answer.optBoolean("expired")) {
                pairingOver = true;
                return false;
            }
            browseDays = answer.optInt("days", -1);
            return true;
        }
    };

    private void startBrowse(Pairing pairing) {
        browseConnected = false;
        browseSessions = null;
        browse = new GatewayLink(pairing, browseListener);
        browse.start();
    }

    private void stopBrowse() {
        if (browse == null) return;
        browse.stop();
        browse = null;
        browseConnected = false;
    }

    // --- actions -----------------------------------------------------------------

    private void pair() {
        String address = addressField.getText().toString().trim();
        String code = codeField.getText().toString().trim();
        if (!Pairing.allowedAddress(address)) {
            pairMessage.setText("That isn't a Tailscale address. It starts with 100.");
            return;
        }
        pairMessage.setText("Pairing");
        new Thread(() -> {
            String shown = "";
            try {
                String key = PhoneKey.create();
                Pairing.save(this, address, GatewayLink.pair(address, code, key));
                pairingOver = false;
            } catch (PhoneKey.Unavailable e) {
                shown = e.getMessage();
            } catch (IOException | JSONException | GeneralSecurityException | RuntimeException e) {
                PhoneKey.delete();
                shown = "That didn't work. Check the address, and that the code is still valid.";
            }
            String result = shown;
            main.post(() -> {
                pairMessage.setText(result);
                codeField.setText("");
                render();
            });
        }, "walkie-pair").start();
    }

    private void forget() {
        stopService(new Intent(this, TalkService.class));
        stopBrowse();
        Pairing.forget(this);
        pairingOver = false;
        picked = null;
        message = null;
        render();
    }

    private void toggle() {
        if (TalkService.status.running) {
            stopService(new Intent(this, TalkService.class));
        } else if (picked == null) {
            message = "Choose a session first.";
            render();
        } else if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS}, 1);
        } else {
            startWanted = true;
            render();
        }
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        // The dialog paused this screen, which closed the list's connection: Start goes on once it's back.
        startWanted = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    /** Start asks the Mac for a fresh challenge, and the fingerprint signs it. */
    private void requestStart() {
        startWanted = false;
        try {
            browse.sendControl(new JSONObject().put("t", "nonce"));
        } catch (JSONException e) {
            message = "That didn't work. Tap Start again.";
        }
    }

    private void askStart(String nonce) {
        JSONObject session = picked;
        if (session == null || nonce.isEmpty() || TalkService.status.running || !resumed) return;
        String name = rowName(session);
        String run = GatewayLink.newRun();
        String signed = GatewayLink.startMessage(nonce, run, session.optString("id"), session.optString("nonce"));
        fingerprint("Connect to " + name + "?", "Your fingerprint lets this phone talk to that session until you stop.", signed, signature -> {
            try {
                JSONObject approve = new JSONObject()
                        .put("session", session.optString("id"))
                        .put("modNonce", session.optString("nonce"))
                        .put("nonce", nonce)
                        .put("sig", signature);
                stopBrowse();
                message = null;
                TalkService.start(this, run, approve, name);
            } catch (JSONException e) {
                message = "That didn't work. Tap Start again.";
            }
        });
    }

    private void confirm() {
        JSONObject confirm = TalkService.status.confirm;
        if (confirm == null) return;
        String nonce = confirm.optString("nonce");
        String text = confirm.optString("text");
        String signed;
        try {
            signed = GatewayLink.confirmMessage(nonce, text);
        } catch (GeneralSecurityException e) {
            return;
        }
        fingerprint("Confirm", text, signed, signature -> startService(new Intent(this, TalkService.class)
                .setAction(TalkService.ACTION_CONFIRMED)
                .putExtra(TalkService.EXTRA_NONCE, nonce)
                .putExtra(TalkService.EXTRA_SIG, signature)));
    }

    private void decline() {
        JSONObject confirm = TalkService.status.confirm;
        if (confirm == null) return;
        startService(new Intent(this, TalkService.class)
                .setAction(TalkService.ACTION_DECLINED)
                .putExtra(TalkService.EXTRA_NONCE, confirm.optString("nonce")));
    }

    private interface Signed {
        void with(String signature);
    }

    /** A strong fingerprint unlocks the hardware key for one signature over exactly these words. No PIN instead. */
    private void fingerprint(String title, String description, String words, Signed then) {
        Signature signer;
        try {
            signer = PhoneKey.signer();
        } catch (GeneralSecurityException | IOException e) {
            message = "This phone's key has gone, because a fingerprint changed or the pairing ran out. Tap Pair again.";
            render();
            return;
        }
        if (getSystemService(BiometricManager.class).canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG)
                != BiometricManager.BIOMETRIC_SUCCESS) {
            message = "Add a fingerprint in the phone's settings first.";
            render();
            return;
        }
        BiometricPrompt prompt = new BiometricPrompt.Builder(this)
                .setTitle(title)
                .setDescription(description)
                .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG)
                .setNegativeButton("Cancel", getMainExecutor(), (dialog, which) -> { })
                .build();
        asking = true;
        prompt.authenticate(new BiometricPrompt.CryptoObject(signer), new CancellationSignal(), getMainExecutor(),
                new BiometricPrompt.AuthenticationCallback() {
                    @Override public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
                        asking = false;
                        try {
                            then.with(PhoneKey.sign(result.getCryptoObject().getSignature(), words));
                        } catch (GeneralSecurityException e) {
                            message = "The fingerprint didn't unlock the key. Try again.";
                        }
                        render();
                    }

                    @Override public void onAuthenticationError(int code, CharSequence why) {
                        asking = false;
                        message = code == BiometricPrompt.BIOMETRIC_ERROR_USER_CANCELED
                                || code == BiometricPrompt.BIOMETRIC_ERROR_CANCELED ? null : why + ".";
                        if (!resumed) stopBrowse();
                        render();
                    }
                });
    }

    private void chooseVoice(String voice) {
        startService(new Intent(this, TalkService.class)
                .setAction(TalkService.ACTION_VOICE).putExtra(TalkService.EXTRA_VOICE, voice));
    }

    // --- drawing -----------------------------------------------------------------

    private void render() {
        String host = Pairing.host(this);
        TalkService.Status status = TalkService.status;
        boolean paired = host != null;
        pairSection.setVisibility(paired ? View.GONE : View.VISIBLE);
        talkSection.setVisibility(paired ? View.VISIBLE : View.GONE);
        pairedSection.setVisibility(paired ? View.VISIBLE : View.GONE);
        confirmSection.setVisibility(status.running && status.confirm != null ? View.VISIBLE : View.GONE);
        if (!paired) {
            stopBrowse();
            show(statusLine, "Not paired yet.");
            return;
        }
        if (status.pairingOver) pairingOver = true;
        if (resumed && !status.running && browse == null && !pairingOver) {
            Pairing pairing = Pairing.load(this);
            if (pairing != null) startBrowse(pairing);
        }
        if (startWanted && browse != null && browseConnected && picked != null && !asking) requestStart();
        if (status.confirm != null) show(confirmText, status.confirm.optString("text"));
        show(pairedLabel, "Paired with " + host);
        show(startButton, status.running ? "Stop" : "Start");
        show(statusLine, statusText(status));
        renderSessions(status);
        renderVoices(status);
        show(details, status.running ? detailsText(status)
                : "Connect your USB mic and your headphones before you start.");
    }

    /** Changes the text only when it changed, so an idle screen does no drawing. */
    private static void show(TextView view, String value) {
        if (!value.contentEquals(view.getText())) view.setText(value);
    }

    private String statusText(TalkService.Status status) {
        if (pairingOver) return "This pairing is over 30 days old. Tap Pair again, with a new code from the Mac.";
        if (status.running) {
            if (status.confirm != null) return "Confirm below with your fingerprint, or tap Not now.";
            if (!status.connected) return "Connecting to the Mac.";
            return "Talking to " + status.session + ". Lock the phone, and talk." + daysLeft(status.pairingDays);
        }
        if (message != null) return message;
        if (status.why != null) return status.why + ".";
        if (!browseConnected) return browseWhy != null ? browseWhy + "." : "Connecting to your Mac.";
        if (startWanted) return "Asking your Mac to start.";
        return "Choose a session, then tap Start." + daysLeft(browseDays);
    }

    private static String daysLeft(int days) {
        if (days < 0 || days > WARN_DAYS) return "";
        return days <= 1 ? " The pairing ends today: pair again at the Mac."
                : String.format(Locale.ROOT, " The pairing ends in %d days: pair again at the Mac.", days);
    }

    private void renderSessions(TalkService.Status status) {
        JSONObject sessions = status.running ? null : browseSessions;
        String key = (status.running ? "on:" + status.session : "off:") + (sessions == null ? "" : sessions.toString())
                + ":" + browseConnected;
        if (key.equals(shownSessions)) return;
        shownSessions = key;
        sessionsGroup.removeAllViews();
        if (status.running) {
            sessionsHint.setVisibility(View.VISIBLE);
            sessionsHint.setText(withQuietLine(status.session, "To talk to another session, tap Stop."));
            return;
        }
        JSONArray list = sessions == null ? null : sessions.optJSONArray("list");
        if (list == null || list.length() == 0) {
            if (sessions != null) picked = null; // a list with nothing in it; while none has come yet, the choice stands
            sessionsHint.setVisibility(View.VISIBLE);
            sessionsHint.setText(!browseConnected || sessions == null
                    ? "Sessions show here once your Mac answers."
                    : "Connected. No session has the mod running. In the session, run /reload-plugins. "
                    + "If that says 0 plugins, install the mod. Worker sessions never show here.");
            return;
        }
        sessionsHint.setVisibility(View.GONE);
        String pickedId = picked == null ? null : picked.optString("id");
        picked = null;
        for (int i = 0; i < list.length(); i++) {
            JSONObject session = list.optJSONObject(i);
            if (session == null) continue;
            RadioButton option = new RadioButton(this);
            option.setText(rowText(session));
            option.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
            option.setPadding(dp(8), dp(10), 0, dp(10));
            sessionsGroup.addView(option);
            if (session.optString("id").equals(pickedId)) {
                option.setChecked(true);
                picked = session; // its challenge may be new, so keep the row as the Mac last sent it
            }
            option.setOnClickListener(v -> {
                picked = session;
                message = null;
                TalkService.status.why = null;
                render();
            });
        }
    }

    private void renderVoices(TalkService.Status status) {
        JSONObject voices = status.running ? status.voices : null;
        String key = (status.running ? "on:" : "off:") + (voices == null ? "" : voices.toString());
        if (key.equals(shownVoices)) return;
        shownVoices = key;
        voicesGroup.removeAllViews();
        JSONArray list = voices == null ? null : voices.optJSONArray("list");
        if (list == null || list.length() == 0) {
            voicesHint.setVisibility(View.VISIBLE);
            voicesHint.setText("Voices show here once you start.");
            return;
        }
        voicesHint.setVisibility(View.GONE);
        String chosen = voices.optString("chosen");
        for (int i = 0; i < list.length(); i++) {
            JSONObject voice = list.optJSONObject(i);
            String id = voice.optString("id");
            RadioButton option = new RadioButton(this);
            option.setText(voice.optString("name"));
            option.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
            option.setPadding(dp(8), dp(10), 0, dp(10));
            voicesGroup.addView(option);
            option.setChecked(id.equals(chosen));
            option.setOnClickListener(v -> chooseVoice(id));
        }
    }

    private static String rowName(JSONObject session) {
        return session.optString("name") + "  " + session.optString("session");
    }

    /** The name, and under it in smaller, quieter text, when the session last took a prompt. */
    private CharSequence rowText(JSONObject session) {
        String ago = ago(session);
        return ago.isEmpty() ? rowName(session) : withQuietLine(rowName(session), ago);
    }

    private CharSequence withQuietLine(String first, String second) {
        SpannableStringBuilder text = new SpannableStringBuilder(first);
        int start = text.length();
        text.append('\n').append(second);
        text.setSpan(new RelativeSizeSpan(0.85f), start, text.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        text.setSpan(new ForegroundColorSpan(getColor(android.R.color.darker_gray)), start, text.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        return text;
    }

    private static String ago(JSONObject session) {
        if (session.isNull("ago")) return "";
        long seconds = session.optLong("ago");
        if (seconds < 60) return "Last prompt just now";
        if (seconds < 3600) return String.format(Locale.ROOT, "Last prompt %d min ago", seconds / 60);
        if (seconds < 86400) return String.format(Locale.ROOT, "Last prompt %d h ago", seconds / 3600);
        return String.format(Locale.ROOT, "Last prompt %d days ago", seconds / 86400);
    }

    private String detailsText(TalkService.Status status) {
        return String.format(Locale.ROOT, "Mic: %s\nMic channels: %s\nMic level: %.0f dB\nBackground: %.0f dB\nEarbud leak: %s\nSound: %s\nMode: %s\nMic silenced: %s\nRound trip: %d ms\nEarbud taps: %d\nTalked over: %d, resumed: %d\nUnderruns: %d",
                routeName(status.micRoute), channelsName(status.micChannels),
                status.micLevel, status.micFloor, status.micLeak > -100 ? String.format(Locale.ROOT, "%.0f dB", status.micLeak) : "none yet",
                routeName(status.soundRoute),
                status.audioMode == AudioManager.MODE_NORMAL ? "normal" : "call (" + status.audioMode + ")",
                status.micSilenced ? "yes" : "no",
                status.roundTripMs, status.taps, status.talkOvers, status.resumedTalkOvers, status.underruns);
    }

    private static String channelsName(int channels) {
        switch (channels) {
            case 1:
                return "left only";
            case 2:
                return "right only";
            case 3:
                return "both";
            default:
                return "no sound";
        }
    }

    private static String routeName(int type) {
        switch (type) {
            case AudioDeviceInfo.TYPE_USB_DEVICE:
            case AudioDeviceInfo.TYPE_USB_HEADSET:
                return "USB";
            case AudioDeviceInfo.TYPE_BLUETOOTH_A2DP:
                return "Bluetooth A2DP";
            case AudioDeviceInfo.TYPE_BLE_HEADSET:
                return "Bluetooth LE";
            case AudioDeviceInfo.TYPE_BUILTIN_MIC:
                return "phone mic (off)";
            case AudioDeviceInfo.TYPE_BUILTIN_SPEAKER:
                return "phone speaker (silent)";
            case -1:
                return "none";
            default:
                return "other (" + type + ")";
        }
    }

    private LinearLayout section() {
        LinearLayout section = new LinearLayout(this);
        section.setOrientation(LinearLayout.VERTICAL);
        section.setPadding(0, 0, 0, dp(24));
        return section;
    }

    private TextView text(int sp) {
        TextView view = new TextView(this);
        view.setTextAppearance(android.R.style.TextAppearance_DeviceDefault);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        return view;
    }

    private TextView label(String value) {
        TextView view = text(14);
        view.setText(value);
        view.setPadding(0, dp(20), 0, dp(6));
        view.setTypeface(view.getTypeface(), Typeface.BOLD);
        return view;
    }

    private EditText field(String hint, int inputType) {
        EditText view = new EditText(this);
        view.setHint(hint);
        view.setInputType(inputType);
        view.setSingleLine(true);
        return view;
    }

    private Button button(String value, Runnable action) {
        Button view = new Button(this);
        view.setText(value);
        view.setAllCaps(false);
        view.setOnClickListener(v -> action.run());
        return view;
    }

    private int dp(int value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics()));
    }
}
