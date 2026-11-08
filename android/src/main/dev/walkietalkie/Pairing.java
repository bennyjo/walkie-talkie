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

    static void save(Context context, String host, String token) throws GeneralSecurityException, IOException {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, sealKey(true));
        byte[] body = cipher.doFinal(token.getBytes(StandardCharsets.US_ASCII));
        byte[] sealed = ByteBuffer.allocate(IV_BYTES + body.length).put(cipher.getIV()).put(body).array();
        prefs(context).edit().putString(HOST, host).putString(TOKEN, Base64.encodeToString(sealed, Base64.NO_WRAP)).apply();
    }

    /** Forgets the Mac, and destroys both keys, so nothing left on the phone can reach it. */
    static void forget(Context context) {
        prefs(context).edit().clear().apply();
        PhoneKey.delete();
        try {
            KeyStore store = KeyStore.getInstance("AndroidKeyStore");
            store.load(null);
            store.deleteEntry(SEAL_ALIAS);
        } catch (GeneralSecurityException | IOException ignored) {
            // Nothing to delete.
        }
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

    /** Only the private network: an IPv4 address in Tailscale's 100.64.0.0/10. */
    static boolean allowedAddress(String host) {
        if (Variant.TEST && "10.0.2.2".equals(host)) return true;
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
