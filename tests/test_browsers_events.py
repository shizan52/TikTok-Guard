"""Browser discovery (which window opens for 'Add account') and the event bus - offline."""
import threading
import time
import unittest

from _helpers import ROOT  # noqa: F401

from app import browsers
from app.events import EventBus

WIN = {"PROGRAMFILES": r"C:\Program Files", "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
       "LOCALAPPDATA": r"C:\Users\me\AppData\Local"}
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BRAVE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def installed(*paths):
    have = {p.lower() for p in paths}
    return lambda p: p.lower() in have


class TestBrowsers(unittest.TestCase):
    def find(self, *paths, env=None, platform="win32"):
        return browsers.find_browsers(env={**WIN, **(env or {})}, platform=platform, exists=installed(*paths),
                                      which=lambda n: None)

    def test_the_kind_comes_from_the_executable(self):
        self.assertEqual([browsers.kind_of(p) for p in (CHROME, BRAVE, EDGE, "/usr/bin/chromium", "/opt/x/browser")],
                         ["chrome", "brave", "edge", "chromium", "chromium"])
        self.assertEqual(browsers.kind_of("/usr/bin/google-chrome-stable"), "chrome")
        self.assertEqual(browsers.kind_of("/usr/bin/microsoft-edge"), "edge")

    def test_installed_windows_browsers_are_found_in_chrome_brave_edge_order(self):
        found = self.find(EDGE, BRAVE, CHROME)
        self.assertEqual([b["kind"] for b in found], ["chrome", "brave", "edge"])
        self.assertEqual(found[0]["name"], "Google Chrome")

    def test_missing_browsers_are_left_out(self):
        self.assertEqual([b["kind"] for b in self.find(BRAVE)], ["brave"])
        self.assertEqual(self.find(), [])

    def test_a_per_user_install_is_found_too(self):
        user_chrome = r"C:\Users\me\AppData\Local\Google\Chrome\Application\chrome.exe"
        self.assertEqual(self.find(user_chrome)[0]["path"], user_chrome)

    def test_the_configured_executable_comes_first_and_is_not_listed_twice(self):
        found = self.find(CHROME, BRAVE, env={"BFP_PATCHED_CHROMIUM": BRAVE})
        self.assertEqual([b["kind"] for b in found], ["brave", "chrome"])
        self.assertTrue(found[0]["configured"])
        self.assertFalse(found[1]["configured"])

    def test_a_configured_path_that_does_not_exist_is_ignored(self):
        found = self.find(CHROME, env={"BFP_PATCHED_CHROMIUM": r"D:\gone\brave.exe"})
        self.assertEqual([b["kind"] for b in found], ["chrome"])

    def test_linux_and_mac_locations(self):
        which = {"google-chrome": "/usr/bin/google-chrome", "brave-browser": "/usr/bin/brave-browser"}.get
        found = browsers.find_browsers(env={}, platform="linux", exists=lambda p: True, which=which)
        self.assertEqual([b["kind"] for b in found], ["chrome", "brave"])
        mac = browsers.find_browsers(env={}, platform="darwin",
                                     exists=lambda p: "Google Chrome" in p)
        self.assertEqual([b["kind"] for b in mac], ["chrome"])

    def test_auto_prefers_the_configured_browser_then_chrome(self):
        pick = lambda pref, **env: browsers.choose(pref, env={**WIN, **env}, platform="win32",
                                                   exists=installed(CHROME, BRAVE, EDGE), which=lambda n: None)
        self.assertEqual(pick("auto")["kind"], "chrome")
        self.assertEqual(pick("auto", BFP_PATCHED_CHROMIUM=BRAVE)["kind"], "brave")
        self.assertEqual(pick("")["kind"], "chrome")

    def test_a_named_browser_is_used_or_refused_never_substituted(self):
        have = dict(env=WIN, platform="win32", exists=installed(BRAVE), which=lambda n: None)
        self.assertEqual(browsers.choose("brave", **have)["kind"], "brave")
        self.assertIsNone(browsers.choose("chrome", **have), "Chrome was asked for and is not installed")
        self.assertIsNone(browsers.choose("auto", env=WIN, platform="win32", exists=lambda p: False, which=lambda n: None))


class TestEventBus(unittest.TestCase):
    def test_ids_rise_and_since_returns_only_newer_events(self):
        bus = EventBus()
        a = bus.publish("approval.created", {"id": "x"})
        b = bus.publish("log", {"kind": "post"}, "warn")
        self.assertEqual((a["id"], b["id"], b["level"], b["data"]), (1, 2, "warn", {"kind": "post"}))
        self.assertEqual([e["id"] for e in bus.since(0)], [1, 2])
        self.assertEqual([e["id"] for e in bus.since(1)], [2])
        self.assertEqual(bus.since(2), [])
        self.assertEqual(bus.last_id(), 2)

    def test_the_buffer_is_bounded_and_a_late_reader_gets_what_is_left(self):
        bus = EventBus(capacity=3)
        for i in range(10):
            bus.publish("n", {"i": i})
        self.assertEqual([e["data"]["i"] for e in bus.since(0)], [7, 8, 9])
        self.assertEqual(bus.last_id(), 10)

    def test_event_data_is_copied(self):
        bus = EventBus()
        data = {"k": 1}
        bus.publish("t", data)
        data["k"] = 2
        self.assertEqual(bus.since(0)[0]["data"], {"k": 1})

    def test_wait_returns_at_once_when_events_exist(self):
        bus = EventBus()
        bus.publish("t")
        t0 = time.monotonic()
        self.assertEqual(len(bus.wait(0, timeout=5)), 1)
        self.assertLess(time.monotonic() - t0, 1)

    def test_wait_blocks_until_an_event_arrives(self):
        bus = EventBus()
        threading.Timer(0.2, lambda: bus.publish("late")).start()
        t0 = time.monotonic()
        got = bus.wait(0, timeout=5)
        self.assertEqual([e["type"] for e in got], ["late"])
        self.assertGreaterEqual(time.monotonic() - t0, 0.15)

    def test_wait_gives_up_after_the_timeout(self):
        bus = EventBus()
        t0 = time.monotonic()
        self.assertEqual(bus.wait(0, timeout=0.3), [])
        self.assertLess(time.monotonic() - t0, 2)

    def test_a_wake_up_without_news_does_not_end_the_wait(self):
        """wake_all() only lets blocked readers look again (a server shutting down); with nothing new they wait on."""
        bus = EventBus()
        out = []
        t = threading.Thread(target=lambda: out.append(bus.wait(0, timeout=5)))
        t.start()
        time.sleep(0.1)
        bus.wake_all()
        t.join(0.3)
        self.assertTrue(t.is_alive(), "still waiting: nothing happened")
        bus.publish("x")
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertEqual(out[0][0]["type"], "x")


if __name__ == "__main__":
    unittest.main()
