package dev.walkietalkie;

import android.os.SystemClock;
import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.net.ConnectException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.ArrayDeque;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * The TCP connection to the Mac: login, framing, and reconnecting. The gateway's phone.py describes
 * the protocol. The tailnet address doesn't change as the phone moves between networks, so a
 * connection often survives a move. When one doesn't, this reconnects, and each side resends what
 * the other missed.
 *
 * Without an approval it only shows the list of sessions. With one, the first login carries the
 * fingerprint's signature, and later logins resume that run for as long as the gateway keeps it.
 */
final class GatewayLink {
    static final int PORT = 8791;

    interface Listener {
        void onSpeech(int generation, byte[] packet, int offset, int length);
        void onMessage(JSONObject message);
        void onConnection(boolean connected, String why);
        /** The gateway has no memory of this run: it restarted, so its numbering did too. */
        void onGatewayReset();
        /** The gateway's login answer. False ends the link for good: the run is over, or never began. */
        boolean onLogin(JSONObject answer);
    }

    private static final int CONTROL = 1, MIC = 2, SPEECH = 3;
    private static final int MAX_FRAME = 16384;
    private static final int CONNECT_TIMEOUT_MS = 5000;
    private static final int LOGIN_TIMEOUT_MS = 5000;
    private static final int READ_TIMEOUT_MS = 15000; // pings answer every 5 s, so this means it's gone
    private static final long PING_EVERY_MS = 5000;
    private static final long BATCH_MS = 40;          // frames share a packet, which saves a lot of overhead
    private static final int MIC_RESEND_FRAMES = 500; // 10 s
    private static final int MAX_QUEUED = 1000;
    private static final long MAX_BACKOFF_MS = 5000;
    private static final long FLUSH_ON_STOP_MS = 500;

    private static final class MicFrame {
        final long seq;
        final byte[] frame;
        MicFrame(long seq, byte[] frame) {
            this.seq = seq;
            this.frame = frame;
        }
    }

    private final Pairing pairing;
    private final Listener listener;
    private final String run;
    private final Object lock = new Object();
    private final ArrayDeque<MicFrame> micFrames = new ArrayDeque<>();
    private final LinkedBlockingQueue<byte[]> outgoing = new LinkedBlockingQueue<>();
    private JSONObject approve;     // the fingerprint's signature, until a login has carried it
    private JSONObject unconfirmedTap;
    private JSONObject unconfirmedVoice;
    private long micSeq;
    private volatile long lastSpeechSeq;
    private volatile boolean running;
    private volatile boolean connected;
    private volatile Socket socket;
    private volatile long roundTripMs;
    private Thread reader;

    /** A link that only lists sessions. */
    GatewayLink(Pairing pairing, Listener listener) {
        this(pairing, listener, newRun(), null);
    }

    /** A run a fingerprint approved: approve holds the session, the challenges, and the signature over them and run. */
    GatewayLink(Pairing pairing, Listener listener, String run, JSONObject approve) {
        this.pairing = pairing;
        this.listener = listener;
        this.run = run;
        this.approve = approve;
    }

    static String newRun() {
        return hex(randomBytes(16));
    }

    /** What the fingerprint signs to start a run: the gateway parses the same words in security.start_message. */
    static String startMessage(String nonce, String run, String session, String modNonce) {
        return "walkie-start|" + nonce + "|" + run + "|" + session + "|" + modNonce;
    }

    /** What the fingerprint signs to confirm a read-back: as security.confirm_message. */
    static String confirmMessage(String nonce, String spoken) throws GeneralSecurityException {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(spoken.getBytes(StandardCharsets.UTF_8));
        return "walkie-confirm|" + nonce + "|" + hex(digest);
    }

    void start() {
        running = true;
        reader = new Thread(this::connectLoop, "walkie-link");
        reader.start();
    }

