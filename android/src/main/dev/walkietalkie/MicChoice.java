package dev.walkietalkie;

/**
 * Plain Java, so android/build.sh --test can run it. The mic chosen in the app, and the routes that choice
 * allows: which input the app may send from, where a reply may play, whether the call needs Android's call
 * mode, and whether talking over a reply counts.
 *
 * <ul>
 *   <li>USB: the mic's USB receiver, with replies in Bluetooth headphones. Media quality.
 *   <li>HEADSET: the Bluetooth headphones' own mic, with replies in them too. That takes Android's call mode,
 *       which drops the headphones to call quality, so only this choice uses it.
 *   <li>PHONE: the phone's own mic, with replies in Bluetooth headphones, or on the phone's speaker when none
 *       are connected. On the speaker, talking over a reply is off: the speaker is next to the mic, so the
 *       reply would be heard as a voice.
 * </ul>
 *
 * The app offers only the choices whose device is connected now: USB while a USB input is there, HEADSET while
 * Android offers a Bluetooth headset as a call's device, and PHONE always. A saved choice whose device isn't
 * connected is kept for when it comes back, and the phone's mic stands in until then.
 *
 * Any other route sends nothing and plays no reply: the phone's mic is sent only when it's the choice in use,
 * and its speaker plays a reply only then, with no headphones there.
 */
final class MicChoice {
    static final int USB = 0;
    static final int HEADSET = 1;
    static final int PHONE = 2;
    static final int DEFAULT = USB;

    // AudioDeviceInfo's types, which this class can't name without Android.
    static final int BUILTIN_SPEAKER = 2;
    static final int BLUETOOTH_SCO = 7;
    static final int USB_DEVICE = 11;
    static final int BUILTIN_MIC = 15;
    static final int USB_HEADSET = 22;
    static final int BLE_HEADSET = 26;

    static final int[] USB_MICS = {USB_DEVICE, USB_HEADSET};
    /** A headset as the call's device, LE Audio first, since it sounds better than the classic call link. */
    static final int[] HEADSET_ROUTES = {BLE_HEADSET, BLUETOOTH_SCO};
    static final int[] PHONE_MICS = {BUILTIN_MIC};
    static final int[] PHONE_SPEAKER = {BUILTIN_SPEAKER};

    /** What a call does with one choice and the devices connected now. */
    static final class Plan {
        final int choice;
        /** Android's call mode, with callDevice as the call's device. */
        final boolean call;
        final int callDevice;
        /** The inputs this choice may send from, the first present one preferred. */
        final int[] mics;
        /** The outputs a reply may play on, the first present one preferred. */
        final int[] outputs;
        final boolean talkOver;

        private Plan(int choice, boolean call, int callDevice, int[] mics, int[] outputs, boolean talkOver) {
            this.choice = choice;
            this.call = call;
            this.callDevice = callDevice;
            this.mics = mics;
            this.outputs = outputs;
            this.talkOver = talkOver;
        }

        /** The phone's speaker plays replies: no headphones, and the phone's mic. */
        boolean onSpeaker() {
            return outputs == PHONE_SPEAKER;
        }
    }

    private MicChoice() {}

    static int valid(int choice) {
        return choice == HEADSET || choice == PHONE ? choice : USB;
    }

    /**
     * The choices whose device is connected now, in the order the app lists them, given the types of the
     * inputs connected now and of the devices Android offers as a call's device. The phone's mic is always there.
     */
    static int[] offered(int[] inputs, int[] callDevices) {
        boolean usb = first(USB_MICS, inputs) >= 0;
        boolean headset = first(HEADSET_ROUTES, callDevices) >= 0;
        int[] choices = new int[1 + (usb ? 1 : 0) + (headset ? 1 : 0)];
        int i = 0;
        if (usb) choices[i++] = USB;
        if (headset) choices[i++] = HEADSET;
        choices[i] = PHONE;
        return choices;
    }

    /** The choice in use: the saved one while it's offered, otherwise the phone's mic, which always is. */
    static int inUse(int saved, int[] offered) {
        int choice = valid(saved);
        return contains(offered, choice) ? choice : PHONE;
    }

    /**
     * The plan for the saved choice, given the types of the inputs and outputs connected now, and of the devices
     * Android offers as a call's device. It's the plan for the choice in use.
     */
    static Plan plan(int saved, int[] inputs, int[] outputs, int[] callDevices) {
        switch (inUse(saved, offered(inputs, callDevices))) {
            case HEADSET: {
                int[] only = {first(HEADSET_ROUTES, callDevices)};
                return new Plan(HEADSET, true, only[0], only, only, true);
            }
            case PHONE:
                if (first(EarbudRoutes.TYPES, outputs) >= 0) {
                    return new Plan(PHONE, false, -1, PHONE_MICS, EarbudRoutes.TYPES, true);
                }
                return new Plan(PHONE, false, -1, PHONE_MICS, PHONE_SPEAKER, false);
            default:
                return new Plan(USB, false, -1, USB_MICS, EarbudRoutes.TYPES, true);
        }
    }

    /**
     * Whether a stream's route is one the plan allows. A route can lag behind a device's removal, so a device
     * of an allowed type must also still be there.
     */
    static boolean allowed(int[] allowedTypes, int routed, int[] present) {
        return contains(allowedTypes, routed) && first(allowedTypes, present) >= 0;
    }

    /** The first of the wanted types that's present, or -1. */
    static int first(int[] wanted, int[] present) {
        for (int type : wanted) {
            if (contains(present, type)) return type;
        }
        return -1;
    }

    static boolean contains(int[] types, int type) {
        for (int t : types) {
            if (t == type) return true;
        }
        return false;
    }
}
