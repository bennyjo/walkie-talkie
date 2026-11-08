import os
import stat
import tempfile
import unittest

from walkie_gateway import security

from .keys import KEY, PhoneKey


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class BindTest(unittest.TestCase):
    def test_only_the_tailnet_and_loopback_may_be_bound(self):
        self.assertEqual(security.check_bind("100.100.1.1"), "100.100.1.1")
        self.assertEqual(security.check_bind("127.0.0.1"), "127.0.0.1")
        for address in ("0.0.0.0", "::", "192.168.1.5", "8.8.8.8", "100.128.0.1", "100.63.255.255", "not an address", ""):
            with self.assertRaises(security.SecurityError, msg=address):
                security.check_bind(address)

    def test_tailnet_range(self):
        self.assertTrue(security.is_tailnet("100.64.0.0"))
        self.assertTrue(security.is_tailnet("100.127.255.255"))
        self.assertTrue(security.is_tailnet("::ffff:100.100.1.1"))
        self.assertFalse(security.is_tailnet("100.128.0.0"))
        self.assertFalse(security.is_tailnet("10.0.0.1"))
        self.assertFalse(security.is_tailnet("fe80::1"))


class PairingWindowTest(unittest.TestCase):
    def test_a_good_code_works_once(self):
        window = security.PairingWindow(Clock())
        code = window.code
        self.assertEqual(len(code), 8)
        self.assertTrue(window.attempt(code))
        self.assertFalse(window.open())
        self.assertFalse(window.attempt(code))

    def test_it_closes_after_a_few_wrong_tries(self):
        window = security.PairingWindow(Clock())
        code = window.code
        for _ in range(security.PAIR_TRIES):
            self.assertFalse(window.attempt("00000000x"))
        self.assertFalse(window.attempt(code))

    def test_it_expires(self):
        clock = Clock()
        window = security.PairingWindow(clock)
        clock.now += security.PAIR_SECONDS + 1
        self.assertFalse(window.attempt(window.code))

    def test_a_code_of_the_wrong_type_counts_as_wrong(self):
        window = security.PairingWindow(Clock())
        self.assertFalse(window.attempt(None))
        self.assertFalse(window.attempt(12345678))


class FailureLimiterTest(unittest.TestCase):
    def test_too_many_failures_close_the_door_for_a_while(self):
        clock = Clock()
        limiter = security.FailureLimiter(clock)
        for _ in range(security.FAILURE_LIMIT):
            self.assertFalse(limiter.closed())
            limiter.failed()
        self.assertTrue(limiter.closed())
        clock.now += security.FAILURE_LOCKOUT + 1
        self.assertFalse(limiter.closed())

    def test_old_failures_are_forgotten(self):
        clock = Clock()
        limiter = security.FailureLimiter(clock)
        for _ in range(security.FAILURE_LIMIT - 1):
            limiter.failed()
        clock.now += security.FAILURE_WINDOW + 1
        limiter.failed()
        self.assertFalse(limiter.closed())


