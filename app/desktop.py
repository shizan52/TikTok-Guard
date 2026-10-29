"""The desktop app's backend in one object: database, AI, accounts, post analysis and the local API - and NOTHING that runs by
itself. No scanning of a profile every two minutes, no Telegram, no notifications: the app does what the person asks, when
they ask (paste a link -> find the negative comments -> reply). `python app.py` wraps this in a window.
"""
import os

import config
from app.api import ApiServer, load_token
from app.db import DB
from app.service import Service


class DesktopApp:
    def __init__(self, *, db: DB | None = None, ai=None, reader=None, poster=None, host: str | None = None,
                 port: int | None = None, token: str | None = None, clean_leftovers: bool = True, verifier=None,
                 backups: bool = True, maintenance: bool = True) -> None:
        # Never Telegram: only ONE process may poll a bot token and the production server already does. (Also makes sure a
        # card changed in the app does not try to edit a Telegram message.)
        config.TELEGRAM_BOT_TOKEN = config.TELEGRAM_CHAT_ID = ""
        # a person works through a post comment by comment: the browser that opened it stays open for a few minutes, so the
        # next reply needs no new page load (TikTok refuses profile pages that are loaded again and again). 0 = off.
        from app import tiktok_monitor
        # a person's computer is not a shared server: the server's "at least 900 MB free before a browser starts" would refuse
        # work on an ordinary PC that has a browser kept open. A browser needs about 300-450 MB.
        self._master_optional_before = config.MASTER_OPTIONAL
        config.MASTER_OPTIONAL = True                      # no phantom "master" account when .env names no browser folder
        self._min_free_before = config.MIN_FREE_MB
        if "MIN_FREE_MB" not in os.environ:
            config.MIN_FREE_MB = min(config.MIN_FREE_MB, 400)
        self._warm_before = tiktok_monitor.WARM_SECONDS
        tiktok_monitor.WARM_SECONDS = max(0, int(os.environ.get("WARM_SESSION_SECONDS", "300") or 0))
        self.db = db or DB()
        self.svc = Service(self.db, ai=ai, poster=poster, reader=reader, verifier=verifier, scanner=lambda d: {"opened": 0, "new": 0},
                           api=False, monitor=False, telegram=False,
                           watchdog_s=float(os.environ.get("JOB_WATCHDOG_SECONDS", "900") or 0) or None)
        self.svc.controller.apply_runtime()                  # posting on/off, limits, AI key ... the person set last time
        self.host, self.port, self.token = host or config.API_HOST, config.API_PORT if port is None else port, token
        self.server: ApiServer | None = None
        self.clean_leftovers, self.backups, self.maintenance = clean_leftovers, backups, maintenance

    @property
    def controller(self):
        return self.svc.controller

    def start(self) -> str:
        """Start the worker (it only runs what it is asked) and the API. Returns the address of the app."""
        if self.clean_leftovers:          # browsers an earlier run left on our account folders would make those accounts "in use"
            try:
                from app import orphans
                orphans.close_stale_browsers(orphans.our_profile_dirs())
            except Exception:  # noqa: BLE001 - never a reason not to start
                pass
        self.svc.start()
        self.server = ApiServer(self.svc.controller, self.svc.events, self.svc.accounts, self.svc, host=self.host,
                                port=self.port, token=self.token or load_token()).start()
        if self.maintenance:
            self.svc.run_background(self.svc.do_maintenance)     # one tidy-up pass at start (old screenshots, stale rows), then idle
        if self.backups:
            from app import backup
            path = self.db.path
            self.svc.run_background(lambda: backup.make_backup(path))   # today's copy of THIS database (one per day, the last 7 are kept)
        return self.server.url

    @property
    def token_value(self) -> str:
        return self.server.token if self.server else ""

    def sending(self) -> bool:
        """Is a reply on its way to TikTok right now? Closing then would cut it in half: nobody could say whether it was posted."""
        state = self.svc.busy()
        return bool(state["sending"] or (state["job"] and state["job"]["kind"] == "post"))

    def stop(self, wait: float = 0.0) -> None:
        """Stop the app. `wait` > 0 lets a reply that is being sent finish first (for at most that many seconds)."""
        self.svc.stop(wait=wait)
        if self.server is not None:
            self.server.stop()
        from app import tiktok_monitor
        tiktok_monitor.WARM_SECONDS = self._warm_before
        config.MIN_FREE_MB = self._min_free_before
        config.MASTER_OPTIONAL = self._master_optional_before
