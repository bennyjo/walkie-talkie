package dev.walkietalkie;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * The Mac's address and the shared token, in this app's private storage, excluded from backups. The
 * token is encrypted under a key in the phone's secure hardware, so the file alone gives it away to
 * nobody. The token only lets the phone see the list of sessions: talking to one takes PhoneKey and a
 * fingerprint.
 */
final class Pairing {
    private static final String FILE = "pairing";
    private static final String HOST = "host";
    private static final String TOKEN = "sealed-token";
    private static final String KEY_SLOT = "key-slot";
    private static final String SEAL_ALIAS = "walkie-token";
    private static final int IV_BYTES = 12;

    final String host;
    final String token;

    private Pairing(String host, String token) {
        this.host = host;
        this.token = token;
    }

    /** The paired Mac's address, or null. Cheap: it reads no secret. */
    static String host(Context context) {
        SharedPreferences prefs = prefs(context);
        return prefs.contains(TOKEN) ? prefs.getString(HOST, null) : null;
    }

    /** The saved pairing, or null if there's none or it can't be opened any more. */
    static Pairing load(Context context) {
        SharedPreferences prefs = prefs(context);
        String host = prefs.getString(HOST, null);
        String sealed = prefs.getString(TOKEN, null);
        if (host == null || sealed == null) return null;
        try {
            ByteBuffer data = ByteBuffer.wrap(Base64.decode(sealed, Base64.NO_WRAP));
            byte[] iv = new byte[IV_BYTES];
            data.get(iv);
            byte[] body = new byte[data.remaining()];
            data.get(body);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, sealKey(false), new GCMParameterSpec(128, iv));
            return new Pairing(host, new String(cipher.doFinal(body), StandardCharsets.US_ASCII));
        } catch (GeneralSecurityException | IOException | IllegalArgumentException | java.nio.BufferUnderflowException e) {
            return null;
        }
    }

    /** Which of PhoneKey's two slots holds the key for the saved pairing. */
    static int keySlot(Context context) {
        return prefs(context).getInt(KEY_SLOT, 0);
    }

    /** Replaces the saved pairing, if any, in one write, and drops the old key only after it. */
    static PairSwap.Vault vault(Context context) {
        return new PairSwap.Vault() {
            private final int old = keySlot(context);
            private final int staged = PhoneKey.otherSlot(old);

            @Override public String stage() throws PhoneKey.Unavailable {
                return PhoneKey.create(staged);
            }

            @Override public void commit(String host, String token) throws GeneralSecurityException, IOException {
                Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
                cipher.init(Cipher.ENCRYPT_MODE, sealKey(true));
                byte[] body = cipher.doFinal(token.getBytes(StandardCharsets.US_ASCII));
                byte[] sealed = ByteBuffer.allocate(IV_BYTES + body.length).put(cipher.getIV()).put(body).array();
                boolean saved = prefs(context).edit().putString(HOST, host)
                        .putString(TOKEN, Base64.encodeToString(sealed, Base64.NO_WRAP)).putInt(KEY_SLOT, staged).commit();
                if (!saved) throw new IOException("the pairing couldn't be saved");
                PhoneKey.delete(old);
            }

            @Override public void discard() {
                PhoneKey.delete(staged);
            }
        };
    }

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    /** The token's key. No fingerprint for this one: the service reconnects with it while the phone is locked. */
    private static SecretKey sealKey(boolean create) throws GeneralSecurityException, IOException {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        SecretKey key = (SecretKey) store.getKey(SEAL_ALIAS, null);
        if (key != null || !create) {
            if (key == null) throw new GeneralSecurityException("no key");
            return key;
        }
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(SEAL_ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build());
        return generator.generateKey();
    }

    /** Only the private network: an IPv4 address in Tailscale's 100.64.0.0/10. The emulator's build also takes its own host. */
    static boolean allowedAddress(String host) {
        if (Variant.TEST && ("10.0.2.2".equals(host) || "127.0.0.1".equals(host))) return true;
        return tailnetAddress(host);
    }

    /** Whether host is an IPv4 address in Tailscale's 100.64.0.0/10. */
    static boolean tailnetAddress(String host) {
        String[] parts = host.split("\\.", -1);
        if (parts.length != 4) return false;
        int[] octets = new int[4];
        for (int i = 0; i < 4; i++) {
            if (parts[i].isEmpty() || parts[i].length() > 3) return false;
            for (char c : parts[i].toCharArray()) {
                if (c < '0' || c > '9') return false;
            }
            octets[i] = Integer.parseInt(parts[i]);
            if (octets[i] > 255) return false;
        }
        return octets[0] == 100 && octets[1] >= 64 && octets[1] <= 127;
    }
}
