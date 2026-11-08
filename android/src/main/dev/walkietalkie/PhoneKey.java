package dev.walkietalkie;

import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyInfo;
import android.security.keystore.KeyProperties;
import android.security.keystore.StrongBoxUnavailableException;
import android.util.Base64;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyFactory;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.spec.ECGenParameterSpec;
import java.util.Date;

/**
 * The phone's signing key. It's made in the phone's secure hardware and never leaves it. It signs
 * only after a strong fingerprint, once per fingerprint, and only while the phone is unlocked. Adding
 * a fingerprint to the phone destroys it, and it stops signing when the pairing runs out, so either
 * way the phone pairs again. The Mac keeps the public half.
 */
final class PhoneKey {
    static final int PAIRING_DAYS = 30; // the gateway's own limit, which this backs up

    private static final String ALIAS = "walkie-phone";
    private static final String STORE = "AndroidKeyStore";

    /** Thrown when the phone has no secure hardware for the key, or no fingerprint to guard it. */
    static final class Unavailable extends Exception {
        private static final long serialVersionUID = 1L;

        Unavailable(String why) {
            super(why);
        }
    }

    private PhoneKey() { }

    /** A new key in place of any earlier one. Returns the public half, base64 of its X.509 form. */
    static String create() throws Unavailable {
        delete();
        try {
            return generate(true);
        } catch (StrongBoxUnavailableException e) {
            try {
                return generate(false); // the phone's trusted environment, when it has no separate chip
            } catch (GeneralSecurityException | IOException again) {
                throw unavailable(again);
            }
        } catch (GeneralSecurityException | IOException e) {
            throw unavailable(e);
        }
    }

    private static String generate(boolean strongBox) throws GeneralSecurityException, IOException {
        KeyPairGenerator generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, STORE);
        generator.initialize(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                .setAlgorithmParameterSpec(new ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setUserAuthenticationRequired(true)
                .setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG) // a fingerprint for each use
                .setInvalidatedByBiometricEnrollment(true)
                .setUnlockedDeviceRequired(true)
                .setKeyValidityForOriginationEnd(new Date(System.currentTimeMillis() + PAIRING_DAYS * 86_400_000L))
                .setIsStrongBoxBacked(strongBox)
                .build());
        byte[] encoded = generator.generateKeyPair().getPublic().getEncoded();
        if (!Variant.TEST && !inHardware()) {
            delete();
            throw new GeneralSecurityException("not in secure hardware");
        }
        return Base64.encodeToString(encoded, Base64.NO_WRAP);
    }

    private static boolean inHardware() throws GeneralSecurityException, IOException {
        PrivateKey key = (PrivateKey) store().getKey(ALIAS, null);
        KeyInfo info = KeyFactory.getInstance(key.getAlgorithm(), STORE).getKeySpec(key, KeyInfo.class);
        int level = info.getSecurityLevel();
        return level == KeyProperties.SECURITY_LEVEL_STRONGBOX || level == KeyProperties.SECURITY_LEVEL_TRUSTED_ENVIRONMENT;
    }

    /**
     * A signature ready to sign once a fingerprint unlocks it. Throws when the key has gone: a new
     * fingerprint on the phone, an expired pairing, or no pairing at all. The phone then pairs again.
     */
    static Signature signer() throws GeneralSecurityException, IOException {
        PrivateKey key = (PrivateKey) store().getKey(ALIAS, null);
        if (key == null) throw new GeneralSecurityException("no key");
        Signature signature = Signature.getInstance("SHA256withECDSA");
        signature.initSign(key);
        return signature;
    }

    /** After the fingerprint: the signature over message, base64 of its DER form. */
    static String sign(Signature unlocked, String message) throws GeneralSecurityException {
        unlocked.update(message.getBytes(StandardCharsets.UTF_8));
        return Base64.encodeToString(unlocked.sign(), Base64.NO_WRAP);
    }

    static void delete() {
        try {
            store().deleteEntry(ALIAS);
        } catch (GeneralSecurityException | IOException ignored) {
            // Nothing to delete.
        }
    }

    private static KeyStore store() throws GeneralSecurityException, IOException {
        KeyStore store = KeyStore.getInstance(STORE);
        store.load(null);
        return store;
    }

    private static Unavailable unavailable(Exception e) {
        if (e instanceof java.security.InvalidAlgorithmParameterException) {
            return new Unavailable("Add a fingerprint in the phone's settings first, then pair.");
        }
        return new Unavailable("This phone can't keep a key in secure hardware, so it can't pair.");
    }
}
