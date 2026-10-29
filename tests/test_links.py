"""TikTok link handling for /comment: every link format, the safety rules, short-link expansion - all offline."""
import unittest

from _helpers import ROOT  # noqa: F401  (puts the project on sys.path)

from app import links

VID = "7695930429047688468"
CID = "7234567890123456789"


class TestParse(unittest.TestCase):
    def test_a_post_link(self):
        t = links.resolve(f"https://www.tiktok.com/@examplebrand/video/{VID}")
        self.assertEqual((t.kind, t.video_id, t.comment_id, t.username, t.media),
                         ("post", VID, None, "examplebrand", "video"))

    def test_a_comment_link_in_each_of_the_known_shapes(self):
        for url in (f"https://www.tiktok.com/@shop/video/{VID}?comment_id={CID}&lang=en",
                    f"https://www.tiktok.com/@shop/video/{VID}?share_comment_id={CID}",
                    f"https://m.tiktok.com/v/{VID}.html?share_comment_id={CID}",
                    f"https://www.tiktok.com/@shop/video/{VID}?cid={CID}",
                    f"https://www.tiktok.com/@shop/video/{VID}?comment={CID}"):
            t = links.resolve(url)
            self.assertEqual((t.kind, t.video_id, t.comment_id), ("comment", VID, CID), url)

    def test_a_comment_id_that_is_not_a_number_is_ignored(self):
        t = links.resolve(f"https://www.tiktok.com/@shop/video/{VID}?comment=abc&comment_id=")
        self.assertEqual((t.kind, t.comment_id), ("post", None))

    def test_photo_posts_and_the_tiktokv_share_path(self):
        self.assertEqual(links.resolve(f"https://www.tiktok.com/@u/photo/{VID}").media, "photo")
        t = links.resolve(f"https://www.tiktokv.com/share/video/{VID}/?x=1")
        self.assertEqual((t.video_id, t.username), (VID, None))

    def test_the_canonical_url_is_clean_and_works_without_a_username(self):
        self.assertEqual(links.resolve(f"https://www.tiktok.com/@shop/video/{VID}?is_from_webapp=1&q=x").url,
                         f"https://www.tiktok.com/@shop/video/{VID}")
        self.assertEqual(links.resolve(f"https://m.tiktok.com/v/{VID}.html").url,
                         f"https://www.tiktok.com/@_/video/{VID}", "TikTok redirects @_ to the real owner")

    def test_the_pasted_text_is_remembered_as_the_source(self):
        url = f"https://www.tiktok.com/@shop/video/{VID}?x=1"
        self.assertEqual(links.resolve(url).source, url)

    def test_pages_that_are_not_a_post_are_refused(self):
        for url in ("https://www.tiktok.com/@shop", "https://www.tiktok.com/", "https://www.tiktok.com/tag/offers",
                    "https://www.tiktok.com/@shop/video/12"):                 # an id that short is not a post id
            with self.assertRaises(links.LinkError) as cm:
                links.resolve(url)
            self.assertEqual(cm.exception.key, "no_post", url)


class TestFindingLinks(unittest.TestCase):
    def test_links_are_found_and_chat_punctuation_is_dropped(self):
        self.assertEqual(links.find_links("look (https://vm.tiktok.com/ZMabc123/), and https://www.tiktok.com/@a/video/12345678!"),
                         ["https://vm.tiktok.com/ZMabc123/", "https://www.tiktok.com/@a/video/12345678"])
        self.assertEqual(links.find_links("no link here"), [])

    def test_the_text_around_the_link_becomes_the_comment(self):
        self.assertEqual(links.split_message("https://vm.tiktok.com/ZMabc/ Great video!"),
                         ("https://vm.tiktok.com/ZMabc/", "Great video!"))
        self.assertEqual(links.split_message("Great video! https://vm.tiktok.com/ZMabc/"),
                         ("https://vm.tiktok.com/ZMabc/", "Great video!"))
        self.assertEqual(links.split_message("https://vm.tiktok.com/ZMabc/ | thanks"),
                         ("https://vm.tiktok.com/ZMabc/", "thanks"))
        self.assertEqual(links.split_message("  just words "), (None, "just words"))


