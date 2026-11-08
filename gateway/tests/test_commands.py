"""The install and revoke commands, against throwaway login folders and a throwaway state folder."""
import asyncio
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from walkie_gateway import __main__ as commands
from walkie_gateway import security
from walkie_gateway.modlink import ModLink

from .keys import KEY


def run(*argv):
    out = StringIO()
    with redirect_stdout(out):
        code = commands.main(list(argv))
    return code, out.getvalue()


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = self.login(".claude")
        self.personal = self.login(".claude-personal")

    def cleanup(self):
        for folder, _, _ in os.walk(self.home.name):
            os.chmod(folder, stat.S_IRWXU)
        self.home.cleanup()

    def login(self, name):
        path = os.path.realpath(os.path.join(self.home.name, name))
        os.makedirs(os.path.join(path, "projects"))
        return path

    def config(self):
        with open(security.connect_config_path()) as f:
            return json.load(f)

    def test_it_installs_a_read_only_copy_into_each_named_login_and_says_where(self):
        code, out = run("install", "--login", self.work, "--login", self.personal)
        self.assertEqual(code, 0)
        for login in (self.work, self.personal):
            target = os.path.join(login, "skills", "walk-voice")
            self.assertIn("Installed the mod into %s, for the login at %s." % (target, login), out)
            self.assertFalse(os.path.islink(target))
            files = sorted(os.path.relpath(os.path.join(folder, name), target)
                           for folder, _, names in os.walk(target) for name in names)
            self.assertIn("hooks/register.js", files)
            self.assertIn("hooks/p256.js", files)
            self.assertIn(".claude-plugin/plugin.json", files)
            self.assertFalse([name for name in files if name.endswith(".ts") or "types" in name], files)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(target, "hooks", "register.js")).st_mode), 0o444)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(target, "hooks")).st_mode), 0o555)
            with self.assertRaises(PermissionError):
                open(os.path.join(target, "hooks", "register.js"), "a").close()

    def test_only_the_named_logins_may_connect_and_nothing_is_denied_by_default(self):
        run("install", "--login", self.personal)
        self.assertEqual(self.config(), {"logins": {self.personal: {"deny": []}}, "deny": []})
        self.assertFalse(os.path.exists(os.path.join(self.work, "skills")))
        self.assertEqual(stat.S_IMODE(os.stat(security.connect_config_path()).st_mode), 0o600)

    def test_deny_lists_are_kept_per_login_and_across_installs(self):
        run("install", "--login", self.work, "--login", self.personal, "--deny", "Secret-Project",
            "--deny-on", self.personal, "made-up-employer")
        code, out = run("install", "--login", self.work, "--login", self.personal, "--deny", "another-secret")
        self.assertEqual(self.config(), {
            "logins": {self.work: {"deny": []}, self.personal: {"deny": ["made-up-employer"]}},
            "deny": ["another-secret", "secret-project"],
        })
        self.assertIn("  %s, with 1 name of its own denied." % self.personal, out)
        self.assertIn("  %s, with 0 names of its own denied." % self.work, out)
        self.assertIn("Denied on every login: 2 names.", out)
        self.assertNotIn("secret", out)

    def test_a_login_left_out_of_a_later_install_keeps_its_deny_list(self):
        run("install", "--login", self.work, "--login", self.personal, "--deny-on", self.personal, "made-up-employer")
        run("install", "--login", self.work)
        run("install", "--login", self.work, "--login", self.personal)
        self.assertEqual(self.config()["logins"][self.personal], {"deny": ["made-up-employer"]})

    def test_an_install_with_no_login_named_refreshes_every_installed_login(self):
        run("install", "--login", self.work, "--login", self.personal)
        commands._remove(os.path.join(self.personal, "skills", "walk-voice"))
        code, out = run("install")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.personal, "skills", "walk-voice", "hooks", "register.js")))
        self.assertEqual(list(self.config()["logins"]), [self.work, self.personal])

    def test_an_install_with_no_login_at_all_is_refused(self):
        with self.assertRaises(SystemExit):
            run("install")
        self.assertFalse(os.path.exists(security.connect_config_path()))

    def test_a_removed_login_stops_connecting_and_loses_the_mod(self):
        run("install", "--login", self.work, "--login", self.personal)
        code, out = run("install", "--remove-login", self.work)
        self.assertEqual(code, 0)
        self.assertEqual(list(self.config()["logins"]), [self.personal])
        self.assertFalse(os.path.exists(os.path.join(self.work, "skills", "walk-voice")))
        self.assertIn("Removed the mod from the login at %s." % self.work, out)

    def test_a_removed_login_keeps_its_deny_list_for_when_it_comes_back(self):
        run("install", "--login", self.work, "--login", self.personal, "--deny-on", self.personal, "made-up-employer")
        run("install", "--remove-login", self.personal)
        self.assertEqual(list(self.config()["logins"]), [self.work])
        code, out = run("install", "--login", self.personal)
        self.assertEqual(code, 0)
        self.assertEqual(self.config()["logins"][self.personal], {"deny": ["made-up-employer"]})
        self.assertNotIn("removed", self.config())
        self.assertIn("The login at %s gets back its own deny list, kept from when it was removed: 1 name." % self.personal, out)

    def test_removing_a_login_that_was_never_installed_is_refused(self):
        run("install", "--login", self.personal)
        with self.assertRaises(SystemExit):
            run("install", "--remove-login", self.work)
        self.assertEqual(list(self.config()["logins"]), [self.personal])

    def test_allow_takes_a_name_off_only_the_list_it_names(self):
        run("install", "--login", self.work, "--login", self.personal, "--deny", "secret-project",
            "--deny-on", self.personal, "made-up-employer", "--deny-on", self.work, "made-up-employer")
        run("install", "--allow", "Secret-Project", "--allow-on", self.personal, "made-up-employer")
        self.assertEqual(self.config(), {
            "logins": {self.work: {"deny": ["made-up-employer"]}, self.personal: {"deny": []}},
            "deny": [],
        })

    def test_allowing_a_name_that_is_not_denied_is_refused(self):
        run("install", "--login", self.personal, "--deny", "secret-project")
        for argv in (["--allow", "another-secret"], ["--allow-on", self.personal, "secret-project"],
                     ["--allow-on", self.work, "secret-project"]):
            with self.assertRaises(SystemExit):
                run("install", *argv)
        self.assertEqual(self.config()["deny"], ["secret-project"])

    def test_a_reinstall_replaces_the_copy_and_an_old_link(self):
        skills = os.path.join(self.personal, "skills")
        os.makedirs(skills)
        os.symlink(commands.MOD_DIR, os.path.join(skills, "walk-voice"))
        run("install", "--login", self.personal)
        self.assertFalse(os.path.islink(os.path.join(skills, "walk-voice")))
        run("install", "--login", self.personal)
        self.assertEqual(sorted(os.listdir(skills)), ["walk-voice"])

    def test_a_folder_that_is_no_login_is_refused(self):
        other = os.path.join(self.home.name, "elsewhere")
        os.makedirs(other)
        with self.assertRaises(SystemExit):
            run("install", "--login", other)
        self.assertFalse(os.path.exists(security.connect_config_path()))

    def test_a_deny_for_a_login_not_installed_is_refused(self):
        with self.assertRaises(SystemExit):
            run("install", "--login", self.personal, "--deny-on", self.work, "made-up-employer")

    def write_config(self, text):
        os.makedirs(os.path.dirname(security.connect_config_path()), exist_ok=True)
        with open(security.connect_config_path(), "w") as f:
            f.write(text)

    def test_an_unreadable_settings_file_stops_the_install_and_keeps_every_denied_name(self):
        # A hand edit left a trailing comma. Read as empty, it would have been rewritten with no deny names.
        run("install", "--login", self.personal, "--deny", "secret-one", "--deny-on", self.personal, "made-up-corp")
        with open(security.connect_config_path()) as f:
            broken = f.read().replace('"secret-one"', '"secret-one",')
        self.write_config(broken)
        with self.assertRaises(SystemExit) as refused:
            run("install", "--login", self.personal)
        self.assertIn("Can't read %s" % security.connect_config_path(), str(refused.exception))
        self.assertIn("Nothing was changed", str(refused.exception))
        with open(security.connect_config_path()) as f:
            self.assertEqual(f.read(), broken)

    def test_a_settings_file_of_the_wrong_shape_stops_the_install(self):
        run("install", "--login", self.personal)
        for config in ([], {"deny": "secret-one"}, {"deny": [1]}, {"logins": []},
                       {"logins": {self.personal: {"deny": "made-up-corp"}}, "deny": []},
                       {"logins": {self.personal: {"deny": []}}, "removed": {self.work: {"deny": "made-up-corp"}}, "deny": []}):
            text = json.dumps(config)
            self.write_config(text)
            with self.assertRaises(SystemExit, msg=text):
                run("install", "--login", self.personal)
            with open(security.connect_config_path()) as f:
                self.assertEqual(f.read(), text)

    def test_a_denied_name_has_no_slash(self):
        with self.assertRaises(SystemExit):
            run("install", "--login", self.personal, "--deny", "made/up")


class RevokeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = security.PhoneStore(self.folder.name)
        self.store.pair("100.64.0.1", KEY.public)

    async def asyncTearDown(self):
        self.folder.cleanup()

    async def test_with_no_gateway_running_the_pairing_goes(self):
        code, out = await asyncio.to_thread(run, "revoke", "--state", self.folder.name)
        self.assertEqual(code, 0)
        self.assertIn("Revoked the phone in %s. No gateway answered there" % self.folder.name, out)
        self.assertIn("run revoke with that --state too", out)
        self.assertFalse(security.PhoneStore(self.folder.name).paired)
        self.assertFalse(os.path.exists(self.store.key_path))

    async def test_a_running_gateway_hears_it_at_once(self):
        class Conversation:
            revoked = False

            def revoke_phone(self):
                self.revoked = True

            def sessions_changed(self):
                pass

        conversation = Conversation()
        link = ModLink(os.path.join(self.folder.name, "mod.sock"), conversation)
        await link.start()
        try:
            code, out = await asyncio.to_thread(run, "revoke", "--state", self.folder.name)
        finally:
            await link.stop()
        self.assertTrue(conversation.revoked)
        self.assertIn("The running gateway closed its connection", out)
        self.assertFalse(security.PhoneStore(self.folder.name).paired)


if __name__ == "__main__":
    unittest.main()
