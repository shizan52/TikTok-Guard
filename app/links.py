"""TikTok links for /comment: find the link in a message, understand what it points to, expand the short links the
TikTok app produces (vm.tiktok.com/XXXX, tiktok.com/t/XXXX).

What a link can point to
  * a post:     https://www.tiktok.com/@user/video/7123456789012345678          (also /photo/<id>)
  * a comment:  the same URL plus ?comment_id=<cid>   or   https://m.tiktok.com/v/<id>.html?share_comment_id=<cid>
                (`cid` and `comment` are accepted too: the parameter name differs between TikTok's share screens)
  * either one behind a short link, which is expanded with one HTTP request.

Parsing is pure; expanding a short link is the only network step and it is injectable (`fetch`), so everything here
is testable offline. Only tiktok.com / tiktokv.com hosts are ever contacted - also across redirects - so a pasted
link can never make the server call somewhere else.
"""
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace

ALLOWED_DOMAINS = ("tiktok.com", "tiktokv.com")
_COMMENT_PARAMS = ("comment_id", "share_comment_id", "cid", "comment")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>»”’"
_POST_PATH = re.compile(r"/(video|photo)/(\d{6,25})")
_MOBILE_PATH = re.compile(r"/v/(\d{6,25})(?:\.html)?(?:/|$)")
_USER_PATH = re.compile(r"/@([^/?#]+)")
_REDIRECTS = (301, 302, 303, 307, 308)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")


class LinkError(Exception):
    """`key` says what went wrong: no_link | not_tiktok | no_post | unresolved (the bot turns it into a sentence)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class LinkTarget:
    video_id: str
    comment_id: str | None = None
    username: str | None = None
    media: str = "video"                 # "video" or "photo"
    source: str = ""                     # the link as it was pasted

    @property
    def kind(self) -> str:
        return "comment" if self.comment_id else "post"

    @property
    def url(self) -> str:
        """Canonical post URL (TikTok redirects '@_' to the real owner, which is what yt-dlp relies on as well)."""
        return f"https://www.tiktok.com/@{self.username or '_'}/{self.media}/{self.video_id}"


# ---------------------------------------------------------------------------------- finding and parsing
def find_links(text: str) -> list:
    """Every http(s) URL in `text`, with the punctuation a chat message hangs on the end removed."""
    return [m.group(0).rstrip(_TRAILING) for m in _URL.finditer(text or "")]


def split_message(text: str) -> tuple:
    """-> (first_url|None, the rest of the message). The rest is what the operator typed around the link: the
    comment text of `/comment <link> great video!`."""
    urls = find_links(text)
    if not urls:
        return None, (text or "").strip()
    first = urls[0]
    rest = (text or "").replace(first, " ", 1)
    return first, re.sub(r"\s+", " ", rest).strip(" |\n\t")


def host_allowed(host: str | None) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def _split(url: str):
    try:
        return urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None


def is_tiktok_url(url: str) -> bool:
    parts = _split(url)
    return bool(parts and parts.scheme in ("http", "https") and host_allowed(parts.hostname))


def needs_expansion(url: str) -> bool:
    """A short link (vm.tiktok.com/XXXX, vt.tiktok.com/XXXX, tiktok.com/t/XXXX): the post id is not in the URL itself."""
    parts = _split(url)
    if not parts or not host_allowed(parts.hostname):
        return False
    host = (parts.hostname or "").lower()
    return host.split(".")[0] in ("vm", "vt") or parts.path.startswith("/t/")


def parse_link(url: str) -> LinkTarget | None:
    """The post (and comment) a TikTok URL points to, or None when the URL itself does not say (a short link, a
    profile, the home page ...). Never touches the network."""
    parts = _split(url)
    if not parts or parts.scheme not in ("http", "https") or not host_allowed(parts.hostname):
        return None
    media, vid = "video", None
    m = _POST_PATH.search(parts.path)
    if m:
        media, vid = m.group(1), m.group(2)
    else:
        m = _MOBILE_PATH.search(parts.path)
        if m:
            vid = m.group(1)
    if not vid:
        return None
    user = _USER_PATH.search(parts.path)
    username = user.group(1) if user and user.group(1) not in ("", "_") else None
    query = urllib.parse.parse_qs(parts.query)
    comment = next((v[0] for k in _COMMENT_PARAMS for v in [query.get(k) or []] if v and v[0].isdigit()), None)
    return LinkTarget(video_id=vid, comment_id=comment, username=username, media=media, source=url)


# ---------------------------------------------------------------------------------- expanding short links
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):          # hand the 3xx back to us instead of following it blindly
        return None


def fetch_once(url: str, timeout: float = 10.0) -> tuple:
    """-> (http status, Location header or None) for ONE request, redirects not followed. (0, None) on a network error."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html,*/*"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Location")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location") if exc.headers else None
    except Exception:  # noqa: BLE001 - offline, DNS, timeout: the caller reports "could not open the link"
        return 0, None


def expand(url: str, *, fetch=fetch_once, max_hops: int = 5) -> str | None:
    """Follow a short link's redirects (every hop must stay on a TikTok host) until the URL names a post.
    The first hop that already contains the post id wins, so the page itself is never downloaded."""
    current = url
    for _ in range(max_hops + 1):
        if not is_tiktok_url(current):
            return None
        if parse_link(current):
            return current
        status, location = fetch(current)
        if status in _REDIRECTS and location:
            current = urllib.parse.urljoin(current, location)
            continue
        return current if status == 200 and parse_link(current) else None
    return None


def resolve(url: str, *, fetch=fetch_once) -> LinkTarget:
    """The post / comment a pasted link points to. Raises LinkError(key) with a reason the operator can act on."""
    url = (url or "").strip()
    if not url:
        raise LinkError("no_link")
    if not is_tiktok_url(url):
        raise LinkError("not_tiktok")
    target = parse_link(url)
    if target:
        return replace(target, source=url)
    if not needs_expansion(url):
        raise LinkError("no_post")                       # a profile, the home page, a hashtag ...
    final = expand(url, fetch=fetch)
    if not final:
        raise LinkError("unresolved")
    target = parse_link(final)
    if not target:
        raise LinkError("no_post")
    return replace(target, source=url)
