"""Deployment bundle logic: what may and may not travel (offline - no browser)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from _helpers import tmp_db  # noqa: F401

from app import deploy


class TestCookies(unittest.TestCase):
    def test_only_tiktok_domains_are_exported(self):
        cookies = [{"name": "sessionid", "domain": ".tiktok.com"}, {"name": "ttwid", "domain": "www.tiktok.com"},
                   {"name": "x", "domain": ".tiktokv.com"}, {"name": "SID", "domain": ".google.com"},
                   {"name": "c_user", "domain": ".facebook.com"}, {"name": "sess", "domain": "example.org"}]
        self.assertEqual([c["name"] for c in deploy.tiktok_cookies(cookies)], ["sessionid", "ttwid", "x"])

    def test_cookie_param_keeps_attributes_and_makes_session_cookies_persistent(self):
        now = 1_800_000_000
        persistent = deploy.cookie_param({"name": "a", "value": "1", "domain": ".tiktok.com", "path": "/",
                                          "expires": now + 5000, "secure": True, "httpOnly": True, "sameSite": "None",
                                          "priority": "Medium", "size": 99, "session": False}, now)
        self.assertEqual((persistent["expires"], persistent["secure"], persistent["httpOnly"], persistent["sameSite"]),
                         (now + 5000, True, True, "None"))
        self.assertNotIn("size", persistent)
        session = deploy.cookie_param({"name": "b", "value": "2", "domain": ".tiktok.com", "expires": -1}, now)
        self.assertEqual(session["expires"], now + 30 * 86400)
        self.assertEqual(session["path"], "/")
        expired = deploy.cookie_param({"name": "c", "value": "3", "domain": ".tiktok.com", "expires": now - 10}, now)
        self.assertGreater(expired["expires"], now)
        weird = deploy.cookie_param({"name": "d", "value": "4", "domain": ".tiktok.com", "sameSite": "Bogus"}, now)
        self.assertNotIn("sameSite", weird)


class TestEnvTemplate(unittest.TestCase):
    def test_secrets_are_never_copied(self):
        env = {"OPENROUTER_API_KEY": "sk-or-v1-REALSECRET", "TELEGRAM_BOT_TOKEN": "123:REALTOKEN",
               "TELEGRAM_CHAT_ID": "123456789", "TARGET_PROFILE_URL": "https://www.tiktok.com/@shop",
               "POSTING_ENABLED": "true", "BFP_USER_DATA_DIR": r"X:\project\network\x", "SOME_PASSWORD": "pw123"}
        text = deploy.env_template(env)
        for leaked in ("REALSECRET", "REALTOKEN", "pw123", r"X:\project"):
            self.assertNotIn(leaked, text)
        self.assertIn("TARGET_PROFILE_URL=https://www.tiktok.com/@shop", text)
        self.assertIn("POSTING_ENABLED=true", text)
        self.assertIn("OPENROUTER_API_KEY=", text)
        self.assertIn("BFP_USER_DATA_DIR=\n", text)


class TestSelectAccounts(unittest.TestCase):
    def test_all_by_default_or_just_the_named_ones_in_chain_order(self):
        chain = ["master", "a1", "a2", "owner"]
        self.assertEqual(deploy.select_accounts(chain, None), chain)
        self.assertEqual(deploy.select_accounts(chain, []), chain)
        self.assertEqual(deploy.select_accounts(chain, ["owner", "a1"]), ["a1", "owner"])
        with self.assertRaises(RuntimeError):
            deploy.select_accounts(chain, ["nope"])


class TestRestoreProfiles(unittest.TestCase):
    """restore_profiles() orchestration with the browser part faked: retries, partial runs, re-runs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="restore-"))
        self.bundle, self.root = self.tmp / "bundle", self.tmp / "app"
        (self.bundle / "data").mkdir(parents=True)
        accounts = [{"name": f"real-account-{i}", "username": f"user{i}", "region": "BD", "enabled": True} for i in (1, 2, 3)]
        (self.bundle / "data" / "accounts.template.json").write_text(
            __import__("json").dumps({"master": "master-prof", "accounts": accounts}), encoding="utf-8")
        for name in ["master-prof"] + [a["name"] for a in accounts]:
            (self.bundle / "profiles" / name).mkdir(parents=True)
            (self.bundle / "profiles" / name / "state.json").write_text("{}", encoding="utf-8")
        self.calls, self.fail = [], {}
        self._orig = (deploy.restore_profile, deploy.time.sleep)
        deploy.time.sleep = lambda s: None

        def fake(name, bundle_profile_dir, target, keep_secrets=False):
            self.calls.append(name)
            mode = self.fail.get(name)
            if mode == "raise" or (mode == "raise-once" and self.calls.count(name) == 1):
                raise TimeoutError("timed out")
            ok = mode != "logged-out"
            if ok:
                (target / "Default").mkdir(parents=True, exist_ok=True)
                (Path(bundle_profile_dir) / "state.json").unlink()             # what the real one does on a proven login
            return {"name": name, "logged_in": ok, "username": f"u-{name}", "expected_username": f"u-{name}",
                    "cookies_set": 30, "cookies_now": 30, "local_storage_keys": 60}
        deploy.restore_profile = fake
        self.addCleanup(lambda: (setattr(deploy, "restore_profile", self._orig[0]),
                                 setattr(deploy.time, "sleep", self._orig[1])))

    def accounts(self):
        return __import__("json").loads((self.root / "data" / "accounts.json").read_text(encoding="utf-8"))

    def test_a_slow_browser_is_retried_and_everything_ends_up_logged_in(self):
        self.fail = {"real-account-2": "raise-once"}
        res = deploy.restore_profiles(self.bundle, self.root, progress=lambda *_: None)
        self.assertTrue(res["all_logged_in"], res)
        self.assertEqual(self.calls.count("real-account-2"), 2)
        self.assertEqual([a["name"] for a in self.accounts()["accounts"]], ["real-account-1", "real-account-2", "real-account-3"])

    def test_one_stubborn_account_does_not_block_the_others_and_is_reported(self):
        self.fail = {"real-account-2": "raise"}
        res = deploy.restore_profiles(self.bundle, self.root, progress=lambda *_: None)
        self.assertFalse(res["all_logged_in"])
        self.assertEqual(res["missing"], ["real-account-2"])
        self.assertEqual(self.calls.count("real-account-2"), 3, "three attempts, then give up on that one")
        self.assertEqual([a["name"] for a in self.accounts()["accounts"]], ["real-account-1", "real-account-3"])

    def test_a_rerun_keeps_the_accounts_an_earlier_run_already_proved(self):
        self.fail = {"real-account-2": "logged-out"}
        deploy.restore_profiles(self.bundle, self.root, progress=lambda *_: None)
        self.calls.clear()
        self.fail = {}
        res = deploy.restore_profiles(self.bundle, self.root, progress=lambda *_: None)
        self.assertEqual(self.calls, ["real-account-2"], "only the unfinished account is touched again")
        self.assertTrue(res["all_logged_in"], res)
        self.assertEqual([a["name"] for a in self.accounts()["accounts"]], ["real-account-1", "real-account-2", "real-account-3"])
        self.assertTrue(self.accounts()["master_username"])


