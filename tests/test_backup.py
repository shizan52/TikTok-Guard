"""The daily copy of the database: consistent, checked, pruned - and never a reason for the app to stop."""
import pathlib
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest import mock

from _helpers import tmp_db  # noqa: F401  (also sets sys.path)

from app import backup


class BackupCase(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="bak-"))
        self.db = tmp_db()
        self.db.set_setting("company_context", "We open 9 to 6.")
        self.db.set_setting("ai_instructions", "Be kind.")

    def make(self, day, **kw):
        return backup.make_backup(self.db.path, self.dir, today=day, **kw)


class TestMakingACopy(BackupCase):
    def test_a_copy_holds_what_cannot_be_read_again_from_tiktok(self):
        got = self.make(date(2026, 10, 15))
        self.assertEqual(got.name, "bot-2026-10-15.db")
        con = sqlite3.connect(got)
        try:
            self.assertEqual(con.execute("SELECT value FROM settings WHERE key='company_context'").fetchone()[0], "We open 9 to 6.")
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            con.close()

    def test_the_copy_is_consistent_while_the_app_keeps_writing(self):
        got_first = self.make(date(2026, 10, 14))
        for i in range(50):
            self.db.set_setting(f"k{i}", "v")
        got = self.make(date(2026, 10, 15))
        con = sqlite3.connect(got)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'k%'").fetchone()[0], 50)
        finally:
            con.close()
        self.assertNotEqual(got_first, got)

    def test_one_copy_per_day(self):
        self.assertIsNotNone(self.make(date(2026, 10, 15)))
        self.assertIsNone(self.make(date(2026, 10, 15)), "today's copy exists: nothing to do")
        self.assertEqual(len(list(self.dir.glob("bot-*.db"))), 1)

    def test_only_the_newest_few_are_kept_and_only_our_own_files_are_ever_deleted(self):
        (self.dir / "my-notes.db").write_text("not ours")
        (self.dir / "bot-latest-manual.db").write_text("not ours either")
        for day in range(1, 11):
            self.make(date(2026, 10, day), keep=3)
        self.assertEqual([r["day"] for r in backup.list_backups(self.dir)], ["2026-10-10", "2026-10-09", "2026-10-08"])
        self.assertTrue((self.dir / "my-notes.db").exists() and (self.dir / "bot-latest-manual.db").exists())

    def test_nothing_to_copy_is_not_an_error(self):
        self.assertIsNone(backup.make_backup(self.dir / "missing.db", self.dir, today=date(2026, 10, 15)))
        empty = self.dir / "empty.db"
        empty.write_bytes(b"")
        self.assertIsNone(backup.make_backup(empty, self.dir, today=date(2026, 10, 15)))

    def test_the_source_is_never_changed(self):
        before = self.db.path.read_bytes() if not (self.db.path.parent / (self.db.path.name + "-wal")).exists() else None
        self.make(date(2026, 10, 15))
        self.assertEqual(self.db.get_setting("ai_instructions"), "Be kind.")
        if before is not None:
            self.assertEqual(self.db.path.read_bytes(), before)


class TestWhenSomethingGoesWrong(BackupCase):
    def test_a_copy_that_fails_its_integrity_check_is_deleted_not_kept_as_a_false_comfort(self):
        with mock.patch.object(backup, "_integrity", return_value="*** in database main *** Page 4: btree error"),                 self.assertLogs("backup", "ERROR"):
            self.assertIsNone(self.make(date(2026, 10, 15)))
        self.assertEqual(list(self.dir.glob("bot-*")), [], "no copy and no half-written file left")

    def test_any_failure_is_logged_and_never_raised(self):
        with mock.patch.object(backup.sqlite3, "connect", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertLogs("backup", "ERROR"):
            self.assertIsNone(self.make(date(2026, 10, 15)))

    def test_a_leftover_partial_file_from_a_crash_is_replaced(self):
        (self.dir / "bot-2026-10-15.part").write_text("half")
        self.assertIsNotNone(self.make(date(2026, 10, 15)))
        self.assertFalse((self.dir / "bot-2026-10-15.part").exists())


class TestWiring(unittest.TestCase):
    def test_the_desktop_app_makes_todays_copy_when_it_starts_and_reports_the_backups(self):
        from app.desktop import DesktopApp
        from app.service import Service
        from _helpers import tmp_db as new_db
        with mock.patch.object(Service, "do_maintenance"), mock.patch("app.backup.make_backup") as make, \
                mock.patch("app.orphans.close_stale_browsers"):
            app = DesktopApp(db=new_db(), port=0, token="t" * 32)
            app.svc._memory_check = lambda: (True, 9000.0)
            app.start()
            self.addCleanup(lambda: None)
            try:
                import time
                end = time.monotonic() + 5
                while not make.called and time.monotonic() < end:
                    time.sleep(0.05)
                make.assert_called_once_with(app.db.path)
                with mock.patch("app.backup.list_backups", return_value=[{"name": "bot-2026-10-15.db", "day": "2026-10-15", "kb": 12}]):
                    diag = app.controller.diagnostics()
                self.assertEqual(diag["backups"][0]["day"], "2026-10-15")
                from app import __version__
                self.assertEqual(diag["app"]["version"], __version__)
            finally:
                app.stop()


if __name__ == "__main__":
    unittest.main()
