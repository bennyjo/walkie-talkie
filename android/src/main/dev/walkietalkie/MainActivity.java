package dev.walkietalkie;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Insets;
import android.graphics.drawable.GradientDrawable;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.security.keystore.KeyExpiredException;
import android.security.keystore.KeyPermanentlyInvalidatedException;
import android.text.InputType;
import android.text.TextUtils;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.window.OnBackInvokedCallback;
import android.window.OnBackInvokedDispatcher;
import java.io.IOException;
import java.security.GeneralSecurityException;
import java.security.Signature;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.TreeMap;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Three screens, one shown at a time. Pair: the Mac's address and a code. Home: the sessions, each a card,
 * and a tap on one starts a call to it, and the mic the call listens on. Talk: the session the call is on, a
 * dot that shows the call's state and stops a reply when tapped, the other sessions to switch to with one
 * tap, the mic, and End.
 *
 * Start asks for a fingerprint, every time, and the fingerprint signs that one session for that one run.
 * A switch takes a fingerprint too, and the screen names the new session only once the Mac has moved the
 * call there. A read-back that needs a yes, and a spoken switch, are confirmed here with a fingerprint as
 * well. Like a banking app, the screen can't be captured.
 */
public class MainActivity extends Activity {
    private static final String TAG = "walkie";
    private static final long REFRESH_MS = 500;
    private static final long CALL_REFRESH_MS = 200; // so the dot follows your voice and the reply
    private static final int WARN_DAYS = 3;
    private static final long MESSAGE_MS = 8000; // how long a message shows during a call
    private static final long NOTICE_MS = 6000;  // how long "Now talking to" or "Not switched" shows
    private static final long SWITCH_WAIT_MS = 5000; // for the Mac to ask for the fingerprint, then to move the call
    private static final long FADE_MS = 180;
    private static final String PAIR_AGAIN = "pairAgain";
    private static final String CONNECT_WHY = "Your fingerprint lets this phone talk to that session until you stop.";
    private static final String[] MIC_NAMES = {"USB mic", "Headphones' mic", "Phone's mic"}; // by MicChoice
    private static final String[] MIC_TIPS = {
        "Plug in the USB mic and connect your headphones before you start.",
        "Connect your headphones before you start. Using their mic makes it a phone call, at call quality.",
        "Replies play in your headphones, or on the phone's speaker if none are connected. "
                + "On the speaker, wait for a reply to finish, or tap the red dot to stop it.",
    };

    private final Handler main = new Handler(Looper.getMainLooper());
    private final PairScreen pairScreen = new PairScreen();
    private Ui ui;

    // Pair
    private View pairView;
    private TextView pairHeadline;
    private EditText addressField;
    private EditText codeField;
    private TextView pairMessage;
    private Button pairButton;
    private Button cancelPairButton;

    // Home
    private View homeView;
    private TextView greeting;
    private TextView homeLine;
    private TextView homeNote;
    private TextView homeDays;
    private LinearLayout waitCard;
    private TextView waitTitle;
    private TextView waitText;
    private LinearLayout overCard;
    private LinearLayout emptyCard;
    private LinearLayout sessionsList;
    private LinearLayout homeMicRow;
    private TextView homeMicValue;
    private TextView homeTip;
    private TextView pairedLabel;

    // Talk
    private View talkView;
    private TextView talkLabel;
    private GradientDrawable liveDot;
    private Boolean shownLive;
    private TextView talkName;
    private TextView talkAccount;
    private Orb orb;
    private TextView stateTitle;
    private TextView stateHint;
    private LinearLayout confirmCard;
    private TextView confirmText;
    private TextView talkNote;
    private TextView talkDays;
    private TextView switchLabel;
    private LinearLayout switchList;
    private TextView talkMicValue;
    private LinearLayout voiceRow;
    private TextView voiceValue;
    private TextView detailsToggle;
    private TextView details;
    private boolean detailsOpen;

    private View shownScreen;
    private String shownHome;
    private String shownSwitch;
    private boolean resumed;

    // The list of sessions while not talking: a link that can see them, and nothing else.
    private GatewayLink browse;
    private volatile boolean browseConnected;
    private volatile String browseWhy;
    private volatile JSONObject browseSessions;
    private volatile int browseDays = -1;
    private volatile boolean pairingOver;
    private JSONObject picked; // the card tapped, until its call starts
    private String message;    // what went wrong with the last Start or switch, until the next choice
    private boolean asking;    // a fingerprint prompt is up
    private boolean startWanted; // a card was tapped, and waits for the list's connection, as after a permission dialog
    private JSONObject switching; // the session a switch goes to, until the Mac moves the call there or the switch fails
    private String switchingAfter; // the confirmation already waiting at that tap, which isn't the Mac's answer
    private boolean switchAsked;   // the fingerprint for that switch has been asked for
    private long switchingSince;
    private String declined;   // the confirmation last turned down here
    private String notice;     // good news during a call, such as the session it moved to
    private long noticeSince;
    private String seenMessage;
    private long messageSince;

    private final Runnable refresh = new Runnable() {
        @Override public void run() {
            render();
            main.postDelayed(this, TalkService.status.running ? CALL_REFRESH_MS : REFRESH_MS);
        }
    };