class StorageTest(unittest.TestCase):
    def test_the_token_file_is_private_and_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = security.PhoneStore(folder)
            self.assertFalse(store.paired)
            self.assertFalse(store.allows("100.64.0.1"))
            token = store.pair("100.64.0.1", KEY.public)
            self.assertEqual(len(token), 64)
            self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(store.key_path).st_mode), 0o600)
            again = security.PhoneStore(folder)
            self.assertEqual(again.token, token)
            self.assertTrue(again.allows("100.64.0.1"))
            self.assertFalse(again.allows("100.64.0.2"))
            self.assertEqual(again.key, KEY.public)

    def test_pairing_again_replaces_the_old_token(self):
        with tempfile.TemporaryDirectory() as folder:
            store = security.PhoneStore(folder)
            first = store.pair("100.64.0.1", KEY.public)
            other = PhoneKey()
            self.assertNotEqual(store.pair("100.64.0.2", other.public), first)
            self.assertFalse(store.allows("100.64.0.1"))
            message = security.start_message("n", "r", "7", "m")
            self.assertFalse(store.signed(message, KEY.sign(message)))
            self.assertTrue(store.signed(message, other.sign(message)))

    def test_forgetting_the_phone_removes_its_files_and_its_trust(self):
        with tempfile.TemporaryDirectory() as folder:
            store = security.PhoneStore(folder)
            store.pair("100.64.0.1", KEY.public)
            store.forget()
            store.forget()  # twice is fine
            self.assertFalse(store.paired)
            self.assertFalse(store.allows("100.64.0.1"))
            self.assertEqual(os.listdir(folder), [])
            message = security.start_message("n", "r", "7", "m")
            self.assertFalse(store.signed(message, KEY.sign(message)))
            self.assertFalse(security.PhoneStore(folder).paired)

    def test_a_pairing_lasts_thirty_days(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            store = security.PhoneStore(folder, clock)
            store.pair("100.64.0.1", KEY.public)
            self.assertEqual(store.days_left(), security.PAIRING_DAYS)
            clock.now += (security.PAIRING_DAYS - 1) * 86400
            self.assertFalse(store.expired())
            self.assertEqual(store.days_left(), 1)
            clock.now += 86400
            self.assertTrue(store.expired())
            self.assertTrue(security.PhoneStore(folder, clock).expired())  # a restart doesn't renew it

    def test_a_pairing_from_before_phone_keys_counts_as_none(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "phone.json"), "w") as f:
                f.write('{"token": "%s", "address": "100.64.0.1"}' % ("ab" * 32))
            store = security.PhoneStore(folder)
            self.assertFalse(store.paired)
            self.assertFalse(store.allows("100.64.0.1"))

    def test_only_a_p256_public_key_is_taken(self):
        security.phone_key(KEY.public)
        for bad in (None, "", "%%%", "YWJj", KEY.public[:-8]):
            with self.assertRaises(ValueError, msg=bad):
                security.phone_key(bad)

    def test_the_state_folder_is_private(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "state")
            security.private_dir(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
            os.chmod(path, 0o755)
            security.private_dir(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)

    def test_a_link_is_refused_as_the_state_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "elsewhere")
            os.mkdir(target)
            link = os.path.join(folder, "state")
            os.symlink(target, link)
            with self.assertRaises(security.SecurityError):
                security.private_dir(link)


class SignatureTest(unittest.TestCase):
    def test_only_the_phones_key_over_the_same_words_counts(self):
        message = security.start_message("nonce", "run", "7", "mod")
        signature = KEY.sign(message)
        self.assertTrue(security.signed(KEY.public, message, signature))
        self.assertFalse(security.signed(KEY.public, security.start_message("nonce", "run", "8", "mod"), signature))
        self.assertFalse(security.signed(PhoneKey().public, message, signature))
        for bad in (None, "", "%%%", signature[:-4]):
            self.assertFalse(security.signed(KEY.public, message, bad))
        self.assertFalse(security.signed(None, message, signature))

    def test_a_confirmation_names_the_exact_words_heard(self):
        message = security.confirm_message("nonce", "Say yes to merge the release branch.")
        self.assertNotEqual(message, security.confirm_message("nonce", "Say yes to delete the release branch."))
        self.assertTrue(message.startswith("walkie-confirm|nonce|"))
        self.assertNotIn("merge", message)


class MacTest(unittest.TestCase):
    def test_the_two_sides_prove_different_things(self):
        token = "ab" * 32
        self.assertNotEqual(security.phone_mac(token, "s", "c"), security.gateway_mac(token, "s", "c"))
        self.assertNotEqual(security.phone_mac(token, "s", "c"), security.phone_mac(token, "s", "d"))
        self.assertNotEqual(security.phone_mac(token, "s", "c"), security.phone_mac("cd" * 32, "s", "c"))


if __name__ == "__main__":
    unittest.main()
