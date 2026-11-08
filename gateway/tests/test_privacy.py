"""What the phone app may do, as Android reads it from the app's manifest."""
import os
import unittest
import xml.etree.ElementTree as ElementTree

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
ANDROID = "{http://schemas.android.com/apk/res/android}"


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifest = ElementTree.parse(os.path.join(ROOT, "android", "AndroidManifest.xml")).getroot()

    def test_the_app_asks_for_only_the_permissions_it_needs(self):
        permissions = {element.get(ANDROID + "name") for element in self.manifest.findall("uses-permission")}
        self.assertEqual(permissions, {"android.permission." + name for name in (
            "INTERNET", "RECORD_AUDIO", "FOREGROUND_SERVICE", "FOREGROUND_SERVICE_MICROPHONE",
            "FOREGROUND_SERVICE_MEDIA_PLAYBACK", "POST_NOTIFICATIONS", "WAKE_LOCK", "USE_BIOMETRIC")})

    def test_nothing_from_the_app_goes_to_a_cloud_backup(self):
        self.assertEqual(self.manifest.find("application").get(ANDROID + "allowBackup"), "false")


if __name__ == "__main__":
    unittest.main()