class TestSafety(unittest.TestCase):
    """A pasted link must never make the server call (or trust) anything that is not TikTok."""

    def refused(self, url, key="not_tiktok"):
        calls = []
        with self.assertRaises(links.LinkError) as cm:
            links.resolve(url, fetch=lambda u: calls.append(u) or (200, None))
        self.assertEqual(cm.exception.key, key, url)
        self.assertEqual(calls, [], f"{url} must not be fetched")

    def test_other_hosts_are_refused_without_a_request(self):
        for url in (f"https://evil.example.com/tiktok.com/video/{VID}",
                    f"https://tiktok.com@evil.example.com/video/{VID}",       # user-info trick: the host is evil.example.com
                    f"https://nottiktok.com/@u/video/{VID}",
                    f"https://tiktok.com.evil.io/@u/video/{VID}",
                    f"ftp://www.tiktok.com/@u/video/{VID}",
                    "javascript:alert(1)", "http://127.0.0.1:8000/t/abc", "http://localhost/t/abc"):
            self.refused(url)

    def test_empty_input(self):
        with self.assertRaises(links.LinkError) as cm:
            links.resolve("   ")
        self.assertEqual(cm.exception.key, "no_link")

    def test_a_subdomain_of_tiktok_is_fine(self):
        self.assertTrue(links.host_allowed("vm.tiktok.com"))
        self.assertTrue(links.host_allowed("WWW.TikTok.com"))
        self.assertFalse(links.host_allowed("tiktok.com.evil.io"))
        self.assertFalse(links.host_allowed(None))


class TestShortLinks(unittest.TestCase):
    FINAL = f"https://www.tiktok.com/@shop/video/{VID}?_r=1&share_comment_id={CID}"

    def fetcher(self, table):
        calls = []

        def fetch(url):
            calls.append(url)
            return table.get(url, (404, None))
        fetch.calls = calls
        return fetch

    def test_the_short_link_forms_are_recognised(self):
        for url in ("https://vm.tiktok.com/ZMabc123/", "https://vt.tiktok.com/ZSabc123/", "https://www.tiktok.com/t/ZTabc123/"):
            self.assertTrue(links.needs_expansion(url), url)
        for url in (f"https://www.tiktok.com/@u/video/{VID}", "https://www.tiktok.com/@u"):
            self.assertFalse(links.needs_expansion(url), url)

    def test_a_short_link_is_expanded_to_the_post_and_the_comment(self):
        fetch = self.fetcher({"https://vm.tiktok.com/ZMabc/": (301, self.FINAL)})
        t = links.resolve("https://vm.tiktok.com/ZMabc/", fetch=fetch)
        self.assertEqual((t.kind, t.video_id, t.comment_id, t.username), ("comment", VID, CID, "shop"))
        self.assertEqual(t.source, "https://vm.tiktok.com/ZMabc/")
        self.assertEqual(fetch.calls, ["https://vm.tiktok.com/ZMabc/"], "the post page itself is never downloaded")

    def test_several_hops_and_relative_locations(self):
        fetch = self.fetcher({"https://vt.tiktok.com/ZSx/": (302, "https://www.tiktok.com/t/ZTy/"),
                              "https://www.tiktok.com/t/ZTy/": (301, f"/@shop/video/{VID}")})
        self.assertEqual(links.resolve("https://vt.tiktok.com/ZSx/", fetch=fetch).video_id, VID)
        self.assertEqual(len(fetch.calls), 2)

    def test_a_redirect_to_another_host_is_never_followed(self):
        fetch = self.fetcher({"https://vm.tiktok.com/ZMabc/": (302, f"https://evil.example.com/@u/video/{VID}")})
        with self.assertRaises(links.LinkError) as cm:
            links.resolve("https://vm.tiktok.com/ZMabc/", fetch=fetch)
        self.assertEqual(cm.exception.key, "unresolved")
        self.assertEqual(fetch.calls, ["https://vm.tiktok.com/ZMabc/"], "the evil host was not contacted")

    def test_an_endless_redirect_loop_gives_up(self):
        fetch = self.fetcher({"https://vm.tiktok.com/a/": (302, "https://vm.tiktok.com/b/"),
                              "https://vm.tiktok.com/b/": (302, "https://vm.tiktok.com/a/")})
        with self.assertRaises(links.LinkError) as cm:
            links.resolve("https://vm.tiktok.com/a/", fetch=fetch)
        self.assertEqual(cm.exception.key, "unresolved")
        self.assertLessEqual(len(fetch.calls), 6)

    def test_a_dead_or_unreachable_short_link_is_reported_not_crashed(self):
        for answer in ((404, None), (0, None), (500, None), (302, None)):
            fetch = self.fetcher({"https://vm.tiktok.com/dead/": answer})
            with self.assertRaises(links.LinkError) as cm:
                links.resolve("https://vm.tiktok.com/dead/", fetch=fetch)
            self.assertEqual(cm.exception.key, "unresolved", answer)

    def test_a_short_link_that_lands_on_a_profile_is_not_a_post(self):
        fetch = self.fetcher({"https://vm.tiktok.com/p/": (301, "https://www.tiktok.com/@shop")})
        with self.assertRaises(links.LinkError) as cm:
            links.resolve("https://vm.tiktok.com/p/", fetch=fetch)
        self.assertIn(cm.exception.key, ("unresolved", "no_post"))

    def test_a_full_link_needs_no_request_at_all(self):
        fetch = self.fetcher({})
        links.resolve(f"https://www.tiktok.com/@u/video/{VID}", fetch=fetch)
        self.assertEqual(fetch.calls, [])


if __name__ == "__main__":
    unittest.main()