    /** What's already queued goes out first, so a last message sent just before this reaches the gateway. */
    void stop() {
        boolean flushing;
        synchronized (lock) {
            flushing = connected;
            running = false;
        }
        if (reader == null) return;
        try {
            if (flushing) reader.join(FLUSH_ON_STOP_MS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        closeSocket();
        reader.interrupt();
    }

    boolean connected() {
        return connected;
    }

    long roundTripMs() {
        return roundTripMs;
    }

    /** From the capture thread. Kept for a while, so a dropped connection loses no words. */
    void sendMic(byte[] packet, int length) {
        ByteBuffer frame = ByteBuffer.allocate(5 + 4 + length);
        frame.putInt(4 + length).put((byte) MIC);
        synchronized (lock) {
            micSeq++;
            frame.putInt((int) micSeq).put(packet, 0, length);
            micFrames.addLast(new MicFrame(micSeq, frame.array()));
            while (micFrames.size() > MIC_RESEND_FRAMES) micFrames.removeFirst();
            if (connected && outgoing.size() < MAX_QUEUED) outgoing.offer(frame.array());
        }
    }

    /** From the capture thread. A frame that won't be sent still counts, so the gateway sees the gap. */
    void skipMic() {
        synchronized (lock) {
            micSeq++;
        }
    }

    /** A tap or a voice choice is kept until the gateway confirms it, and sent again after a reconnect. */
    void sendControl(JSONObject message) {
        synchronized (lock) {
            if (!running) return;
            String kind = message.optString("t");
            if (kind.equals("tap")) unconfirmedTap = message;
            if (kind.equals("voice")) unconfirmedVoice = message;
            if (connected) outgoing.offer(controlFrame(message));
        }
    }

    private void confirm(JSONObject message) {
        synchronized (lock) {
            String kind = message.optString("t");
            if (kind.equals("cut") && unconfirmedTap != null
                    && message.optInt("gen") >= unconfirmedTap.optInt("gen")) {
                unconfirmedTap = null;
            }
            if (kind.equals("voices") && unconfirmedVoice != null
                    && unconfirmedVoice.optString("id").equals(message.optString("chosen"))) {
                unconfirmedVoice = null;
            }
        }
    }

    private void connectLoop() {
        long backoff = 500;
        while (running) {
            String why = null;
            Thread sender = null;
            Socket s = new Socket();
            socket = s;
            try {
                s.setTcpNoDelay(true);
                s.connect(new InetSocketAddress(pairing.host, PORT), CONNECT_TIMEOUT_MS);
                DataInputStream in = new DataInputStream(new BufferedInputStream(s.getInputStream()));
                DataOutputStream out = new DataOutputStream(new BufferedOutputStream(s.getOutputStream()));
                s.setSoTimeout(LOGIN_TIMEOUT_MS);
                long up;
                try {
                    up = login(in, out);
                } catch (EOFException e) {
                    throw new EOFException("refused"); // the gateway closed the door during the login
                }
                if (up == Long.MIN_VALUE) {
                    running = false; // the run is over, or the pairing is: retrying can't help
                    break;
                }
                s.setSoTimeout(READ_TIMEOUT_MS);
                if (up < 0) {
                    lastSpeechSeq = 0;
                    listener.onGatewayReset();
                }
                goOnline(up);
                listener.onConnection(true, null);
                sender = new Thread(() -> sendLoop(out), "walkie-send");
                sender.start();
                backoff = 500;
                readLoop(in);
            } catch (IOException | JSONException | GeneralSecurityException e) {
                why = why(s, e);
            } finally {
                synchronized (lock) {
                    connected = false;
                    outgoing.clear();
                }
                closeSocket();
                if (sender != null) sender.interrupt();
                if (running) listener.onConnection(false, why);
            }
            if (!running) break;
            SystemClock.sleep(backoff);
            backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
        }
    }

    /** Says what's wrong in words that name the fix. */
    private static String why(Socket s, Exception e) {
        if (e instanceof EOFException && "refused".equals(e.getMessage())) {
            return "Your Mac refused this phone. Wait a minute, then tap Pair again if it still fails";
        }
        if (e instanceof SocketTimeoutException) {
            return s.isConnected() ? "Your Mac answered, then went quiet"
                    : "Can't reach your Mac. Turn off any VPN that blocks Tailscale, check Tailscale on both devices, and check the Mac is awake";
        }
        if (e instanceof ConnectException && String.valueOf(e.getMessage()).contains("ECONNREFUSED")) {
            return "Your Mac is on, but the gateway isn't running. Start it on the Mac";
        }
        if (e instanceof GeneralSecurityException) return "Your Mac's answer didn't prove it's your Mac";
        return "Can't reach your Mac. Check Tailscale on both devices, and check the Mac is awake";
    }

    private void closeSocket() {
        Socket s = socket;
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
                // It's closed either way.
            }
        }
    }

    /**
     * Each side proves it holds the token. No audio goes out until the gateway has proved it. Returns
     * the gateway's last mic sequence, or Long.MIN_VALUE when the listener ends the link.
     */
    private long login(DataInputStream in, DataOutputStream out)
            throws IOException, JSONException, GeneralSecurityException {
        JSONObject challenge = readControl(in);
        String serverNonce = challenge.getString("nonce");
        String clientNonce = hex(randomBytes(16));
        JSONObject auth = new JSONObject()
                .put("t", "auth")
                .put("nonce", clientNonce)
                .put("mac", mac("phone", serverNonce, clientNonce))
                .put("run", run)
                .put("down", lastSpeechSeq);
        JSONObject signed = approve;
        if (signed != null) auth.put("approve", signed);
        out.write(controlFrame(auth));
        out.flush();
        JSONObject answer = readControl(in);
        byte[] expected = mac("gateway", serverNonce, clientNonce).getBytes(StandardCharsets.US_ASCII);
        byte[] given = answer.getString("mac").getBytes(StandardCharsets.US_ASCII);
        if (!MessageDigest.isEqual(expected, given)) {
            throw new GeneralSecurityException("the gateway failed to prove it holds the token");
        }
        if (signed != null) approve = null; // carried once: a later login resumes the run, or finds it over
        if (!listener.onLogin(answer)) return Long.MIN_VALUE;
        return answer.getLong("up");
    }

