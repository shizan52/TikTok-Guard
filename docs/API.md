# Desktop backend API (v1)

The backend that the desktop app talks to. It is the same process that watches TikTok - `python run.py serve --api` -
and it exposes **everything the Telegram bot can do** as JSON, plus a live event stream. Telegram becomes optional: with no
bot token / chat linked the service runs for the desktop app alone.

```
python run.py serve --api            start the backend (or set API_ENABLED=true in .env)
python run.py api-token              print the token every call needs
http://127.0.0.1:8765/               a small console page to try everything by hand (add  #token=<token>  to the URL once)
```

| setting (.env) | default | meaning |
|---|---|---|
| `API_ENABLED` | false | start the API together with `serve` (`serve --api` does the same for one run) |
| `API_HOST` / `API_PORT` | 127.0.0.1 / 8765 | where it listens. Keep it on 127.0.0.1: it controls TikTok accounts |
| `API_TOKEN` | random | the token; when empty a random one is kept in `data/api_token.txt` |
| `API_ALLOWED_ORIGINS` | - | extra browser origins allowed to call it (comma list). `localhost`, `127.0.0.1`, `null`, `tauri://localhost`, `app://*`, `file://` are always allowed |
| `BROWSER_PREFERENCE` | auto | browser of the "Add account" window: `auto` \| `chrome` \| `brave` \| `edge` |
| `ACCOUNT_LOGIN_TIMEOUT_S` | 900 | how long a login window waits for the person |
| `ACCOUNT_SETTLE_S` | 8 | after the login is seen, how long the session settles before the window is closed |
| `LINK_COMMENTS_PER_DAY` | 10 | cap for comments the app posts on other posts (0 = no limit) |

## Conventions
* **Auth**: `Authorization: Bearer <token>` on everything except `GET /api/ping` and the console page. `GET` requests may use
  `?token=` instead (the browser's `EventSource` cannot set headers); a URL token never authorises a change.
* **Host / Origin**: the `Host` header must be this machine, and a browser `Origin` must be local (see `API_ALLOWED_ORIGINS`).
  A web page on the internet can never call it; neither can another program take over the port.
* **Bodies** are JSON objects (max 1 MB). **Errors** are always
  `{"error": {"code": "...", "message": "human readable", ...extra}}` with an HTTP status that matches:

| code | HTTP | meaning |
|---|---|---|
| `unauthorized` / `forbidden` | 401 / 403 | no/wrong token; foreign Host or Origin |
| `bad_request` | 400 | invalid input (nothing was changed) |
| `not_found` | 404 | no such endpoint / card / account / screenshot |
| `already_handled`, `busy`, `master`, `no_profile`, `no_reply`, `link_already_open` | 409 | the thing is not in a state for this (see `status`, `existing_id` in the error) |
| `link_not_tiktok`, `link_no_post`, `link_unresolved`, `link_no_link` | 422 | the link cannot be used (`message` says what to do) |
| `no_browser` | 422 | the asked browser is not installed |
| `cap_reached`, `regen_limit`, `ai_budget` | 429 | a limit was hit (`limit` in the error for the daily cap) |
| `ai_failed` | 502 | the AI could not write the text |
| `low_memory`, `busy` (timeout) | 503 | try again in a moment |

## Pages
`GET /` and `GET /app` serve the desktop screen (`app/web/desktop.html`), `GET /console` the small hand-testing console. Both open
with `#token=<token>` in the address (the page keeps it for the session and removes it from the address bar). `python app.py` builds that
address and opens it in its own window.

## Live events - the heart of a responsive UI
`GET /api/events/stream` (Server-Sent Events). Each event: `id`, `ts`, `type`, `level` (`info|warn|error`), `data`.
Start from now, or send `Last-Event-ID` / `?since=N` to replay what was missed (the last 1000 are kept).
Without SSE: `GET /api/events` -> `{events: [], last_id}` (where the stream is now), then
`GET /api/events?since=N&timeout=25` (long-poll, waits for news).

| type | when | `data` |
|---|---|---|
| `approval.created` | a card appears (negative comment, question, link comment) | `id, kind, status` |
| `approval.updated` | any change of a card (text, status, account ...) | `id, status, fields[]` |
| `post.stage` | the browser moved to the next step of a reply that is being sent | `id, text` ("Opening the post ...", "Typing the reply ...") |
| `proof` | a post succeeded and its screenshot exists | `approval_id, file` -> `GET /api/screenshots/<file>` |
| `account.flow` | progress of "Add account" | `flow` (see below) |
| `accounts.changed` | the account list changed - refetch `GET /api/accounts` | - |
| `account.checked` | result of a session check | `name, ok, session, username?, unconfirmed?, error?` |
| `account.session` | a post/scan found an account logged in / out | `name, session` (`ok`\|`expired`) |
| `scan.started` / `scan.finished` | a monitoring round | `opened, new` or `error` |
| `notice` | something the operator should read (captcha wall, AI budget used up, scan problem ...) | `key, text` |
| `setting.changed` | a setting was changed (here or in Telegram) | `key` |
| `cleanup.done` | disk clean-up finished | `freed_mb, db_before_mb, db_after_mb` |
| `log` | an entry of the audit trail | `kind, detail, level` |

## Accounts - the "Add account" workflow
```
POST /api/accounts/add   {"browser": "auto|chrome|brave|edge", "label": "optional name"}   -> 202 + flow
```
A **fresh window** of the person's browser opens on TikTok's login page, on a profile folder of the app's own (never their
personal browser profile). They log in as they always do. Nothing automated touches that window meanwhile.
The app **detects the login by itself**, lets the session settle, reads who logged in, **closes the window itself** and saves
the account. No "done" button.

