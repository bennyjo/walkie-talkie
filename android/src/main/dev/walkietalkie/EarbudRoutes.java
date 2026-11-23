package dev.walkietalkie;

/** Plain Java, so android/build.sh --test can run it. The Bluetooth outputs that count as the earbuds. */
final class EarbudRoutes {
    // AudioDeviceInfo.TYPE_BLUETOOTH_A2DP and TYPE_BLE_HEADSET, which this class can't name without Android.
    static final int[] TYPES = {8, 26};

    private EarbudRoutes() {}

    static boolean isEarbuds(int type) {
        for (int earbuds : TYPES) {
            if (earbuds == type) return true;
        }
        return false;
    }

    /** True when any of the removed outputs was the earbuds, which ends the call at once. */
    static boolean anyEarbuds(int[] removedTypes) {
        for (int type : removedTypes) {
            if (isEarbuds(type)) return true;
        }
        return false;
    }
}
