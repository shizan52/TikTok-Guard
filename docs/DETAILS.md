# TikTok Guard - detailed manual

> The full internal manual. Brand names, accounts and example data in it are fictional placeholders. The short overview is in the [README](../README.md).

Watches the brand's TikTok profile, finds **negative comments** and **customer questions**,
tells you on Telegram with full details and a suggested reply, and posts the reply **only when you tap the button**.
A question gets an *answer* drafted from the company information you saved with `/ai_context` (address, history, shipping,
prices ... whatever it says); when the information does not cover the question, the draft says the team will confirm it and
invites a direct message - it never makes an answer up.

```
python run.py serve      # the one command to run in production
```

Everything is controlled from Telegram — send `/help` to the bot.

**Monitoring is always on.** From the moment the service starts, the bot checks the profile every **120 seconds** by
itself (start to start), forever - there is no `/scan`, `/pause` or `/resume`, nothing to remember. A new negative comment
becomes an alert card within about two minutes of TikTok showing it.

## What it watches
* the **latest 5 posts** + **2 pinned posts** of the profile (change live: `/watch latest 5`, `/watch pinned 2`; the
  profile currently has 3 pinned posts, the first two in profile order are used).
* **the latest five always slide forward**: when a new post goes live it joins the five and the oldest one drops out (its
  stored comments are kept; it just is not read any more).
* one page load of the profile tells it every post's comment count, so a post is **opened only when its count
  changed** (or it was not read for `RECHECK_MAX_HOURS`, default 12). Posts are opened by *clicking their tile*, never
  by loading the `/video/<id>` URL (TikTok answers repeated loads of one video URL with an empty HTTP 403).
* what TikTok itself hides (comments with `status: 2` - "visible to the author only", e.g. caught by the page's comment
  filter) is not delivered to any viewer, so the bot cannot see it either.

## Telegram commands
| command | what it does |
|---|---|
| `/ai` | show / change **how the AI replies** (tone, rules). Buttons: Edit, Default, Undo, Test. Also `/ai <new text>` |
| `/ai_context` | show / change the **company information** the AI answers from. Replace, Add, Undo, Clear, Download; or send a `.txt` file. Also `/ai_context <text>` / `/ai_context add <text>` |
| `/status` | monitoring (always on), time of the last check, AI budget, database size, what is waiting |
| `/pending` | replies waiting for your decision |
| `/posts` | the watched posts, comment counts, last read |
| `/questions on\|off` | customer questions get an alert + a drafted answer (default **on**; a question the AI budget could not handle is still announced, and answered later) |
| `/watch` | how many posts to watch |
| `/budget N` | AI requests per day |
| `/accounts` | TikTok accounts used for posting |
| `/comment <link> [text]` | **comment on any TikTok post, or reply to one comment, from its link** - see below. Pasting the link alone works too |
| `/cleanup` | free disk space now |

Each alert card shows: negative / question, severity and category, the post (pinned or latest #n, comment count,
link), who wrote it, likes, how long ago, **why it was flagged**, and the suggested reply with buttons
**✅ Post reply · ✏️ Edit · 🔄 Regenerate (max 3) · 🚫 Ignore**. Only the chat in `TELEGRAM_CHAT_ID` is obeyed.

## Comment on any TikTok link (`/comment`)
Got the link of a post or of one comment, and want the page's accounts to comment there? Send it to the bot:
```
/comment https://vm.tiktok.com/ZM…/ Thanks for sharing!      link of a POST     -> a new comment under that post
/comment <link of one comment> We agree, thank you!         link of a COMMENT  -> a reply to that comment
```
Just pasting the link works as well, and `/comment` on its own asks for it. Short links from the TikTok app
(`vm.tiktok.com/…`, `tiktok.com/t/…`) are expanded for you; the bot only ever contacts tiktok.com hosts.

