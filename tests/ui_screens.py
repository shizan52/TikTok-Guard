"""Open the desktop app in a real headless browser against the fake world and save screenshots of every screen (light + dark):

    python tests/ui_screens.py [output_folder]

Used to LOOK at the design; the assertions live in test_desktop_ui.py."""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ui_support import UI, World  # noqa: E402


def main(out: pathlib.Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    world, ui = World(), None
    try:
        url = world.start()
        ui = UI()
        ui.open(url)
        ui.wait("document.querySelector('#url-input')", 15, "home loaded")
        time.sleep(0.6)
        ui.shot(out / "01_home.png")
        ui.fill("#url-input", "https://www.tiktok.com/@examplebrand/video/7100000000000000001")
        ui.click("#analyze-btn")
        ui.wait("document.querySelector('[data-role=progress]')", 8, "progress card")
        ui.shot(out / "02_progress.png")
        ui.wait("document.querySelector('[data-role=results]')", 40, "results")
        time.sleep(0.5)
        ui.shot(out / "03_results.png")
        ui.click("[data-act=reply]")
        ui.wait("document.querySelector('[data-role=composer]')", 8, "composer")
        ui.click("[data-act=ai]")
        ui.wait("document.querySelector('[data-role=text]').value.length > 5", 10, "ai text")
        ui.shot(out / "04_composer.png")
        ui.click("[data-act=send]")
        ui.wait("document.querySelector('.status-line') && document.querySelector('.status-line').innerText.includes('Sending')", 6, "sending")
        ui.shot(out / "04b_sending.png")
        ui.wait("document.querySelector('.proof img')", 20, "proof")
        time.sleep(0.8)
        ui.shot(out / "05_sent.png")
        # a held reply (posting OFF) and a failed one, on the other comments
        world.post_results[:] = [{"ok": False, "error": "TikTok did not confirm that the comment was published"}]
        ui.click("[data-act=reply]", 1)
        ui.wait("document.querySelectorAll('[data-role=composer]').length === 2", 8)
        ui.fill("[data-role=text]", "Peço desculpa, vamos resolver hoje mesmo.", 1)
        ui.click("[data-act=send]", 1)
        ui.wait("document.querySelectorAll('.status-line.bad').length === 1", 20, "failed state")
        time.sleep(0.4)
        ui.shot(out / "05b_failed.png")
        ui.click("#side-posting")
        ui.wait("document.querySelector('#side-post-state').innerText === 'OFF'", 6)
        ui.click("[data-act=send]", 1)
        ui.wait("document.querySelectorAll('.status-line.warn').length === 1", 10, "held state")
        ui.shot(out / "05c_held.png")
        ui.click("#side-posting")
        for page in ("replies", "accounts", "ai", "controls", "help"):
            ui.ev(f"location.hash = '#/{page}'")
            time.sleep(1.2)
            ui.shot(out / f"06_{page}.png")
        ui.ev("location.hash = '#/accounts'")
        ui.wait("document.querySelector('#add-account')", 8)
        world.window_kwargs = dict(login_after=10 ** 6)
        ui.click("#add-account")
        ui.wait("document.querySelector('[data-role=flow][data-state=window_open]')", 10)
        time.sleep(0.3)
        ui.shot(out / "06b_add_account_waiting.png")
        ui.click("[data-role=flow] .btn.danger")
        time.sleep(1.2)
        ui.ev("location.hash = '#/home'")
        time.sleep(0.8)
        ui.ev("document.documentElement.setAttribute('data-theme', 'dark')")
        time.sleep(0.3)
        ui.shot(out / "07_dark_home.png")
        ui.ev("location.hash = '#/accounts'")
        time.sleep(1.0)
        ui.shot(out / "08_dark_accounts.png")
        ui.resize(760, 900)
        time.sleep(0.4)
        ui.shot(out / "09_narrow.png")
    finally:
        if ui:
            ui.close()
        world.stop()
    print("screenshots in", out)


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("ui_shots"))