    private void goOnline(long up) {
        synchronized (lock) {
            connected = true;
            if (unconfirmedTap != null) outgoing.offer(controlFrame(unconfirmedTap));
            if (unconfirmedVoice != null) outgoing.offer(controlFrame(unconfirmedVoice));
            if (up < 0) {
                micFrames.clear();
            } else {
                for (MicFrame f : micFrames) {
                    if (f.seq > up) outgoing.offer(f.frame);
                }
            }
        }
    }

    private void sendLoop(DataOutputStream out) {
        long lastPing = 0;
        try {
            while (true) {
                long now = SystemClock.elapsedRealtime();
                if (now - lastPing >= PING_EVERY_MS) {
                    lastPing = now;
                    outgoing.offer(controlFrame(new JSONObject().put("t", "ping").put("ms", now)));
                }
                byte[] frame = outgoing.poll(1, TimeUnit.SECONDS);
                if (frame == null) continue;
                out.write(frame);
                Thread.sleep(BATCH_MS);
                while ((frame = outgoing.poll()) != null) out.write(frame);
                out.flush();
                if (!running && outgoing.isEmpty()) {
                    closeSocket();
                    return;
                }
            }
        } catch (IOException | JSONException e) {
            closeSocket(); // the reader wakes up and reconnects
        } catch (InterruptedException e) {
            // This connection is over.
        }
    }

    /** Ends when the socket closes, so a stop can't cut off what's still being sent. */
    private void readLoop(DataInputStream in) throws IOException, JSONException {
        while (true) {
            int length = in.readInt();
            int type = in.readUnsignedByte();
            if (length < 0 || length > MAX_FRAME) throw new IOException("bad frame length");
            byte[] payload = new byte[length];
            in.readFully(payload);
            if (type == SPEECH && length >= 6) {
                ByteBuffer header = ByteBuffer.wrap(payload);
                lastSpeechSeq = header.getInt() & 0xffffffffL;
                int generation = header.getShort() & 0xffff;
                listener.onSpeech(generation, payload, 6, length - 6);
            } else if (type == CONTROL) {
                JSONObject message = new JSONObject(new String(payload, StandardCharsets.UTF_8));
                if (message.optString("t").equals("pong")) {
                    roundTripMs = SystemClock.elapsedRealtime() - message.optLong("ms");
                } else {
                    confirm(message);
                    listener.onMessage(message);
                }
            }
        }
    }

    /** Trades a pairing code and the phone's public key for the token. Blocks: call it off the main thread. */
    static String pair(String host, String code, String publicKey) throws IOException, JSONException {
        try (Socket s = new Socket()) {
            s.connect(new InetSocketAddress(host, PORT), CONNECT_TIMEOUT_MS);
            s.setSoTimeout(LOGIN_TIMEOUT_MS);
            DataInputStream in = new DataInputStream(new BufferedInputStream(s.getInputStream()));
            DataOutputStream out = new DataOutputStream(new BufferedOutputStream(s.getOutputStream()));
            readControl(in);
            out.write(controlFrame(new JSONObject().put("t", "pair").put("code", code).put("key", publicKey)));
            out.flush();
            return readControl(in).getString("token");
        }
    }

    private static JSONObject readControl(DataInputStream in) throws IOException, JSONException {
        int length = in.readInt();
        int type = in.readUnsignedByte();
        if (type != CONTROL || length < 0 || length > MAX_FRAME) throw new IOException("bad frame");
        byte[] payload = new byte[length];
        in.readFully(payload);
        return new JSONObject(new String(payload, StandardCharsets.UTF_8));
    }

    private static byte[] controlFrame(JSONObject message) {
        byte[] text = message.toString().getBytes(StandardCharsets.UTF_8);
        ByteBuffer frame = ByteBuffer.allocate(5 + text.length);
        frame.putInt(text.length).put((byte) CONTROL).put(text);
        return frame.array();
    }

    private String mac(String role, String serverNonce, String clientNonce) throws GeneralSecurityException {
        Mac hmac = Mac.getInstance("HmacSHA256");
        hmac.init(new SecretKeySpec(unhex(pairing.token), "HmacSHA256"));
        String message = role + "|" + serverNonce + "|" + clientNonce;
        return hex(hmac.doFinal(message.getBytes(StandardCharsets.US_ASCII)));
    }

    private static byte[] randomBytes(int count) {
        byte[] bytes = new byte[count];
        new SecureRandom().nextBytes(bytes);
        return bytes;
    }

    private static String hex(byte[] bytes) {
        StringBuilder b = new StringBuilder(bytes.length * 2);
        for (byte x : bytes) b.append(Character.forDigit((x >> 4) & 15, 16)).append(Character.forDigit(x & 15, 16));
        return b.toString();
    }

    private static byte[] unhex(String text) {
        byte[] bytes = new byte[text.length() / 2];
        for (int i = 0; i < bytes.length; i++) {
            bytes[i] = (byte) Integer.parseInt(text.substring(2 * i, 2 * i + 2), 16);
        }
        return bytes;
    }
}