1. A **card** shows what the link points to (post / comment), which account will post, and the comment text.
2. The text: type it after the link, or tap **✏️ Write**, or tap **✨ AI write** and say in a few words what the comment
   should say ("thank them and invite them to the store") - the AI writes it with your `/ai` style and `/ai_context`
   facts, in the language of your brief (costs one AI request; ✨ again for another version).
3. **👤 Account** cycles Auto → each account. *Auto* uses the normal order (it falls over to the next account when one
   is captcha-walled); a chosen account is used alone.
4. **✅ Post comment** sends it, exactly like an approved reply (screenshot proof afterwards). **Nothing is ever sent
   without that tap**, and with `POSTING_ENABLED=false` the tap only marks it ready.

Safeguards: at most `LINK_COMMENTS_PER_DAY` (10, UTC day; 0 = no limit) per day · one open card per target · a card warns
when the bot already commented on the same target in the last 24 h · 150 characters · the usual `POST_MIN_GAP_SECONDS`
spacing · link comments never wait for, or use up, the AI budget on their own (only your ✨ tap does) · only the
configured Telegram chat can use it. Limits: the post must allow comments, and a link to a *reply under a comment* (a
sub-comment) is not supported - link the top-level comment instead.

Command-line twin: `python run.py comment <link> "<text>" [--account NAME] [--post]` - a **dry run** unless `--post` (the
page is opened, the text typed and checked, a screenshot saved, nothing sent).

## Desktop app (`python app.py`)
```
python app.py            # opens the app in its own window (Edge / Chrome / Brave / Chromium - whichever is installed)
python app.py --no-window   # only the local server; open the printed address in any browser
```
On Windows double-click **`TikTok Guard.bat`**. Closing the app's window stops the app; starting it again while one is already running
opens one more window on the running app (two copies are never started - they would fight over the same accounts).
The desktop app is the Telegram bot's replacement for a person sitting at a computer. **There is no continuous monitoring and
there are no notifications** - nothing happens until the person asks. The Telegram bot token is never used by the app (the
production server already polls it), so the app and the server can run side by side without fighting over it.

* **Home** - paste the link of ANY TikTok post. The app opens it with one of your accounts (read-only), reads its comments,
  lets the AI pick out the **negative** ones (and the questions) and lists them with the reason, the author, likes and age.
  Filters: Negative / Questions / All. A result is kept, so coming back to the same link shows it at once.
* **Reply** - on a comment press *Reply*: write your own text or press *AI draft* (the AI answers in the comment's language, with
  your tone from *AI* and facts from *Company info*). *Account* chooses who replies (Automatic = the normal order, or one
  account alone). *Send* posts it and the app shows the **proof screenshot** - the comment panel cropped to the comment and the
  reply - exactly like the Telegram bot. Nothing is sent without that button; with posting switched off the reply is kept as
  "ready" and nothing leaves the computer.
* **Replies** - every draft and sent reply, with its proof, status and the option to try again, edit or discard.
* **Accounts** - *Add account* opens a fresh browser window on TikTok's login page (its own profile folder, never the person's
  browser profile), detects the login, saves the account and closes the window. Check, log in again, choose the order, remove.
* **AI**, **Controls** (posting on/off, replies per day, how many comments to read, brand name, OpenRouter key), **Help** - the sidebar
  also shows the posting switch and the state of the accounts at all times.

### Made to be left running
* **You always see what it is doing.** A strip in the sidebar - on every page - shows the browser's current step of a reply being
  sent ("Opening the post with @name... 12 s") or of a post being read; the reply's own card shows the same.
* **Quick after the first step.** The browser that just opened a post stays open for 5 minutes (`WARM_SESSION_SECONDS`, 0 = off): the
  next reply under that post needs no navigating at all, another post of the same owner only a click. It closes by itself when idle,
  at once when memory runs short or an account has to be checked/removed, and when the app stops.