`flow` = `{id, state, message, finished, outcome, result, waiting_for_you, browser{kind,name}, replace, started_at, updated_at}`

| state | meaning | show the user |
|---|---|---|
| `starting` | opening the window | spinner |
| `window_open` (`waiting_for_you: true`) | window is open, waiting for the login | "Log in in the window that just opened" + **Cancel** |
| `login_detected` | the session cookie appeared | "Login detected..." |
| `verifying` | reading who logged in | spinner |
| `saving` | closing the window, saving | spinner |
| `done` / `failed` | finished - read `outcome` | `message` |

`outcome` when finished: `added` (new account) · `refreshed` (the same user logged in again: that account's expired session is
replaced, its name/position/settings kept) · `duplicate` (the user is already added and works, or is the page's own account -
nothing saved; `result.duplicate_of`) · `cancelled` (Cancel pressed, or the window was closed before logging in - nothing left
behind) · `timeout` · `failed`. `result.account` is the saved account; `result.account.identity_pending` is true when who
logged in could not be read yet (its `session` is then `unknown` until the automatic background check settles it).

| call | |
|---|---|
| `GET /api/accounts/add` | the current flow (or the last finished one) - for a screen that opens mid-way. `{"flow": null}` when none |
| `POST /api/accounts/add/cancel` | close the window, save nothing |
| `GET /api/browsers` | installed browsers `[{kind, name, path, configured}]` for a picker |
| `GET /api/accounts` | `{accounts, chain, first, flow, browsers}` - each account: `name, label, username, nickname, region, enabled, is_master, session, session_checked_at, added, browser_kind, position, preferred, folder_exists, resting_seconds` (how much longer TikTok's refusal of this account is expected to last - 0 when it is not resting) |
| `GET /api/accounts/{name}` | one account |
| `PATCH /api/accounts/{name}` | `{label?, enabled?}` |
| `POST /api/accounts/{name}/relogin` | "Log in again" - same window flow, `replace` = that account; the same user refreshes it, a different user is added as a new account |
| `POST /api/accounts/{name}/check` | is it still logged in? Blocks up to ~1 min. Result `session`: `ok` (and who) \| `expired` (TikTok's session cookie is gone) \| `unknown` + `unconfirmed: true` (TikTok served its guest page; nothing is overwritten). Never claims `expired` without proof |
| `POST /api/accounts/check-all` | 202; every account is checked in the background, results arrive as `account.checked` events |
| `PUT /api/accounts/order` | `{names: [...]}` posting order of the registered accounts |
| `PUT /api/accounts/first` | `{name}` or `{name: null}` - the account that answers first (usually the page's own) |
| `DELETE /api/accounts/{name}` | forget it and delete its saved browser folder |

`session` of an account: `ok` · `expired` (offer **Log in again**; also set by a failed post) · `unknown` (not checked / could not
confirm). The page's own account (`is_master`) is configured in `.env` and cannot be removed, renamed or re-added here, but it
can be checked.

## Cards (alerts and link comments)
A card = an *approval*: either an **alert** (a negative comment or question found on the page, with a suggested reply) or a
**link comment** (the operator asked for a comment on a TikTok link). Nothing is ever posted without `.../post`.

| call | |
|---|---|
| `GET /api/approvals?status=pending,post_failed&kind=alert\|link&limit=50` | the cards. `status=all` = every status (the desktop app's Replies page) |
| `GET /api/approvals/{id}` | one card: `id, kind (alert\|link_post\|link_reply), status, reply, reply_length, regen_count, error, interrupted (the app was closed while it was being sent - it may or may not be on TikTok; a person decides), checking (a check on TikTok is running), checked (`absent`\|`maybe`\|null - what the last check found), check{outcome, message}\|null, can_check (the Check on TikTok button applies), replies_before is kept internally, progress{text, seconds} (only while `posting`: what the browser is doing now), posted_by, visibility, account, account_label, target{video_id, video_url, comment_id, username}, comment{...} and post{...} (alerts), proof_file?, can{post, edit, regenerate, ignore, choose_account}` |
| `POST /api/approvals/{id}/reply` | `{text}` - the operator's own words (shortened to TikTok's 150; `shortened` tells) |
| `POST /api/approvals/{id}/regenerate` | `{brief?}` - a new AI text. Alerts: from the comment (max 3). Link comments: `brief` is required ("thank them and invite them to the store") |
| `POST /api/approvals/{id}/verify` | **Check on TikTok** - was this reply posted after all (the app was closed mid-send, TikTok never answered)? 202 + the card with `checking: true`; the post is read again until the comment is seen (a background job; `activity.checking` shows its step). Verdict: our text among TikTok's preview of the first replies -> the card becomes `posted`; more replies than when it was sent -> `checked: "maybe"`; none -> `checked: "absent"` ("not posted, safe to send again"); the post could not be read -> nothing changes, `check.outcome: "error"`. Only for a `post_failed` reply card (400 otherwise) |
| `POST /api/approvals/{id}/post` | send it: status -> `posting` (queued) and later `posted` / `post_failed` (watch `approval.updated`); `approved` when `POSTING_ENABLED=false` |
| `POST /api/approvals/{id}/ignore` | close it |
| `POST /api/approvals/{id}/account` | `{account}` or `{account: null}` (automatic) - link comments only |
| `POST /api/links/preview` | `{link}` -> `{kind: post\|comment, video_id, comment_id, username, url}` (short `vm.tiktok.com` links are expanded) |
| `POST /api/comments/link` | `{link, text?, account?}` -> 201 card. One open card per target (`link_already_open` + `existing_id`) |
| `GET /api/screenshots/{file}` | the proof screenshot (PNG), only from the screenshots folder |

Statuses: `pending` -> `posting` -> `posted` · `post_failed` (can be posted again) · `rejected` · `approved` (posting is off) ·
`expired`. Actions on a card that is not open answer `already_handled`.

## Analyse a post, reply to its comments (the desktop app's home page)
Nothing here runs by itself - a person pastes a link and asks. Available in `python app.py` and in `serve --api`; in the latter the
analysis waits its turn on the same browser thread as the watcher's scans (`queued`).

| call | |
|---|---|
| `POST /api/analyze` | `{url, account?}` - any TikTok post link (short links are expanded; a link to a comment is accepted too and analyses its post). 202 + a **job**. One job at a time (`busy` 409 with the running `job`). `account` = read with exactly that account, otherwise the first two accounts of the normal order are tried |
| `GET /api/analyze` | `{job}` - the running job, else the last finished one, else `null` (for a screen that opens mid-way) |
| `GET /api/analyze/{id}` | one job |
| `POST /api/analyze/{id}/cancel` | stop it; the job ends as `cancelled` |
| `GET /api/analysis/{video_id}` | the result: `{post{video_id, url, description, comment_count, last_checked_at, author}, counts{...}, comments[]}` - each comment: `cid, text, username, likes, created_ts, replies, status (negative\|question\|ok\|unchecked), severity, category, reason, lang, judged_by, card{id, status, reply, account, posted_by}\|null`. `404 not_found` if the post was never analysed |
| `GET /api/posts/recent?limit=8` | `{posts[]}` the posts analysed lately (`video_id, url, description, author, comment_count, negatives, questions, stored, last_checked_at`) |
| `POST /api/comments/{video_id}/{cid}/reply` | `{account?}` - a reply **card** for that comment (201; `kind: link_reply`). One open card per comment: asking again returns it (200, `existing: true`). Then use the card calls above: `/regenerate` (AI draft; `brief` optional here), `/reply` (own text), `/account`, `/post`, `/ignore`. **Nothing is sent before `/post`** |
| `GET /api/dashboard` | what the home screen shows at a glance: `posting_enabled, ai{...}, accounts{total, usable, ready, need_login, unchecked}, replies{today, daily_limit, counted_today}, waiting, recent_posts[], analysis` |

A **job**: `{id, url, video_id, state, message, finished, outcome, error{code, message}|null, note, progress{read, total}, account,
used_account, started_at, updated_at}`. `state`: `queued` (waiting for the browser - the service has ONE browser thread) -> `reading`
("Reading comments... 60 of 152") -> `analysing` (free rules first, the AI only for the rest, once per comment) -> `done` /
`failed` / `cancelled`. `error.code`: `captcha` (TikTok showed a security check - wait a few minutes or read with another
account), `blocked` (TikTok would not open the page for any account tried - it limits profile-page loads per account; wait a few
minutes), `read_failed`, `busy`, `internal`. While `reading`, `message` is the browser's current step ("Opening the post with @name...",
"Reading comments... 14 of 26"). Poll `GET /api/analyze/{id}` (about every 1-2 s) while `finished` is false; the
`message` is already written for a person. `note` carries a hint such as "the account used is not logged in". A post is opened
through its owner's profile and a click on its tile (TikTok blocks repeated direct loads of one post URL); comments stored earlier
keep their verdict, so analysing the same post again only pays for new comments.

## What is happening, is the computer ready
| call | |
|---|---|
| `GET /api/activity` | `{busy, sending[{id, username, account, text, seconds}], checking[{id, text, seconds}], reading{id, message, seconds, progress, url}\|null}` - what is in flight right now (a reply on its way, a reply being checked on TikTok, a post being read). Cheap; the desktop screen polls it every 2 s **only while `busy`** |
| `GET /api/health` | `{status: ok\|warn\|fail, items[{id, title, status, detail, fix}]}` - is this computer ready: Python, data folder, browser engine, a web browser, accounts, AI key, company information, disk, memory. `?net=1` also checks that tiktok.com can be reached (up to 2.5 s) |
| `GET /api/diagnostics` | `{app{version, python, platform, data_dir, log_file, uptime_seconds}, health, problems[{at, level, kind, detail}], log[], backups[{name, day, kb}]}` - what to send when something is wrong: the checks (with the network one), the recent warnings/errors and the last lines of `data/logs/desktop.log` (tokens and keys masked) |

## Status, posts, settings
| call | |
|---|---|
| `GET /api/ping` | no token: `{ok, app, api}` - is the backend there? |
| `GET /api/status` | monitoring (last scan, running, error), AI budget, posts watched, waiting cards, backlog, database size, posting on/off, link comments today/limit, telegram on/off, recent problems |
| `GET /api/posts` | the watched posts |
| `GET /api/settings` | `ai_instructions{value, is_default, can_undo, max_chars}`, `company_context{value, chars, sections, can_undo, max_chars}`, `notify_questions`, `watch{latest, pinned}`, `ai_budget{...}`, `posting_enabled`, `link_comments_per_day`, `read_limit`, `brand_name`, `ai_key{set, hint}` (only the last characters of the key are ever returned), `poll_interval_seconds` |
| `PUT /api/settings` | partial: `ai_instructions`, `company_context` (+ `company_context_append: true`), `notify_questions` (bool), `watch_latest`, `watch_pinned` (0-10), `ai_daily_requests` (0-1000), `posting_enabled` (bool - switches posting on/off at once and survives a restart), `link_comments_per_day` (0-500, 0 = no limit), `read_limit` (20-1000 comments read per analysed post), `brand_name`, `openrouter_api_key` (empty string = go back to the key in `.env`). **All or nothing**: one invalid value changes nothing |
| `POST /api/settings/{ai-instructions\|company-context}/reset` · `/undo` | default / previous version (a reset is itself undoable) |
| `POST /api/ai/test` | `{text}` -> `{reply}` what a sample comment would get (1 AI request) |
| `POST /api/backfill` | `{days}` - also analyse older stored comments |
| `POST /api/maintenance/cleanup` | 202; result as a `cleanup.done` event. With `?wait=1` it runs now and answers `{freed_mb, db_before_mb, db_after_mb}` |

## A sensible client
1. Start `python run.py serve --api`; read the token (`python run.py api-token`, or `API_TOKEN` you set); poll `GET /api/ping`.
2. Open the SSE stream and keep it open; on `approval.*`, `accounts.changed`, `account.*` refetch the matching list.
3. "Add account" button -> `POST /api/accounts/add`; render `account.flow` events; the window itself is the login UI.
4. Cards screen = `GET /api/approvals`; buttons map 1:1 to the `/approvals/{id}/...` calls and to `can{}`.

## Notes for operators
* The Telegram bot keeps working unchanged next to the app (set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`); a card changed in the
  app is refreshed in Telegram. Only ONE process may poll a Telegram bot - never run a second backend on the same token.
* An account added in the app records which browser made it and is always driven by that browser later.
* Moving accounts to a server: unchanged - `python run.py package --accounts <name> --no-db`, see `deploy/DEPLOY.md`.
