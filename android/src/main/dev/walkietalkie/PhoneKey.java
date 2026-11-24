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

    private static final String[] ALIASES = {"walkie-phone", "walkie-phone-2"}; // a new key goes in the slot the old one isn't in
    private static final String STORE = "AndroidKeyStore";

    /** Thrown when the phone has no secure hardware for the key, or no fingerprint to guard it. */
    static final class Unavailable extends Exception {
        private static final long serialVersionUID = 1L;

        Unavailable(String why) {
            super(why);
        }
    }

    private PhoneKey() { }

    /** The other slot, for a new key to be made while the key in this one still works. */
    static int otherSlot(int slot) {
        return 1 - slot;
    }

    /** A new key in this slot, in place of any earlier one there. Returns the public half, base64 of its X.509 form. */
    static String create(int slot) throws Unavailable {
        delete(slot);
        try {
            return generate(slot, true);
        } catch (StrongBoxUnavailableException e) {
            try {
                return generate(slot, false); // the phone's trusted environment, when it has no separate chip
            } catch (GeneralSecurityException | IOException again) {
                throw unavailable(again);
            }
        } catch (GeneralSecurityException | IOException e) {
            throw unavailable(e);
        }
    }

    private static String generate(int slot, boolean strongBox) throws GeneralSecurityException, IOException {
        KeyPairGenerator generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, STORE);
        generator.initialize(new KeyGenParameterSpec.Builder(ALIASES[slot], KeyProperties.PURPOSE_SIGN)
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
        if (!Variant.TEST && !inHardware(slot)) {
            delete(slot);
            throw new GeneralSecurityException("not in secure hardware");
        }
        return Base64.encodeToString(encoded, Base64.NO_WRAP);
    }

    private static boolean inHardware(int slot) throws GeneralSecurityException, IOException {
        PrivateKey key = (PrivateKey) store().getKey(ALIASES[slot], null);
        KeyInfo info = KeyFactory.getInstance(key.getAlgorithm(), STORE).getKeySpec(key, KeyInfo.class);
        int level = info.getSecurityLevel();
        return level == KeyProperties.SECURITY_LEVEL_STRONGBOX || level == KeyProperties.SECURITY_LEVEL_TRUSTED_ENVIRONMENT;
    }

    /**
     * A signature ready to sign once a fingerprint unlocks it. Throws when it can't. When the key has
     * gone, from a new fingerprint on the phone or an expired pairing, the phone pairs again.
     */
    static Signature signer(int slot) throws GeneralSecurityException, IOException {
        PrivateKey key = (PrivateKey) store().getKey(ALIASES[slot], null);
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

    static void delete(int slot) {
        try {
            store().deleteEntry(ALIASES[slot]);
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