* **TikTok's limit on profile pages.** TikTok refuses a profile page that one account loads again and again (an empty HTTP 403 for
  several minutes; opening a post by its address meets a captcha). So a profile page is loaded once per kept browser; reading and
  replying on Auto start with the same account (the operator's order), so the browser that read a post is the one that replies - an
  account that just met a wall, or loaded two profile pages lately, goes to the back; an account TikTok refuses is given up after
  seconds, not minutes, and the next one is tried (at most 3); when none works the card says so plainly. A click that opens the
  wrong post (the grid shifts under the pointer while it scrolls) is noticed and repeated instead of reloading the page.
* **A stuck browser cannot freeze the app.** A browser job that has run for 15 minutes (`JOB_WATCHDOG_SECONDS`, 0 = off) has its browser
  closed, so it fails cleanly and the single worker is free again. On an ordinary PC the "enough free memory before a browser
  starts" threshold is 400 MB (the server keeps 900; `MIN_FREE_MB` in `.env` overrides both).
* **Closing the app is safe.** Close the window while a reply is on its way and the app finishes sending it first (the console
  program waits, and says so in a message box - or in the console when it has one; Ctrl+C quits anyway). If the app was killed or crashed mid-send, the card is found at the next start as
  **"Check on TikTok"** - it is never left spinning and never re-sent by itself; a person looks, then may send again.
  Browsers a killed app left behind on its account folders are closed at start (only automation-launched ones on this app's own folders).
* **"Check on TikTok".** A reply that failed or was interrupted has a button that reads the post again and compares the comment's reply
  count and TikTok's preview of its first replies with what was there when the reply was sent: our text there -> the card becomes
  Sent; more replies than before -> "Maybe sent" (open the post to be sure); none -> "Not sent - safe to send again". Nothing is
  re-sent by itself.
* **Judging comments is cheap and consistent.** A text is judged once: identical comments (in this post, in an earlier round, on another
  post) share one verdict and one AI question; a whole post is judged in big requests (`AI_BATCH_SIZE_ON_DEMAND`, 24 at a time) and the
  screen shows "Checking which comments are negative... 36 of 120". A plain demand ("bring X back") is mild, never "hostile".
* **Accounts TikTok just refused are marked "resting"** where an account is chosen, and a daily copy of the database
  (`data/backups/bot-YYYY-MM-DD.db`, the last 7, integrity-checked) protects the AI instructions, the company information and the cards.
* **A log and a self-check.** `data/logs/desktop.log` (2 MB x 5, tokens/keys masked) has every step with its timing and every error
  with its traceback. Help -> Diagnostics runs the checks and shows the recent problems and the end of the log (one click copies it);
  Home shows a banner when something is missing. `python app.py --check` prints the same checks (browser, accounts, AI key,
  company information, disk, memory, internet) and exits with 1 only if something essential is missing.

Everything the screen does goes through the same local API ([docs/API.md](docs/API.md)); the page is one file
(`app/web/desktop.html`, no external scripts or fonts) and renders text with `textContent` only. Safeguards of the link comments
(per-day cap, one open card per target, 150 characters, spacing between posts) apply to replies from the app too.
A post is opened the way a person does - through its **owner's profile page and a click on the tile** - because TikTok answers
repeated direct loads of one `/video/<id>` URL with a captcha or an empty HTTP 403; the direct load is only the fallback.

## The Windows exe (`production/`)
The client gets **one file: `TikTok-Guard-Setup-<version>.exe`** - an installer (Inno Setup, `buildexe/installer.iss`): Next -> Install -> Finish,
per-user in `%LOCALAPPDATA%\Programs\TikTok Guard` (no administrator password), Start menu group (app, diagnostics, read me, uninstall), an optional
desktop shortcut, an entry in Windows Settings -> Apps, and the app opens when it finishes. Running Setup again updates in place and keeps the
client's data; uninstalling asks whether to delete the data (default: keep). Setup and uninstaller refuse to touch a running app (the app holds the
named mutex `TikTokGuardRunning`). `production/portable/` holds the same program as a ZIP for anyone who cannot run an installer.

Build chain: `python buildexe/build.py build` (PyInstaller -> **`TikTok Guard.exe`**, one folder: the exe + `_internal`, no Python needed on
the client's PC) -> `scan` -> `python buildexe/test_exe.py` -> `installer` (Inno Setup is looked for in `buildexe/tools/InnoSetup`, then in the
usual folders) -> `python buildexe/test_installer.py` -> `package` (copies Setup + the portable ZIP + `CHECKSUMS.txt` + `BUILD-INFO.txt` + the
client's `README.txt` = `buildexe/client_README.txt` to `production/`).

* **Two programs, one folder.** `TikTok Guard.exe` has no console window; `TikTok Guard Diagnostics.exe` is the same program as a
  console program: it runs the self-test and saves `last-report.txt` (versions, checks, the end of the log - secrets masked) for support.
  Options of the main exe (they attach to the console they were started from): `--version`, `--check`, `--self-test`, `--no-window`, `--port N`.
* **Where things live.** The program's own files are read from the bundle; everything the person creates (settings, accounts, database,
  screenshots, logs) is in `%LOCALAPPDATA%\TikTok Guard` (`TIKTOK_GUARD_HOME` overrides it). `.env` there beats the defaults shipped in
  the bundle (`buildexe/client_defaults.env`: brand, profile URL, `BFP_BACKEND=patched`, posting on - **no secrets**; the client enters
  their own OpenRouter key on the Controls page). A fresh install has no "master" account (only accounts added in the app).
* **Engine.** bfp (a separate browser-engine module, copied into the exe at build time; not part of this repository) drives the client's own Edge/Chrome/Brave over its debugging
  port through the Playwright driver that is inside the bundle. The 109 MB `patchright` package is deliberately NOT bundled (its own
  PyInstaller hook would add it - the spec removes it); `BFP_BACKEND=patched` is the engine that was proven against the real site.
* **A windowless program must still speak.** Failures before the app exists, a reply still being sent when the window is closed, no
  browser found: message boxes (`TIKTOK_GUARD_NO_DIALOG=1` prints instead - for automated runs); everything else goes to `data/logs`.
  stdout/stderr are replaced by log streams when there is no console; child processes start with `CREATE_NO_WINDOW`.
* **Not in the build** (checked by `build.py scan`, inside the archives too): `.env`, API keys, `data/`, `network/` (accounts and cookies),
  screenshots, databases, logs, tests, the real patchright. The scan also looks for this machine's actual secret values.
* **What `test_exe.py` checks on the built files**: PE metadata (icon, version, no administrator manifest, GUI vs console), `--version`,
  `--self-test` (a REAL browser is started and driven by the packaged engine), `--check` on a fresh install, the diagnostics report, a copy
  in a folder with spaces/accents run from another directory with hostile `PYTHONPATH`/`PYTHONHOME`, nothing written into the program folder,
  data in `%LOCALAPPDATA%`, a start that fails reports itself, the running server + every screen in a real headless browser with no
  script errors, a fresh install shows no phantom account, a saved key/brand survive a restart and never reach the log, a second start joins
  the running app, a taken port is replaced by a free one, the real app window opens and closing it ends the program cleanly (no leftovers),
  and that no console window flashed up. `python buildexe/live_exe.py` reads one real public post through the packaged exe (read-only).
* **What `test_installer.py` checks on the built Setup** (it really installs, then removes everything): metadata and no administrator manifest;
  a silent install puts exactly the tested files in a path with spaces/accents; desktop + Start menu shortcuts point at the installed exe; the
  Apps & features entry; no desktop shortcut when that task is declined; the installed program passes the whole `test_exe.py` suite; Setup and the
  uninstaller refuse while the app runs; a new Setup over the old one keeps data and duplicates nothing; uninstall removes program, shortcuts and
  registry entry but keeps the data; and the REAL wizard is driven through its buttons (Next, Install, Finish -> the app window opens -> closing it
  ends the program -> uninstall), with a picture of every page in `buildexe/build/exe-test-shots/`.
* Unsigned: Windows SmartScreen shows "Windows protected your PC" once, when the Setup is first run (More info -> Run anyway); a code-signing certificate removes it.

## Desktop app backend (Telegram becomes optional)
`python run.py serve --api` starts the monitoring **and** a local API on `127.0.0.1:8765` for the desktop app - everything the
Telegram bot can do (cards, settings, status, `/comment`-style link comments) plus **Add account**, account health and a live
event stream. Reference: **[docs/API.md](docs/API.md)**. `python run.py api-token` prints the token; open
`http://127.0.0.1:8765/` for a small console page to try it by hand.

**Add account**: the app opens a fresh window of Chrome / Brave / Edge on TikTok's login page (a profile folder of its own, never
the person's browser profile). They log in; the app detects the login, lets the session settle, reads who logged in, closes the
window and saves the account - no "done" button. The same user logging in again refreshes an expired account instead of
duplicating it; a window closed early cancels and leaves nothing behind. Accounts keep a health state (`ok` / `expired` /
`unknown`) that a failed post or scan updates, so the app can offer "log in again". With no Telegram bot token / chat
linked, the service runs for the desktop app alone; with one, both work side by side.

## Reply language and proof screenshots
* **Replies are always in the language of the comment**: Polish comment → Polish reply, Spanish → Spanish, Portuguese →
  Portuguese (any other language likewise). The AI labels each comment's language; `app/lang.py` double-checks that label
  offline (Polish letters `ą ę ł ń ś ż`, Spanish `¿ ¡ ñ`, typical words) and corrects it when the evidence is strong, so a
  mislabelled comment still gets a reply in the right language. The company information and the `/ai` notes may be written
  in any language (even English): the prompt forces the *comment's* language for the reply.
* **After every reply the bot posts, it sends you a screenshot**: the comment panel cropped to the comment and the reply
  that just went out (sent as a reply to the alert card, caption with the account, the comment and the reply text).
  Only sent for successful posts; if no picture could be taken the card still shows ✅ Posted and the reply text.
* **The bot talks to you in English only** (no other interface language exists).

## When TikTok hides a reply (visible to the poster only)
Measured live on 2026-10-08: comments and replies written by the bot's helper accounts (@helper_account_7, @helper_account_2 ...) were
published by TikTok with `status: 2` - **shown only to the account that wrote them**. Every other account, a logged-out
visitor and the post's comment count never receive them. Ordinary customers' comments on the same page are public, and the
posts allow everyone to comment (`itemCommentStatus 0`, `commentSetting 0`), so this is TikTok's own moderation of those
accounts (account trust, spam signals, the page's comment filters) - the bot cannot force it, and the web sources found
(account restrictions, spam-like behaviour, creator filters; the official Business API for page owners) say the same.

What the bot does about it:
* **It notices.** TikTok's answer to the reply carries that `status`. A reply published as author-only is not celebrated:
  the card says "TikTok shows this reply ONLY to the account that posted it", you get a Telegram message, and the database
  keeps `posted_visibility = limited` (event `reply_limited`).
* **The fix is the account that answers.** A page's own account is never filtered by its own comment settings. Add it and
  let it answer first:
  ```
  python run.py add-account                       # window opens: log in as the PAGE's account, then close it
  # .env:  REPLY_ACCOUNT_FIRST=<the name add-account printed, e.g. real-account-5>
  ```
  `/accounts` shows the real order. On the server: `python run.py package --out <dir> --accounts <name> --no-db`,
  copy it over, `restore-profiles`, set `REPLY_ACCOUNT_FIRST` in the server's `.env`, restart the service
  (see `deploy/DEPLOY.md`). The official TikTok Business API (comment reply endpoints for the page's own account) is the
  long-term alternative; it needs an approved app and the owner's authorisation.

## How a customer question is answered (and why it can be trusted)
A question is recognised by the AI (and, as a free safety net when the AI has no budget, by `heuristics.looks_like_question`:
a question mark or a typical opening word in Portuguese / Spanish / English / Polish). The answer is drafted from your
`/ai_context` text, **in the customer's language, speaking as the business ("we")**. Because a free AI model can invent
facts, three guards stand between the model and the customer:
1. **The AI must say `UNKNOWN`** when the company information does not contain the answer, and may only quote numbers exactly
   as written there.
2. **A number check** (`ai.unsupported_numbers`): a reply containing a number (year, hour, price, count) that appears in none
   of the company information, the post text or the comment is rejected (12 h ↔ 24 h clock forms are understood).
3. In both cases the customer gets the standard reply instead - *"Thanks for your question! We'll confirm that for you.
   Please send us a direct message."* - in Portuguese, Spanish, English or Polish (`ai.CONFIRM_REPLY`), and the event
   `question_unanswerable` is logged.

A question gets a much larger share of the company text than a complaint (`AI_CONTEXT_CHARS_FOR_QUESTIONS`, 6000 characters -
the whole company text for most businesses), because an answer needs the facts; it is drafted with low randomness (0.1).
Measured on the real company text: history ("since 1950") and the three stores are answered correctly, opening hours and a
street address - which the text does not contain - get the standard reply, never an invention. Add the missing facts with
`/ai_context add ...` (e.g. opening hours) and those questions are answered too.

## AI budget (free OpenRouter)
A comment costs AI budget **at most once, usually never**:
1. already in the database → skipped forever;
2. first scan of a post → only comments from the last `INITIAL_LOOKBACK_DAYS` (7) are analysed, older ones are stored as `legacy`;
3. same text analysed before → analysis cache (free);
4. emoji / laughter / short praise → keyword screen (free);
5. the rest → **one request per 12 comments** (compact line format);
6. a reply draft = 1 request, and only the **relevant sections of the company context** (≤ `AI_CONTEXT_CHARS_PER_CALL`)
   are sent — a long `/ai_context` does not burn tokens.

Guards: a daily request cap (`/budget`, default 40) checked **before** every request, 20 % reserved for replies, the
account's own live counter from OpenRouter (`/key`) as a second cap, a daily-limit 429 parks the AI until UTC midnight,
a per-minute 429 only cools one model down. When the AI is unavailable, comments with strongly negative words are
still announced (keyword fallback, no drafted reply) and the drafts are filled in later automatically.
Reasoning is switched **off** on the free models (they all think by default and then run out of tokens); a model that
refuses is remembered. The model list is checked once a day against OpenRouter's live list.

## Database & disk (`data/bot.db`, SQLite, WAL)
Tables: `settings` (+ version history for Undo), `posts`, `comments`, `analysis_cache`, `approvals`, `ai_usage`,
`chat_state`, `events`, `meta`. Daily maintenance (also `python run.py maintain`, `/cleanup`):

| what | kept |
|---|---|
| harmless / analysed comments | 30 days (`RETENTION_COMMENT_DAYS`) |
| negative / question comments | 180 days (`RETENTION_NEGATIVE_DAYS`) |
| posts that left the watch set | 14 days |
| finished approvals | 180 days; pending ones go `expired` after 14 days |
| analysis cache | 45 days unused, max 20 000 rows |
| AI usage / events | 90 / 14 days |
| setting versions | last 10 per setting |

The file is kept under `DB_MAX_MB` (50): if still over, the retention windows are halved step by step. Freed pages are
returned to the OS (`incremental_vacuum` every run, full `VACUUM` weekly). Files: screenshots 7 days / 100 MB,
`network/bfp_runs` session folders 3 days, leftover throw-away browser profiles (`bfp_runs/cdp-*`) 1 day, and the
**cache folders** of the saved browser profiles when they pass `BROWSER_CACHE_MAX_MB` (cookies, localStorage,
IndexedDB and the login are never touched; a profile in use is skipped).

## Posting accounts
`python run.py add-account` logs in one more TikTok account (visible window, you log in by hand; saved as its own real
browser profile). `post_reply` tries the accounts in order (master first) and moves on only when one is captcha-walled /
logged out. `python run.py accounts` lists them. Posts are spaced `POST_MIN_GAP_SECONDS` (60) apart.

## Files
`run.py` commands · `app/service.py` threads (Telegram long-poll + browser worker) · `app/bot.py` commands/buttons ·
`app/tiktok_watch.py` profile scan · `app/analysis.py` + `app/heuristics.py` + `app/ai.py` + `app/context.py` AI pipeline ·
`app/notifier.py` cards/approvals · `app/db.py` + `app/maintenance.py` storage · `app/i18n.py` bot texts (English only) ·
`app/tiktok_monitor.py` the proven reply-posting flow (+ `post_link_comment` for /comment) · `app/links.py` TikTok link
parsing / short-link expansion · `app/accounts.py` extra accounts (registry, session health) · `app/account_manager.py` the
desktop "Add account" flow and account organising · `app/browsers.py` which browsers are installed · `app/controller.py`
the rules both Telegram and the desktop app use · `app/api.py` + `app/web/console.html` the local API and its console ·
`app/events.py` the live event bus · `app.py` + `app/desktop.py` the desktop launcher / app (no Telegram, no monitor) ·
`app/analyzer.py` the paste-a-link analysis · `app/web/desktop.html` the desktop screen · `app/logs.py` the log file ·
`app/health.py` the "is this computer ready" checks · `app/orphans.py` leftover-browser clean-up · `app/backup.py` the daily
database copy · `app/launcher.py` the launcher (`app.py` is a shim; message boxes, console attach, `--self-test`) · `app/selftest.py`
the packaged app's self-test · `buildexe/` the exe build (spec, icon, `build.py`, `test_exe.py`, `live_exe.py`, client README).

## Tests
```
python -W ignore -m unittest discover -s tests
```
639 tests, all offline (no TikTok, no Telegram); 73 of them drive the desktop screen in a REAL headless browser against a faked TikTok (`tests/test_desktop_ui.py`, about two minutes). `python tests/ui_screens.py [folder]` saves a screenshot of every screen (light, dark, narrow) to look at. Live checks used while building: real profile scan, real
OpenRouter batch + drafts, real Telegram screens and message edits, and the same suite plus a live scan on the server;
for `/comment` three real-browser dry runs (a post with no comments, a comment near the top, the oldest comment on a
25-comment post that needs scrolling) that stop just before the Post button. For the desktop **Add account** flow:
`python tests/live_add_account.py [chrome] [brave] [edge]` drives REAL browser windows through the HTTP API (a login, Cancel,
closing the window by hand) on a throw-away registry - run by hand, it is not part of the automatic suite.
`python tests/live_desktop.py [post_url] [--account NAME]` runs the whole desktop flow against REAL TikTok and the REAL AI through the real screen (read the comments, find the negative ones, AI draft, Send) with the send in **dry-run** - the reply is typed into the real comment box, a screenshot is taken, the box is emptied and nothing is sent.

## Settings (`.env`, all optional)
`POLL_INTERVAL_SECONDS` 120 (the page is read every 120 s, start to start) · `WATCH_LATEST_POSTS` 5 ·
`WATCH_PINNED_POSTS` 2 · `MAX_COMMENT_PAGES` 12 (20 comments per page) · `AI_DAILY_REQUESTS` 40 · `AI_BATCH_SIZE` 12 ·
`AI_MODELS` (comma list) · `NOTIFY_BURST` 5 per scan · `DB_MAX_MB` 50 · `POSTING_ENABLED` · `POST_MIN_GAP_SECONDS` 60 ·
`LINK_COMMENTS_PER_DAY` 10 (`/comment <link>` cap per UTC day, 0 = no limit) · retention values above.

Shared-server behaviour: `MIN_FREE_MB` 900 (no browser is launched while less memory than that is available - the
slot is skipped) · `CAPTCHA_COOLDOWN_MIN_MINUTES` 5 / `CAPTCHA_COOLDOWN_MAX_MINUTES` 60 (after TikTok shows a security
check, post opening pauses 5, 10, 20, 40, 60 min; you get one Telegram notice per episode) ·
`POLL_INTERVAL_MAX_SECONDS` (optional: let quiet periods stretch the interval).