    @Override protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        if (!Variant.TEST) getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        ui = new Ui(this);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(getColor(R.color.bg));
        // Edge to edge: the screens sit inside the system bars and above the keyboard.
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime()
                    | WindowInsets.Type.displayCutout());
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return WindowInsets.CONSUMED;
        });
        pairView = buildPair();
        homeView = buildHome();
        talkView = buildTalk();
        for (View screen : new View[] {pairView, homeView, talkView}) {
            screen.setVisibility(View.GONE);
            root.addView(screen, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT,
                    FrameLayout.LayoutParams.MATCH_PARENT));
        }
        setContentView(root);
        // A theme change or a turn of the phone rebuilds the screen: an open Pair again form stays open.
        if (saved != null && saved.getBoolean(PAIR_AGAIN)) pairScreen.pairAgain();
    }

    @Override protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        out.putBoolean(PAIR_AGAIN, pairScreen.cancelShown(Pairing.host(this) != null));
    }

    // --- building the screens ------------------------------------------------------

    /** A screen: its content scrolls, and its bottom bar, if any, stays put. */
    private LinearLayout screen(LinearLayout content, View bottom) {
        LinearLayout screen = new LinearLayout(this);
        screen.setOrientation(LinearLayout.VERTICAL);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setClipToPadding(false);
        scroll.setVerticalFadingEdgeEnabled(true); // content fades out above the bottom bar, rather than being cut
        scroll.setFadingEdgeLength(ui.dp(Ui.SPACE_XL));
        content.setPadding(ui.dp(Ui.SPACE_XL), ui.dp(Ui.SPACE_L), ui.dp(Ui.SPACE_XL), ui.dp(Ui.SPACE_XL));
        scroll.addView(content);
        screen.addView(scroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        if (bottom != null) {
            bottom.setPadding(ui.dp(Ui.SPACE_XL), ui.dp(Ui.SPACE_M), ui.dp(Ui.SPACE_XL), ui.dp(Ui.SPACE_L));
            screen.addView(bottom);
        }
        return screen;
    }

    private LinearLayout column() {
        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        return column;
    }

    /** The app's name, small, at the top of Pair and Home. A plain dot stands in for a mark. */
    private View wordmark() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        View dot = new View(this);
        GradientDrawable circle = new GradientDrawable();
        circle.setShape(GradientDrawable.OVAL);
        circle.setColor(getColor(R.color.accent));
        dot.setBackground(circle);
        row.addView(dot, new LinearLayout.LayoutParams(ui.dp(10), ui.dp(10)));
        TextView name = ui.text("Walkie Talkie", Ui.SANS_MEDIUM, 15, R.color.text);
        name.setPadding(ui.dp(Ui.SPACE_S), 0, 0, 0);
        row.addView(name);
        row.setPadding(0, ui.dp(Ui.SPACE_S), 0, 0);
        return row;
    }

    private View buildPair() {
        LinearLayout content = column();
        content.addView(wordmark());
        pairHeadline = ui.display("Pair with your Mac");
        content.addView(pairHeadline, ui.below(Ui.SPACE_XXL + Ui.SPACE_S));
        content.addView(ui.muted("On your Mac, start the gateway with --pair. It shows the Mac's address "
                + "and an 8-digit code that works once, for two minutes."), ui.below(Ui.SPACE_M));

        LinearLayout card = ui.card();
        card.setPadding(ui.dp(Ui.SPACE_L), ui.dp(Ui.SPACE_L), ui.dp(Ui.SPACE_L), ui.dp(Ui.SPACE_L + 4));
        card.addView(ui.label("Mac's Tailscale address"));
        addressField = ui.field("100.x.y.z",
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        card.addView(addressField, ui.below(Ui.SPACE_S));
        TextView codeLabel = ui.label("Pairing code");
        card.addView(codeLabel, ui.below(Ui.SPACE_L + 4));
        codeField = ui.field("8 digits", InputType.TYPE_CLASS_NUMBER);
        card.addView(codeField, ui.below(Ui.SPACE_S));
        content.addView(card, ui.below(Ui.SPACE_XL));

        pairMessage = ui.note(true);
        content.addView(pairMessage, ui.below(Ui.SPACE_L));

        LinearLayout bottom = column();
        pairButton = ui.primary("Pair", this::pair);
        bottom.addView(pairButton, ui.below(0));
        cancelPairButton = ui.quiet("Cancel", this::cancelPair);
        bottom.addView(cancelPairButton, ui.below(Ui.SPACE_XS));
        return screen(content, bottom);
    }

    private View buildHome() {
        LinearLayout content = column();
        content.addView(wordmark());
        greeting = ui.display("");
        content.addView(greeting, ui.below(Ui.SPACE_XXL + Ui.SPACE_S));
        homeLine = ui.muted("");
        content.addView(homeLine, ui.below(Ui.SPACE_S));

        homeNote = ui.note(false);
        content.addView(homeNote, ui.below(Ui.SPACE_XL));
        homeDays = ui.note(false);
        content.addView(homeDays, ui.below(Ui.SPACE_M));

        waitCard = ui.card();
        waitTitle = ui.title("Looking for your Mac");
        waitCard.addView(waitTitle);
        waitText = ui.muted("");
        waitCard.addView(waitText, ui.below(Ui.SPACE_S));
        content.addView(waitCard, ui.below(Ui.SPACE_XL));

        overCard = ui.card();
        overCard.addView(ui.title("This pairing has ended"));
        overCard.addView(ui.muted("A pairing lasts 30 days. Start the gateway with --pair on your Mac, "
                + "then pair again with the new code."), ui.below(Ui.SPACE_S));
        overCard.addView(ui.primary("Pair again", this::pairAgain), ui.below(Ui.SPACE_L));
        content.addView(overCard, ui.below(Ui.SPACE_XL));

        emptyCard = ui.card();
        emptyCard.addView(ui.title("No sessions yet"));
        emptyCard.addView(ui.muted("In the session you want to talk to, run /\u2060reload\u2011plugins. If it says 0 plugins, "
                + "install the mod first. Worker sessions never show here."), ui.below(Ui.SPACE_S));
        content.addView(emptyCard, ui.below(Ui.SPACE_XL));

        sessionsList = column();
        content.addView(sessionsList, ui.below(Ui.SPACE_S));

        homeMicRow = row("Mic", this::chooseMicDialog);
        homeMicValue = (TextView) homeMicRow.getChildAt(1);
        content.addView(homeMicRow, ui.below(Ui.SPACE_XL));
        homeTip = ui.muted("");
        homeTip.setTextSize(14);
        content.addView(homeTip, ui.below(Ui.SPACE_M));

        // Pushes the footer to the bottom of a short page.
        content.addView(new View(this), new LinearLayout.LayoutParams(1, 0, 1f));
        LinearLayout footer = new LinearLayout(this);
        footer.setOrientation(LinearLayout.HORIZONTAL);
        footer.setGravity(Gravity.CENTER_VERTICAL);
        pairedLabel = ui.muted("");
        pairedLabel.setTextSize(14);
        footer.addView(pairedLabel, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        LinearLayout.LayoutParams flush = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        flush.setMarginEnd(-ui.dp(Ui.SPACE_L)); // the button's text lines up with the gutter
        footer.addView(ui.quiet("Pair again", this::pairAgain), flush);
        content.addView(footer, ui.below(Ui.SPACE_XL));
        return screen(content, null);
    }

    private View buildTalk() {
        LinearLayout content = column();
        content.setGravity(Gravity.CENTER_HORIZONTAL);

        LinearLayout labelRow = new LinearLayout(this);
        labelRow.setGravity(Gravity.CENTER_VERTICAL);
        View live = new View(this);
        liveDot = new GradientDrawable();
        liveDot.setShape(GradientDrawable.OVAL);
        liveDot.setColor(getColor(R.color.live));
        live.setBackground(liveDot);
        labelRow.addView(live, new LinearLayout.LayoutParams(ui.dp(8), ui.dp(8)));
        talkLabel = ui.label("Talking to");
        talkLabel.setTextColor(getColor(R.color.live));
        talkLabel.setPadding(ui.dp(Ui.SPACE_S), 0, 0, 0);
        labelRow.addView(talkLabel);
        content.addView(labelRow, centered(Ui.SPACE_L));

        talkName = ui.display("");
        talkName.setGravity(Gravity.CENTER);
        content.addView(talkName, centered(Ui.SPACE_S));
        talkAccount = ui.muted("");
        talkAccount.setGravity(Gravity.CENTER);
        content.addView(talkAccount, centered(Ui.SPACE_XS));

        orb = new Orb(this);
        orb.setContentDescription("Stop the reply");
        orb.setOnClickListener(v -> stopReply());
        LinearLayout.LayoutParams orbSpace = new LinearLayout.LayoutParams(ui.dp(240), ui.dp(240));
        orbSpace.topMargin = ui.dp(Ui.SPACE_M);
        content.addView(orb, orbSpace);
        stateTitle = ui.title("");
        stateTitle.setGravity(Gravity.CENTER);
        content.addView(stateTitle, centered(0));
        stateHint = ui.muted("");
        stateHint.setGravity(Gravity.CENTER);
        content.addView(stateHint, centered(Ui.SPACE_XS));

        confirmCard = ui.card();
        confirmCard.setPadding(ui.dp(Ui.SPACE_XL), ui.dp(Ui.SPACE_XL), ui.dp(Ui.SPACE_XL), ui.dp(Ui.SPACE_L));
        confirmCard.addView(ui.label("Waiting for your fingerprint"));
        confirmText = ui.title("");
        confirmText.setLineSpacing(0, 1.15f);
        confirmCard.addView(confirmText, ui.below(Ui.SPACE_S));
        confirmCard.addView(ui.primary("Confirm with fingerprint", this::confirm), ui.below(Ui.SPACE_XL));
        confirmCard.addView(ui.quiet("Not now", this::decline), ui.below(Ui.SPACE_XS));
        content.addView(confirmCard, ui.below(Ui.SPACE_XXL));

        talkNote = ui.note(false);
        content.addView(talkNote, ui.below(Ui.SPACE_XL));
        talkDays = ui.note(false);
        content.addView(talkDays, ui.below(Ui.SPACE_M));

        switchLabel = ui.label("Switch to");
        content.addView(switchLabel, ui.below(Ui.SPACE_XXL));
        switchList = column();
        content.addView(switchList, ui.below(0));

        LinearLayout micRow = row("Mic", this::chooseMicDialog);
        talkMicValue = (TextView) micRow.getChildAt(1);
        content.addView(micRow, ui.below(Ui.SPACE_XL));

        voiceRow = row("Voice", this::chooseVoiceDialog);
        voiceValue = (TextView) voiceRow.getChildAt(1);
        content.addView(voiceRow, ui.below(Ui.SPACE_S));

        LinearLayout detailsRow = row("Audio details", () -> {
            detailsOpen = !detailsOpen;
            render();
        });
        detailsToggle = (TextView) detailsRow.getChildAt(1);
        content.addView(detailsRow, ui.below(Ui.SPACE_S));
        details = ui.muted("");
        details.setTextSize(13);
        details.setTextIsSelectable(true);
        details.setTypeface(android.graphics.Typeface.MONOSPACE);
        details.setPadding(ui.dp(Ui.SPACE_L + 4), ui.dp(Ui.SPACE_M), ui.dp(Ui.SPACE_L), 0);
        content.addView(details, ui.below(0));

        LinearLayout bottom = column();
        bottom.addView(ui.primary("End call", this::endCall), ui.below(0));
        return screen(content, bottom);
    }

    private LinearLayout.LayoutParams centered(int topDp) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        params.topMargin = ui.dp(topDp);
        params.gravity = Gravity.CENTER_HORIZONTAL;
        return params;
    }

    /** A card-like row with a name on the left and a value on the right, which opens something. */
    private LinearLayout row(String name, Runnable action) {
        LinearLayout row = ui.tappableCard(action);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(ui.dp(Ui.SPACE_L + 4), ui.dp(Ui.SPACE_L), ui.dp(Ui.SPACE_L + 4), ui.dp(Ui.SPACE_L));
        TextView left = ui.body(name);
        row.addView(left, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        TextView right = ui.muted("");
        right.setGravity(Gravity.END);
        right.setSingleLine(true);
        right.setEllipsize(TextUtils.TruncateAt.END);
        row.addView(right, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.4f));
        return row;
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

    // --- the list, before a call ---------------------------------------------------

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
        browse = new GatewayLink(this, pairing, browseListener);
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
            render();
            return;
        }
        if (!pairScreen.pairing()) return;
        pairMessage.setText("");
        render();
        new Thread(() -> {
            String shown = "";
            boolean paired = false;
            try {
                PairSwap.run(Pairing.vault(this), address, key -> GatewayLink.pair(this, address, code, key));
                paired = true;
            } catch (PhoneKey.Unavailable e) {
                shown = e.getMessage();
            } catch (Exception e) {
                shown = "That didn't work. Check the address, and that the code is still valid.";
            }
            String result = shown;
            boolean replaced = paired;
            main.post(() -> {
                pairMessage.setText(result);
                codeField.setText("");
                if (replaced) newPairing();
                else pairScreen.failed();
                render();
            });
        }, "walkie-pair").start();
    }

    /** Opens the pairing form. The saved pairing stays until a new one succeeds. */
    private void pairAgain() {
        pairScreen.pairAgain();
        pairMessage.setText("");
        render();
    }

    private void cancelPair() {
        if (pairScreen.cancel()) closePair();
    }

    private void closePair() {
        hideKeyboard();
        addressField.setText("");
        codeField.setText("");
        pairMessage.setText("");
        render();
    }

    /** The pairing form's fields are the only text fields, so the keyboard goes when the form does. */
    private void hideKeyboard() {
        View focused = getCurrentFocus();
        if (focused != null) {
            getSystemService(InputMethodManager.class).hideSoftInputFromWindow(focused.getWindowToken(), 0);
            focused.clearFocus();
        }
    }

    /** Back closes the pairing form when there is a pairing to return to. Registered only then, so Back is the system's otherwise. */
    private final OnBackInvokedCallback closeForm = () -> {
        if (pairScreen.back(Pairing.host(this) != null)) closePair();
    };
    private boolean closeFormRegistered;

    private void watchBack(boolean wanted) {
        if (wanted == closeFormRegistered) return;
        closeFormRegistered = wanted;
        if (wanted) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT, closeForm);
        } else {
            getOnBackInvokedDispatcher().unregisterOnBackInvokedCallback(closeForm);
        }
    }

    /** A new pairing has replaced the old one, so whatever ran on the old one is over. */
    private void newPairing() {
        stopService(new Intent(this, TalkService.class));
        stopBrowse();
        pairScreen.paired();
        hideKeyboard();
        pairingOver = false;
        TalkService.status.pairingOver = false;
        picked = null;
        message = null;
    }

    /** A card tapped on Home: the call to that session starts, after the fingerprint. */
    private void startWith(JSONObject session) {
        if (TalkService.status.running || startWanted || asking) return;
        message = null;
        picked = session;
        TalkService.status.why = null;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS}, 1);
        } else {
            startWanted = true;
        }
        render();
    }

    private void endCall() {
        message = null;
        notice = null;
        switching = null;
        stopService(new Intent(this, TalkService.class));
        render();
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        // The dialog paused this screen, which closed the list's connection: the call goes on once it's back.
        startWanted = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    /** Starting asks the Mac for a fresh challenge, and the fingerprint signs it. */
    private void requestStart() {
        startWanted = false;
        try {
            browse.sendControl(new JSONObject().put("t", "nonce"));
        } catch (JSONException e) {
            message = "That didn't work. Tap the session again.";
        }
    }

    private void askStart(String nonce) {
        JSONObject session = picked;
        if (session == null || nonce.isEmpty() || TalkService.status.running || !resumed) return;
        String name = rowName(session);
        String run = GatewayLink.newRun();
        String signed = GatewayLink.startMessage(nonce, run, session.optString("id"), session.optString("nonce"));
        fingerprint("Connect to " + name + "?", CONNECT_WHY, signed, signature -> {
            try {
                JSONObject approve = new JSONObject()
                        .put("session", session.optString("id"))
                        .put("modNonce", session.optString("nonce"))
                        .put("nonce", nonce)
                        .put("sig", signature);
                stopBrowse();
                message = null;
                notice = null;
                TalkService.start(this, run, approve, name);
            } catch (JSONException e) {
                message = "That didn't work. Tap the session again.";
            }
        });
    }

    private void confirm() {
        confirm(false);
    }

    /** A tapped switch says no when its fingerprint is cancelled, since nothing else on screen asked for it. */
    private void confirm(boolean tapped) {
        JSONObject confirm = TalkService.status.confirm;
        if (confirm == null) return;
        String nonce = confirm.optString("nonce");
        String text = confirm.optString("text");
        JSONObject target = confirm.optJSONObject("switch");
        String run = TalkService.status.run;
        String signed;
        if (target != null) {
            // A switch, spoken or tapped: the fingerprint signs the other session for this run, exactly as Start does.
            if (run == null) return;
            signed = GatewayLink.startMessage(nonce, run, target.optString("id"), target.optString("modNonce"));
        } else {
            try {
                signed = GatewayLink.confirmMessage(nonce, text);
            } catch (GeneralSecurityException e) {
                return;
            }
        }
        String title = target != null ? text : "Confirm";
        String description = target != null ? CONNECT_WHY : text;
        fingerprint(title, description, signed, signature -> {
            if (target != null) waitForSwitch(target);
            startService(new Intent(this, TalkService.class)
                    .setAction(TalkService.ACTION_CONFIRMED)
                    .putExtra(TalkService.EXTRA_NONCE, nonce)
                    .putExtra(TalkService.EXTRA_SIG, signature));
        }, tapped ? () -> {
            decline(nonce);
            notSwitched();
        } : null);
    }

    private void decline() {
        JSONObject confirm = TalkService.status.confirm;
        if (confirm != null) decline(confirm.optString("nonce"));
    }

    private void decline(String nonce) {
        declined = nonce; // the card goes now, not when the service has passed the no on
        startService(new Intent(this, TalkService.class)
                .setAction(TalkService.ACTION_DECLINED)
                .putExtra(TalkService.EXTRA_NONCE, nonce));
    }

    /** A session tapped under Switch to: the call moves there, after the fingerprint. */
    private void switchTo(JSONObject session) {
        TalkService.Status status = TalkService.status;
        if (!status.running || status.run == null || switching != null || asking) return;
        if (session.optString("id").equals(chosenId(status))) return;
        message = null;
        notice = null;
        switching = session;
        switchAsked = false;
        switchingSince = SystemClock.elapsedRealtime();
        switchingAfter = status.confirm == null ? null : status.confirm.optString("nonce");
        startService(new Intent(this, TalkService.class)
                .setAction(TalkService.ACTION_SWITCH)
                .putExtra(TalkService.EXTRA_SESSION, session.optString("id")));
        render();
    }

    /** The fingerprint signed a switch: the screen says Switching until the Mac reports the call on that session. */
    private void waitForSwitch(JSONObject target) {
        JSONObject row = rowById(TalkService.status.sessions, target.optString("id"));
        switching = row != null ? row : target;
        switchAsked = true;
        switchingSince = SystemClock.elapsedRealtime();
    }

    /** The switch was turned down here, so the note is plain: nothing went wrong. */
    private void notSwitched() {
        switching = null;
        String session = currentName(TalkService.status);
        notice = session == null ? "Not switched." : "Not switched. You're still talking to " + session + ".";
        noticeSince = SystemClock.elapsedRealtime();
    }

    /**
     * Moves a switch along. Once the Mac asks for the fingerprint over the tapped session, the prompt comes up
     * with no more taps. The switch is done only when the Mac's list names the new session as chosen.
     */
    private void checkSwitch(TalkService.Status status) {
        if (switching == null) return;
        String id = switching.optString("id");
        JSONObject confirm = status.confirm;
        JSONObject target = confirm == null ? null : confirm.optJSONObject("switch");
        if (!status.running) {
            switching = null;
        } else if (id.equals(chosenId(status))) {
            switching = null;
            notice = "Now talking to " + currentName(status) + ".";
            noticeSince = SystemClock.elapsedRealtime();
        } else if (!switchAsked && target != null && target.optString("id").equals(id)
                && !confirm.optString("nonce").equals(switchingAfter)) {
            if (!resumed || asking) return;
            switchAsked = true;
            confirm(true);
        } else if (!asking && SystemClock.elapsedRealtime() - switchingSince > SWITCH_WAIT_MS) {
            switching = null;
            message = "The call didn't move. You're still talking to " + currentName(status) + ".";
        }
    }

    private static String chosenId(TalkService.Status status) {
        JSONObject sessions = status.sessions;
        return sessions == null || sessions.isNull("chosen") ? null : sessions.optString("chosen");
    }

    private static JSONObject rowById(JSONObject sessions, String id) {
        JSONArray list = sessions == null ? null : sessions.optJSONArray("list");
        if (list == null || id == null) return null;
        for (int i = 0; i < list.length(); i++) {
            JSONObject row = list.optJSONObject(i);
            if (row != null && id.equals(row.optString("id"))) return row;
        }
        return null;
    }

    /** The session the call is on, as the Mac last said, or as tapped until the Mac's list comes. */
    private JSONObject currentRow(TalkService.Status status) {
        JSONObject row = rowById(status.sessions, chosenId(status));
        return row != null ? row : picked;
    }

    private String currentName(TalkService.Status status) {
        JSONObject row = rowById(status.sessions, chosenId(status));
        return row != null ? rowName(row) : status.session;
    }

    private interface Signed {
        void with(String signature);
    }

    /** A strong fingerprint unlocks the hardware key for one signature over exactly these words. No PIN instead. */
    private void fingerprint(String title, String description, String words, Signed then) {
        fingerprint(title, description, words, then, null);
    }

    private void fingerprint(String title, String description, String words, Signed then, Runnable cancelled) {
        Signature signer;
        try {
            signer = PhoneKey.signer(Pairing.keySlot(this));
        } catch (GeneralSecurityException | IOException e) {
            Log.e(TAG, "the key won't sign", e);
            if (cancelled != null) cancelled.run();
            message = e instanceof KeyPermanentlyInvalidatedException || e instanceof KeyExpiredException
                    ? "This phone's key has gone, because a fingerprint changed or the pairing ran out. Tap Pair again. "
                            + describe(e)
                    : "The key won't sign: " + describe(e);
            render();
            return;
        }
        if (getSystemService(BiometricManager.class).canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG)
                != BiometricManager.BIOMETRIC_SUCCESS) {
            if (cancelled != null) cancelled.run();
            message = "Add a fingerprint in the phone's settings first.";
            render();
            return;
        }
        BiometricPrompt prompt = new BiometricPrompt.Builder(this)
                .setTitle(title)
                .setDescription(description)
                .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG)
                // Android doesn't report this button as an error, so it ends the prompt the same way a dismissal does.
                .setNegativeButton("Cancel", getMainExecutor(), (dialog, which) -> {
                    asking = false;
                    message = null;
                    if (cancelled != null) cancelled.run();
                    if (!resumed) stopBrowse();
                    render();
                })
                .build();
        asking = true;
        prompt.authenticate(new BiometricPrompt.CryptoObject(signer), new CancellationSignal(), getMainExecutor(),
                new BiometricPrompt.AuthenticationCallback() {
                    @Override public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
                        asking = false;
                        try {
                            then.with(PhoneKey.sign(result.getCryptoObject().getSignature(), words));
                        } catch (GeneralSecurityException e) {
                            Log.e(TAG, "the fingerprint didn't unlock the key", e);
                            message = "The fingerprint didn't unlock the key. Try again. " + describe(e);
                        }
                        render();
                    }

                    @Override public void onAuthenticationError(int code, CharSequence why) {
                        asking = false;
                        message = null;
                        if (cancelled != null) cancelled.run();
                        if (code != BiometricPrompt.BIOMETRIC_ERROR_USER_CANCELED
                                && code != BiometricPrompt.BIOMETRIC_ERROR_CANCELED) {
                            message = why + ".";
                        }
                        if (!resumed) stopBrowse();
                        render();
                    }
                });
    }

    /** The exception's class and message, then each cause's, so the real error can be read on the phone. */
    private static String describe(Throwable e) {
        StringBuilder text = new StringBuilder();
        for (int depth = 0; e != null && depth < 4; depth++, e = e.getCause()) {
            if (depth > 0) text.append(", caused by ");
            text.append(e.getClass().getSimpleName());
            if (e.getMessage() != null) text.append(": ").append(e.getMessage());
        }
        return text.toString();
    }

    /** Which mic a call listens on. A call on now moves to the new one. */
    private void chooseMicDialog() {
        new AlertDialog.Builder(this)
                .setTitle("Mic")
                .setSingleChoiceItems(MIC_NAMES, TalkService.micChoice(this), (dialog, which) -> {
                    TalkService.chooseMic(this, which);
                    dialog.dismiss();
                    render();
                })
                .setNegativeButton("Close", null)
                .show();
    }

    /** A tap on the dot stops a reply, as an earbud tap does, for when there are no earbuds to tap. */
    private void stopReply() {
        if (TalkService.status.running && TalkService.status.replying) {
            startService(new Intent(this, TalkService.class).setAction(TalkService.ACTION_TAP));
        }
    }

    private void chooseVoice(String voice) {
        startService(new Intent(this, TalkService.class)
                .setAction(TalkService.ACTION_VOICE).putExtra(TalkService.EXTRA_VOICE, voice));
    }

    private void chooseVoiceDialog() {
        JSONObject voices = TalkService.status.voices;
        JSONArray list = voices == null ? null : voices.optJSONArray("list");
        if (list == null || list.length() == 0) return;
        String chosen = voices.optString("chosen");
        String[] names = new String[list.length()];
        String[] ids = new String[list.length()];
        int checked = -1;
        for (int i = 0; i < list.length(); i++) {
            JSONObject voice = list.optJSONObject(i);
            names[i] = voice == null ? "" : voice.optString("name");
            ids[i] = voice == null ? "" : voice.optString("id");
            if (ids[i].equals(chosen)) checked = i;
        }
        new AlertDialog.Builder(this)
                .setTitle("Voice")
                .setSingleChoiceItems(names, checked, (dialog, which) -> {
                    chooseVoice(ids[which]);
                    dialog.dismiss();
                })
                .setNegativeButton("Close", null)
                .show();
    }

    // --- drawing -----------------------------------------------------------------

    private void render() {
        String host = Pairing.host(this);
        TalkService.Status status = TalkService.status;
        boolean paired = host != null;
        boolean form = pairScreen.formShown(paired);
        watchBack(pairScreen.cancelShown(paired));
        if (form) {
            renderPair(paired);
            showScreen(pairView);
            if (!paired) stopBrowse();
            return;
        }
        if (status.pairingOver) pairingOver = true;
        if (resumed && !status.running && browse == null && !pairingOver) {
            Pairing pairing = Pairing.load(this);
            if (pairing != null) startBrowse(pairing);
        }
        if (startWanted && browse != null && browseConnected && picked != null && !asking) requestStart();
        if (!Objects.equals(message, seenMessage)) {
            seenMessage = message;
            messageSince = SystemClock.elapsedRealtime();
        }
        if (status.running && message != null && SystemClock.elapsedRealtime() - messageSince > MESSAGE_MS) {
            message = null;
        }
        if (notice != null && SystemClock.elapsedRealtime() - noticeSince > NOTICE_MS) notice = null;
        checkSwitch(status);
        if (status.running) {
            renderTalk(status);
            showScreen(talkView);
        } else {
            switching = null;
            notice = null;
            renderHome(status, host);
            showScreen(homeView);
        }
    }

    /** Shows one screen, with a short fade when it changes. */
    private void showScreen(View screen) {
        if (screen == shownScreen) return;
        if (shownScreen != null) shownScreen.setVisibility(View.GONE);
        shownScreen = screen;
        screen.setAlpha(0f);
        screen.setVisibility(View.VISIBLE);
        screen.animate().alpha(1f).setDuration(FADE_MS).start();
    }

    private void renderPair(boolean paired) {
        boolean idle = pairScreen.idle();
        show(pairHeadline, paired ? "Pair again" : "Pair with your Mac");
        show(pairButton, idle ? "Pair" : "Pairing\u2026");
        pairButton.setEnabled(idle);
        pairButton.setAlpha(idle ? 1f : 0.6f);
        cancelPairButton.setEnabled(idle);
        Ui.shown(cancelPairButton, pairScreen.cancelShown(paired));
        Ui.shown(pairMessage, pairMessage.getText().length() > 0);
    }

    private void renderHome(TalkService.Status status, String host) {
        show(greeting, greeting());
        show(pairedLabel, "Paired with " + host);
        boolean starting = (startWanted || asking) && picked != null;
        show(homeLine, starting ? "Asking your Mac to start " + rowName(picked) + "."
                : "Choose a session to start talking.");

        String note = message != null ? message : status.why != null ? status.why + "." : null;
        note(homeNote, note, message != null);
        note(homeDays, pairingOver ? null : daysLeft(browseDays), false);

        Ui.shown(overCard, pairingOver);
        boolean waiting = !pairingOver && !browseConnected;
        Ui.shown(waitCard, waiting);
        if (waiting) show(waitText, browseWhy != null ? browseWhy + "."
                : "Make sure the gateway runs on your Mac, and Tailscale is on here and there.");

        JSONObject sessions = browseConnected && !pairingOver ? browseSessions : null;
        JSONArray list = sessions == null ? null : sessions.optJSONArray("list");
        boolean empty = sessions != null && (list == null || list.length() == 0);
        if (empty) picked = null;
        Ui.shown(emptyCard, empty);
        boolean listed = list != null && list.length() > 0;
        Ui.shown(homeMicRow, listed);
        Ui.shown(homeTip, listed);
        int mic = TalkService.micChoice(this);
        show(homeMicValue, MIC_NAMES[mic]);
        show(homeTip, MIC_TIPS[mic]);
        Ui.shown(homeLine, starting || list != null && list.length() > 0);

        String key = (list == null ? "" : list.toString()) + ":" + (starting ? picked.optString("id") : "");
        if (key.equals(shownHome)) return;
        shownHome = key;
        sessionsList.removeAllViews();
        if (list == null) return;
        String pickedId = picked == null ? null : picked.optString("id");
        Set<String> twins = twins(list);
        boolean headed = false;
        Map<String, List<JSONObject>> groups = byLogin(list);
        for (String login : groups.keySet()) headed |= !login.isEmpty();
        headed |= groups.size() > 1;
        for (Map.Entry<String, List<JSONObject>> group : groups.entrySet()) {
            if (headed) sessionsList.addView(ui.label(accountName(group.getKey())), ui.below(Ui.SPACE_XL));
            for (JSONObject session : group.getValue()) {
                boolean isStarting = starting && session.optString("id").equals(pickedId);
                if (session.optString("id").equals(pickedId)) picked = session; // its challenge may be new
                String meta = isStarting ? "Connecting\u2026" : rowMeta(session, twins);
                sessionsList.addView(sessionCard(session, meta, isStarting, () -> startWith(session)),
                        ui.below(Ui.SPACE_S + 2));
            }
        }
    }

    private void renderTalk(TalkService.Status status) {
        JSONObject row = currentRow(status);
        String name = row != null ? row.optString("name") : status.session;
        show(talkName, name == null ? "" : name);
        String account = row == null ? "" : join(" \u00b7 ", row.optString("login").isEmpty() ? ""
                : accountName(row.optString("login")), row.optString("where"));
        show(talkAccount, account);
        Ui.shown(talkAccount, !account.isEmpty());
        show(talkLabel, status.connected ? "Talking to" : "Connecting to");
        if (!Boolean.valueOf(status.connected).equals(shownLive)) {
            shownLive = status.connected; // green while the call is live, grey while it reconnects
            int colour = getColor(status.connected ? R.color.live : R.color.text_muted);
            liveDot.setColor(colour);
            talkLabel.setTextColor(colour);
        }

        boolean confirming = status.confirm != null && switching == null
                && !status.confirm.optString("nonce").equals(declined);
        Ui.shown(confirmCard, confirming);
        Ui.shown(orb, !confirming);
        Ui.shown(stateTitle, !confirming);
        Ui.shown(stateHint, !confirming);
        if (confirming) show(confirmText, status.confirm.optString("text"));

        int state;
        String title;
        String hint;
        if (!status.connected) {
            state = Orb.WAITING;
            title = "Connecting";
            hint = status.why != null ? status.why + "." : "Reaching your Mac.";
        } else if (switching != null) {
            state = Orb.WAITING;
            title = "Switching";
            hint = "To " + rowName(switching) + ".";
        } else if (status.hearing) {
            state = Orb.HEARING;
            title = "Hearing you";
            hint = "Pause when you're done.";
        } else if (status.replying) {
            state = Orb.REPLYING;
            title = "Replying";
            hint = status.onSpeaker ? "Tap the dot to stop it." : "Talk over it to cut in.";
        } else {
            state = Orb.LISTENING;
            title = "Listening";
            hint = "Lock the phone, and talk.";
        }
        orb.setState(state);
        show(stateTitle, title);
        show(stateHint, hint);

        if (message != null) note(talkNote, message, true);
        else if (status.problem != null) note(talkNote, status.problem, true); // whole sentences already
        else note(talkNote, notice, false);
        note(talkDays, daysLeft(status.pairingDays), false);

        renderSwitchList(status);
        show(talkMicValue, MIC_NAMES[TalkService.micChoice(this)]);

        JSONObject voices = status.voices;
        JSONArray voiceList = voices == null ? null : voices.optJSONArray("list");
        Ui.shown(voiceRow, voiceList != null && voiceList.length() > 0);
        if (voiceList != null) {
            String chosen = voices.optString("chosen");
            String voiceName = "";
            for (int i = 0; i < voiceList.length(); i++) {
                JSONObject voice = voiceList.optJSONObject(i);
                if (voice != null && voice.optString("id").equals(chosen)) voiceName = voice.optString("name");
            }
            show(voiceValue, voiceName);
        }
        show(detailsToggle, detailsOpen ? "Hide" : "Show");
        Ui.shown(details, detailsOpen);
        if (detailsOpen) show(details, detailsText(status));
    }

    /** The other sessions, one tap each. The session a switch is going to says so, and nothing moves before the Mac does. */
    private void renderSwitchList(TalkService.Status status) {
        JSONObject sessions = status.sessions;
        JSONArray list = sessions == null ? null : sessions.optJSONArray("list");
        String current = chosenId(status);
        String goingTo = switching == null ? "" : switching.optString("id");
        String key = (list == null ? "" : list.toString()) + ":" + current + ":" + goingTo;
        if (key.equals(shownSwitch)) return;
        shownSwitch = key;
        switchList.removeAllViews();
        int others = 0;
        if (list != null) {
            Set<String> twins = twins(list);
            for (List<JSONObject> group : byLogin(list).values()) {
                for (JSONObject session : group) {
                    String id = session.optString("id");
                    if (id.equals(current)) continue;
                    others++;
                    boolean going = id.equals(goingTo);
                    String login = session.optString("login");
                    String meta = going ? "Switching\u2026" : join(" \u00b7 ", login.isEmpty() ? "" : accountName(login),
                            ago(session), twins.contains(twinKey(session)) ? "session " + session.optString("session") : "");
                    switchList.addView(sessionCard(session, meta, going, () -> switchTo(session)), ui.below(Ui.SPACE_S + 2));
                }
            }
        }
        Ui.shown(switchLabel, others > 0);
    }

    /** A session as a card: its name, and under it, quieter, where it is and when it last took a prompt. */
    private View sessionCard(JSONObject session, String meta, boolean busy, Runnable action) {
        LinearLayout card = ui.tappableCard(action);
        TextView name = ui.text(session.optString("name"), Ui.SANS_MEDIUM, 17, R.color.text);
        card.addView(name);
        if (!meta.isEmpty()) {
            TextView quiet = ui.muted(meta);
            quiet.setTextSize(14);
            if (busy) quiet.setTextColor(getColor(R.color.accent_text));
            card.addView(quiet, ui.below(2));
        }
        return card;
    }

    /** Changes the text only when it changed, so an idle screen does no drawing. */
    private static void show(TextView view, String value) {
        if (!value.contentEquals(view.getText())) view.setText(value);
    }

    private void note(TextView view, String value, boolean danger) {
        Ui.shown(view, value != null && !value.isEmpty());
        if (value == null) return;
        show(view, value);
        ui.noteStyle(view, danger);
    }

    private static String greeting() {
        int hour = Calendar.getInstance().get(Calendar.HOUR_OF_DAY);
        if (hour < 5 || hour >= 18) return "Good evening";
        return hour < 12 ? "Good morning" : "Good afternoon";
    }

    private static String daysLeft(int days) {
        if (days < 0 || days > WARN_DAYS) return null;
        return days <= 1 ? "The pairing ends today. Pair again at the Mac."
                : String.format(Locale.ROOT, "The pairing ends in %d days. Pair again at the Mac.", days);
    }

    /**
     * Groups sessions by login, in a fixed order so a group doesn't move when one of its sessions takes a
     * prompt. Within a group, the Mac's order: the latest prompt first. A login with no name comes last.
     */
    private static Map<String, List<JSONObject>> byLogin(JSONArray list) {
        TreeMap<String, List<JSONObject>> groups = new TreeMap<>((a, b) ->
                a.isEmpty() != b.isEmpty() ? (a.isEmpty() ? 1 : -1) : a.compareToIgnoreCase(b));
        for (int i = 0; i < list.length(); i++) {
            JSONObject session = list.optJSONObject(i);
            if (session != null) groups.computeIfAbsent(session.optString("login"), k -> new ArrayList<>()).add(session);
        }
        return groups;
    }

    private static String accountName(String login) {
        return login.isEmpty() ? "Other account"
                : login.substring(0, 1).toUpperCase(Locale.ROOT) + login.substring(1) + " account";
    }

    /** The session's name as the Mac says and shows it everywhere, account included: "firstmate on the work account". */
    static String rowName(JSONObject session) {
        String label = session.optString("label");
        return label.isEmpty() ? session.optString("name") : label;
    }

    /** Sessions that share a name and a folder, which only their short ids tell apart. */
    private static Set<String> twins(JSONArray list) {
        Set<String> seen = new HashSet<>();
        Set<String> twins = new HashSet<>();
        for (int i = 0; i < list.length(); i++) {
            JSONObject session = list.optJSONObject(i);
            if (session == null) continue;
            String key = twinKey(session);
            if (!seen.add(key)) twins.add(key);
        }
        return twins;
    }

    /** Where the session is, when it last took a prompt, and its short id when another looks the same. */
    private static String rowMeta(JSONObject session, Set<String> twins) {
        String id = session.optString("session");
        return join(" \u00b7 ", session.optString("where"), ago(session),
                twins.contains(twinKey(session)) && !id.isEmpty() ? "session " + id : "");
    }

    private static String twinKey(JSONObject session) {
        return session.optString("login") + "\n" + session.optString("name") + "\n" + session.optString("where");
    }

    private static String join(String between, String... parts) {
        StringBuilder text = new StringBuilder();
        for (String part : parts) {
            if (part.isEmpty()) continue;
            if (text.length() > 0) text.append(between);
            text.append(part);
        }
        return text.toString();
    }

    private static String ago(JSONObject session) {
        if (session.isNull("ago")) return "";
        long seconds = session.optLong("ago");
        if (seconds < 60) return "last prompt just now";
        if (seconds < 3600) return String.format(Locale.ROOT, "last prompt %d min ago", seconds / 60);
        if (seconds < 86400) return String.format(Locale.ROOT, "last prompt %d h ago", seconds / 3600);
        long days = seconds / 86400;
        return days == 1 ? "last prompt 1 day ago" : String.format(Locale.ROOT, "last prompt %d days ago", days);
    }

    private String detailsText(TalkService.Status status) {
        return String.format(Locale.ROOT, "Mic: %s\nMic channels: %s\nMic level: %.0f dB\nBackground: %.0f dB\nReply leak: %s\nSound: %s\nMode: %s\nMic silenced: %s\nRound trip: %d ms\nEarbud taps: %d\nTalked over: %d, resumed: %d\nUnderruns: %d",
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
            case AudioDeviceInfo.TYPE_BLUETOOTH_SCO:
                return "Bluetooth call";
            case AudioDeviceInfo.TYPE_BUILTIN_MIC:
                return "phone mic";
            case AudioDeviceInfo.TYPE_BUILTIN_SPEAKER:
                return "phone speaker";
            case -1:
                return "none";
            default:
                return "other (" + type + ")";
        }
    }
}