class TestVerifyBundle(unittest.TestCase):
    def make(self):
        root = Path(tempfile.mkdtemp(prefix="bundle-"))
        (root / "app").mkdir()
        (root / "app" / "x.py").write_text("x", encoding="utf-8")
        prof = root / "profiles" / "master"
        idb = prof / "indexeddb" / "https_www.tiktok.com_0.indexeddb.leveldb"
        idb.mkdir(parents=True)
        (idb / "000003.log").write_bytes(b"x")
        (prof / "state.json").write_text("{}", encoding="utf-8")
        (root / ".env.template").write_text("A=", encoding="utf-8")
        return root

    def test_a_clean_bundle_passes(self):
        self.assertEqual(deploy.verify_bundle(self.make()), [])

    def test_every_kind_of_personal_or_secret_data_is_caught(self):
        for sneaky in ("Login Data", "History", "Web Data", "Bookmarks", "Cookies", "Local State", "Preferences",
                       ".env", "session.har"):
            root = self.make()
            (root / "profiles" / "master" / sneaky).write_text("x", encoding="utf-8")
            self.assertTrue(deploy.verify_bundle(root), sneaky)
        root = self.make()
        (root / "profiles" / "master" / "Extensions").mkdir()
        self.assertTrue(deploy.verify_bundle(root))
        root = self.make()
        (root / ".env").write_text("OPENROUTER_API_KEY=sk", encoding="utf-8")
        self.assertTrue(any("secrets file" in p or "forbidden" in p for p in deploy.verify_bundle(root)))

    def test_other_sites_indexeddb_and_localstorage_are_caught(self):
        root = self.make()
        other = root / "profiles" / "master" / "indexeddb" / "https_accounts.google.com_0.indexeddb.leveldb"
        other.mkdir(parents=True)
        (other / "000003.log").write_bytes(b"x")
        self.assertTrue(any("not TikTok-only" in p for p in deploy.verify_bundle(root)))
        root = self.make()
        (root / "profiles" / "master" / "Local Storage").mkdir()
        self.assertTrue(deploy.verify_bundle(root))

    def test_manifest_lists_every_file_with_a_hash(self):
        root = self.make()
        m = deploy.build_manifest(root)
        self.assertIn("app/x.py", m["files"])
        self.assertEqual(len(m["files"]["app/x.py"]["sha256"]), 64)


class TestDbSnapshot(unittest.TestCase):
    def test_snapshot_keeps_settings_drops_runtime_state(self):
        db = tmp_db()
        db.set_setting("company_context", "We are a mall.")
        db.set_setting("ai_instructions", "Be nice.")
        db.meta_set("telegram_offset", 77)
        db.meta_set("ai_exhausted_day", "2026-10-07")
        db.meta_set("paused", "1")
        db.set_chat_state(5, "await_ai", {"x": 1})
        db.log_event("x", "y")
        dst = Path(tempfile.mkdtemp()) / "out" / "bot.db"
        deploy.snapshot_db(db.path, dst)
        c = sqlite3.connect(str(dst))

        def get(sql):
            return c.execute(sql).fetchall()

        self.assertEqual(get("SELECT value FROM settings WHERE key='company_context'"), [("We are a mall.",)])
        self.assertEqual(get("SELECT value FROM meta WHERE key='telegram_offset'"), [("77",)])
        self.assertEqual(get("SELECT * FROM meta WHERE key IN ('ai_exhausted_day','paused')"), [])
        self.assertEqual(get("SELECT * FROM chat_state"), [])
        self.assertEqual(get("SELECT * FROM events"), [])
        c.close()


class TestPackageRefusals(unittest.TestCase):
    def test_refuses_a_non_empty_target_without_force(self):
        d = Path(tempfile.mkdtemp())
        (d / "keep.txt").write_text("x")
        with self.assertRaises(RuntimeError):
            deploy.package(d)
        self.assertTrue((d / "keep.txt").exists(), "nothing was deleted")


if __name__ == "__main__":
    unittest.main()
