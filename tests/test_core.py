"""DB, heuristics, context selection, AI budget & parsing, analysis pipeline."""
import time
import unittest

from _helpers import comment, make_ai, tmp_db  # noqa: F401  (also sets sys.path)

import config
from app import analysis, context as ctx, heuristics
from app.ai import AI, CONFIRM_REPLY, DEFAULT_INSTRUCTIONS, parse_classification, sanitize_reply
from app.db import text_hash, utc_day


class TestDB(unittest.TestCase):
    def test_settings_versions_and_undo(self):
        db = tmp_db()
        self.assertTrue(db.set_setting("k", "one"))
        self.assertFalse(db.set_setting("k", "one"), "same value is a no-op")
        db.set_setting("k", "two")
        db.set_setting("k", "three")
        self.assertEqual(db.get_setting("k"), "three")
        self.assertTrue(db.undo_setting("k"))
        self.assertEqual(db.get_setting("k"), "two")
        self.assertTrue(db.undo_setting("k"))
        self.assertEqual(db.get_setting("k"), "one")
        self.assertFalse(db.undo_setting("k"), "nothing older")

    def test_version_history_is_capped(self):
        db = tmp_db()
        for i in range(30):
            db.set_setting("k", f"v{i}", keep_versions=10)
        self.assertEqual(db.setting_version_count("k"), 10)

    def test_delete_setting_is_undoable(self):
        db = tmp_db()
        db.set_setting("k", "x")
        db.delete_setting("k")
        self.assertIsNone(db.get_setting("k"))
        self.assertTrue(db.undo_setting("k"))
        self.assertEqual(db.get_setting("k"), "x")

    def test_comments_inserted_once(self):
        db = tmp_db()
        c = comment("a", "oi")
        self.assertEqual(len(db.insert_new_comments("v1", [c])), 1)
        self.assertEqual(len(db.insert_new_comments("v1", [c])), 0, "a known comment is never inserted again")

    def test_legacy_cutoff_and_empty_text(self):
        db = tmp_db()
        old = comment("old", "velho", create_time=int(time.time()) - 30 * 86400)
        new = comment("new", "novo")
        empty = comment("e", "   ")
        fresh = db.insert_new_comments("v1", [old, new, empty], legacy_before_ts=int(time.time()) - 7 * 86400)
        by = {f["cid"]: f["status"] for f in fresh}
        self.assertEqual(by, {"old": "legacy", "new": "new", "e": "legacy" if False else by["e"]})
        self.assertEqual(by["old"], "legacy")
        self.assertEqual(by["new"], "new")
        self.assertEqual(by["e"], "skipped")

    def test_one_open_approval_per_comment(self):
        db = tmp_db()
        rec = {"id": "a1", "cid": "c1", "video_id": "v1", "status": "pending"}
        self.assertEqual(db.create_approval(rec), "a1")
        self.assertIsNone(db.create_approval({**rec, "id": "a2"}), "second open approval for the same comment")
        db.update_approval("a1", status="rejected")
        self.assertEqual(db.create_approval({**rec, "id": "a3"}), "a3", "allowed again once the first is closed")

    def test_usage_accounting(self):
        db = tmp_db()
        db.add_usage("m1", "classify", ok=True, prompt_tokens=100, completion_tokens=30)
        db.add_usage("m1", "classify", ok=False)
        db.add_usage("m2", "draft", ok=True, prompt_tokens=10, completion_tokens=5)
        u = db.usage_for_day()
        self.assertEqual((u["calls"], u["ok_calls"], u["prompt_tokens"], u["completion_tokens"]), (3, 2, 110, 35))
        self.assertEqual(u["by_kind"], {"classify": 2, "draft": 1})

    def test_chat_state_roundtrip(self):
        db = tmp_db()
        db.set_chat_state(5, "await_ai", {"x": 1})
        st = db.get_chat_state(5)
        self.assertEqual((st["mode"], st["payload"]), ("await_ai", {"x": 1}))
        db.clear_chat_state(5)
        self.assertIsNone(db.get_chat_state(5))

    def test_text_hash_normalises(self):
        self.assertEqual(text_hash("Top!  "), text_hash("  top!"))
        self.assertNotEqual(text_hash("top"), text_hash("tops"))


class TestHeuristics(unittest.TestCase):
    def test_harmless_is_settled_for_free(self):
        for t in ("😍😍😍", "kkkkkk", "@maria @joao", "Amei! 😍", "Top demais", "lindo", "haha", "👏👏"):
            self.assertIsNotNone(heuristics.screen(t), t)

    def test_unclear_goes_to_ai(self):
        for t in ("Cadê o endereço?", "qual o valor?", "Isso é golpe!", "nunca mais compro aqui",
                  "O produto chegou quebrado e ninguém responde"):
            self.assertIsNone(heuristics.screen(t), t)

    def test_negative_wording_scores_with_accents_ignored(self):
        self.assertGreaterEqual(heuristics.negative_score("PÉSSIMO atendimento, é golpe"), 2)
        self.assertEqual(heuristics.negative_score("muito obrigado"), 0)

    def test_fallback_flags_strong_negatives_and_questions_only(self):
        fb = heuristics.fallback_classify("é golpe, fraude!")
        self.assertEqual((fb["sentiment"], fb["severity"], fb["category"]), ("NEG", 3, "scam"))
        q = heuristics.fallback_classify("bom dia, qual o horário?")       # a question still reaches a human
        self.assertEqual((q["sentiment"], q["severity"], q["source"]), ("Q", 0, "fallback"))
        self.assertIsNone(heuristics.fallback_classify("Que lindo esse shopping"), "praise is left alone")
        self.assertEqual(heuristics.fallback_classify("meu pedido não chegou")["category"], "delivery")
        self.assertEqual(heuristics.fallback_classify("não recebi, onde está meu pedido?")["sentiment"], "NEG",
                         "a complaint phrased as a question stays a complaint")

    def test_questions_are_recognised_in_four_languages_and_praise_is_not(self):
        asking = ["Qual o valor", "Que horário abre?", "Faz envio para o Brasil", "o estacionamento é gratuito?",
                  "Bom dia, qual o horário", "Cuánto cuesta el envío", "Hola, donde queda", "Czy wysyłacie do Polski",
                  "Jak dojechać", "Where is the store", "Oi, tem estacionamento?"]
        chatter = ["Amei, como sempre!", "Que lindo esse Chopping", "Maravilhoso 🥰", "Muito bom", "gratidão 🙏",
                   "Eu amo comprar meus perfumes nesse shopping", "Que maravilha!"]
        self.assertEqual([t for t in asking if not heuristics.looks_like_question(t)], [])
        self.assertEqual([t for t in chatter if heuristics.looks_like_question(t)], [])

    def test_a_short_question_is_not_swallowed_by_the_praise_shortcut(self):
        self.assertIsNone(heuristics.screen("Qual o melhor preço"), "contains 'melhor' but asks - goes to the AI")
        self.assertEqual(heuristics.screen("Muito bom, parabéns")["reason"], "short praise")


class TestContext(unittest.TestCase):
    TEXT = ("Example Store is a mall in Central City with 400 stores.\n\n"
            "Opening hours: Monday to Saturday 8am to 6pm, Sunday 8am to 2pm.\n\n"
            "Shipping: we do not ship; purchases are picked up in store or via a freight agent.\n\n"
            "Returns: unopened items can be exchanged within 7 days with the receipt.\n\n" + ("filler " * 200))

    def test_short_context_is_sent_whole(self):
        self.assertEqual(ctx.select_context("small text", "anything", max_chars=500), "small text")

    def test_relevant_chunk_selected_and_first_always_kept(self):
        out = ctx.select_context(self.TEXT, "qual o horário de funcionamento? opening hours", max_chars=400)
        self.assertIn("Example Store is a mall", out)
        self.assertIn("Opening hours", out)
        self.assertNotIn("Returns:", out)
        self.assertLessEqual(len(out), 400)

    def test_returns_question_picks_returns(self):
        out = ctx.select_context(self.TEXT, "can I return or exchange my items? returns", max_chars=400)
        self.assertIn("Returns:", out)
        self.assertNotIn("Opening hours", out)

    def test_split_chunks_cuts_long_paragraphs(self):
        chunks = ctx.split_chunks("word " * 500, max_chunk=300)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        self.assertGreater(len(chunks), 5)

    def test_a_stray_tail_stays_with_its_section(self):
        """Measured on the real company text: a 608-character section left 'to Brazil.' as a 10-character chunk of its own."""
        chunks = ctx.split_chunks("A" * 590 + " to Brazil.\n\nNext paragraph here.")
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].endswith("to Brazil."))
        self.assertTrue(all(len(c) >= 15 for c in chunks))

    def test_history_and_store_questions_find_their_sections_in_every_language(self):
        text = ("About us: a family business.\n\n" + "x " * 300 + "\n\nHistory: founded in 1950 by an Italian immigrant.\n\n"
                + "y " * 300 + "\n\nLocations: stores in Central City, Northtown and Riverside.\n\n" + "z " * 300)
        for q, want in (("Vocês existem desde que ano?", "History:"), ("Desde cuándo existen?", "History:"),
                        ("Since when do you exist?", "History:"), ("¿Cuántas tiendas tienen?", "Locations:"),
                        ("Quantas lojas vocês têm?", "Locations:"), ("Ile macie sklepów?", "Locations:")):
            self.assertIn(want, ctx.select_context(text, q, max_chars=800), q)


class TestAIParsing(unittest.TestCase):
    def test_parse_messy_output(self):
        out = parse_classification(
            "```\n1|NEG|pt|2|delivery|never arrived\n- 2 | OK | es | 0 | other | praise\n3|Q|pt|0|price|asks the price\n"
            "garbage line\n9|NEG|pt|1|other|unexpected number\n```", {1, 2, 3})
        self.assertEqual(set(out), {1, 2, 3})
        self.assertEqual(out[1]["sentiment"], "NEG")
        self.assertEqual(out[1]["severity"], 2)
        self.assertEqual(out[2]["lang"], "es")
        self.assertEqual(out[3]["sentiment"], "Q")

    def test_sanitize_reply(self):
        self.assertEqual(sanitize_reply('Reply: "Oi, tudo bem!"'), "Oi, tudo bem!")
        long = "Olá! " + "Estamos muito felizes em ajudar você com isso. " * 8
        out = sanitize_reply(long)
        self.assertLessEqual(len(out), 150)
        self.assertTrue(out.endswith((".", "!", "…")))


class TestAIBudget(unittest.TestCase):
    def ok_lines(self, n):
        return {"content": "\n".join(f"{i}|OK|pt|0|other|fine" for i in range(1, n + 1))}

    def test_batch_is_one_request(self):
        db = tmp_db()
        ai = make_ai(db, [self.ok_lines(12)])
        items = [{"cid": f"c{i}", "text": f"texto {i}"} for i in range(12)]
        res, err = ai.classify_batch(items, "Brand")
        self.assertEqual((len(res), err), (12, None))
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(db.usage_for_day()["calls"], 1)

    def test_twenty_five_comments_three_requests(self):
        db = tmp_db()
        ai = make_ai(db, lambda m, s, u: self.ok_lines(u.count("\n") + 1))
        items = [{"cid": f"c{i}", "text": f"texto {i}"} for i in range(25)]
        res, err = ai.classify_batch(items, "Brand")
        self.assertEqual(len(res), 25)
        self.assertEqual(len(ai.calls), 3)

    def test_budget_is_enforced_and_reserve_kept_for_drafts(self):
        db = tmp_db()
        db.set_setting("ai_daily_requests", "10")           # reserve = max(2, 10//5) = 2
        ai = make_ai(db, [{"content": "1|OK|pt|0|other|x"}])
        for i in range(8):                                   # 8 classify calls use 8 of 10
            res, _ = ai.classify_batch([{"cid": f"c{i}", "text": "t"}], "B")
            self.assertEqual(len(res), 1, f"call {i}")
        self.assertEqual(ai.remaining(), 2)
        res, err = ai.classify_batch([{"cid": "x", "text": "t"}], "B")
        self.assertEqual(res, {})
        self.assertIn("reserve", err.lower())
        self.assertEqual(len(ai.calls), 8, "no request was made once only the reserve was left")
        ai2 = make_ai(db, [{"content": "Oi!"}])               # drafting may still use the reserve
        reply, _m, e = ai2.draft_reply({"text": "ruim", "lang": "pt"})
        self.assertEqual((reply, e), ("Oi!", None))

    def test_daily_limit_429_parks_ai_until_tomorrow(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": None, "http": 429, "error": "http 429",
                           "body": '{"error":{"message":"Rate limit exceeded: free-models-per-day"}}'}])
        res, err = ai.classify_batch([{"cid": "a", "text": "t"}], "B")
        self.assertEqual(res, {})
        self.assertTrue(ai.exhausted())
        n = len(ai.calls)
        self.assertEqual(ai.draft_reply({"text": "x"})[0], None)
        ai.classify_batch([{"cid": "b", "text": "t"}], "B")
        self.assertEqual(len(ai.calls), n, "no further requests after the daily limit")
        self.assertEqual(db.meta_get("ai_exhausted_day"), utc_day())

    def test_per_minute_429_only_cools_that_model(self):
        db = tmp_db()
        seen = []

        def replies(model, system, user):
            seen.append(model)
            if len(seen) == 1:
                return {"content": None, "http": 429, "error": "http 429", "body": "rate limited, retry shortly"}
            return {"content": "1|OK|pt|0|other|x"}

        ai = make_ai(db, replies)
        res, err = ai.classify_batch([{"cid": "a", "text": "t"}], "B")
        self.assertEqual(len(res), 1)
        self.assertEqual(len(set(seen[:2])), 2, "fell through to the next model")
        self.assertFalse(ai.exhausted())

    def test_auth_error_stops_the_chain(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": None, "http": 401, "error": "http 401", "body": "no key"}])
        res, err = ai.classify_batch([{"cid": "a", "text": "t"}], "B")
        self.assertEqual(len(ai.calls), 1)
        self.assertIn("auth", err.lower())

    def test_draft_uses_instructions_and_only_relevant_context(self):
        db = tmp_db()
        db.set_setting("ai_instructions", "Always sign off with a smiley :)")
        db.set_setting("company_context", TestContext.TEXT)
        ai = make_ai(db, [{"content": "Claro! Abrimos das 8h às 18h."}])
        reply, model, err = ai.draft_reply({"text": "qual o horário de funcionamento?", "lang": "pt",
                                            "username": "ana", "category": "other"})
        self.assertEqual(reply, "Claro! Abrimos das 8h às 18h.")
        system = ai.calls[0]["system"]
        self.assertIn("Always sign off with a smiley", system)
        self.assertIn("Opening hours", system)
        sent = system.split("Company information:")[1]
        self.assertLessEqual(len(sent), config.AI_CONTEXT_CHARS_FOR_QUESTIONS + 5)
        self.assertEqual(sent.strip(), TestContext.TEXT.strip(), "a company text that fits the question cap is sent whole")

    def test_questions_are_answered_with_low_randomness_and_told_to_quote_numbers_exactly(self):
        db = tmp_db()
        db.set_setting("company_context", "Founded in 1950.")
        ai = make_ai(db, [{"content": "Desde 1950!"}])
        ai.draft_reply({"text": "desde que ano?", "lang": "pt", "status": "question"})
        ai.draft_reply({"text": "péssimo", "lang": "pt", "status": "negative"})
        self.assertEqual(ai.calls[0]["temperature"], 0.1, "a factual answer is not a dice roll")
        self.assertIsNone(ai.calls[1]["temperature"], "complaint replies keep the normal setting")
        self.assertIn("EXACTLY as they are written", ai.calls[0]["system"])
        self.assertIn("1950, not 'more than 70 years'", ai.calls[0]["system"])

    def test_a_question_with_no_company_information_is_never_answered_by_guessing(self):
        db = tmp_db()                                            # nothing saved under /ai_context
        ai = make_ai(db, [{"content": "Sim, fazemos envios para todo o Brasil."}])
        reply, model, err = ai.draft_reply({"text": "Faz envio para o Brasil ?", "lang": "pt", "status": "question"})
        self.assertEqual(reply, CONFIRM_REPLY["pt"], "the honest 'we will confirm' reply, in the comment's language")
        self.assertIsNone(err)
        self.assertEqual(ai.calls, [], "and it costs no AI request")
        self.assertTrue(any("no company information" in str(e) for e in db.recent_events(5)), "and it is logged")
        # a complaint is still answered by the AI: an apology needs no facts
        ai2 = make_ai(db, [{"content": "Sentimos muito."}])
        self.assertEqual(ai2.draft_reply({"text": "péssimo", "lang": "pt", "status": "negative"})[0], "Sentimos muito.")

    def test_a_question_gets_more_of_the_company_information_than_a_complaint(self):
        long_ctx = "About us: family business.\n\n" + "\n\n".join(f"Section {i}: " + f"fact {i}. " * 60 for i in range(9))
        self.assertGreater(len(long_ctx), config.AI_CONTEXT_CHARS_PER_CALL * 3)
        db = tmp_db()
        db.set_setting("company_context", long_ctx)
        ai = make_ai(db, [{"content": "ok"}])
        ai.draft_reply({"text": "o que vocês vendem?", "lang": "pt", "status": "question"})
        ai.draft_reply({"text": "péssimo, nunca mais", "lang": "pt", "status": "negative"})
        asked = len(ai.calls[0]["system"].split("Company information:")[1])
        complained = len(ai.calls[1]["system"].split("Company information:")[1])
        self.assertGreater(asked, config.AI_CONTEXT_CHARS_PER_CALL * 2, "answers need the facts")
        self.assertLessEqual(complained, config.AI_CONTEXT_CHARS_PER_CALL + 5, "complaints keep the small, cheap share")

    def test_default_instructions_used_when_unset(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": "Oi!"}])
        ai.draft_reply({"text": "x", "lang": "pt"})
        self.assertIn(DEFAULT_INSTRUCTIONS[:40], ai.calls[0]["system"])
        self.assertIn("no company information", ai.calls[0]["system"])


class TestAIModels(unittest.TestCase):
    def test_reasoning_is_off_by_default(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": "1|OK|pt|0|other|x"}])
        ai.classify_batch([{"cid": "a", "text": "t"}], "B")
        self.assertEqual(ai.calls[0]["reasoning"], {"enabled": False})

    def test_mandatory_reasoning_model_is_retried_once_then_remembered(self):
        db = tmp_db()
        state = {"n": 0}

        def replies(model, system, user):
            state["n"] += 1
            if state["n"] == 1:
                return {"content": None, "http": 400, "error": "http 400",
                        "body": '{"message": "Reasoning is mandatory for this endpoint and cannot be disabled."}'}
            return {"content": "1|OK|pt|0|other|x"}

        ai = make_ai(db, replies)
        res, err = ai.classify_batch([{"cid": "a", "text": "t"}], "B")
        self.assertEqual(len(res), 1)
        self.assertEqual(ai.calls[0]["model"], ai.calls[1]["model"], "same model, second try")
        self.assertEqual(ai.calls[1]["reasoning"], {"effort": "low"})
        self.assertGreaterEqual(ai.calls[1]["max_tokens"], 1800, "room to think")
        ai.classify_batch([{"cid": "b", "text": "t"}], "B")                    # remembered: straight to reasoning on
        self.assertEqual(ai.calls[2]["reasoning"], {"effort": "low"})
        self.assertEqual(len(ai.calls), 3)

    def test_openrouters_own_counter_caps_the_budget(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": "1|OK|pt|0|other|x"}], key_info=lambda: {"used": 998, "limit": 1000, "remaining": 2})
        self.assertEqual(ai.remaining(), 2)
        self.assertFalse(ai.can("classify"), "only the reserve is left")
        self.assertTrue(ai.can("draft"))
        self.assertIn("OpenRouter free quota left: 2", ai.budget_text())
        ai0 = make_ai(db, [{"content": "x"}], key_info=lambda: {"remaining": 0})
        self.assertEqual(ai0.remaining(), 0)
        self.assertIsNone(ai0.draft_reply({"text": "t"})[0])
        self.assertEqual(len(ai0.calls), 0)

    def test_unreadable_remote_counter_falls_back_to_the_local_budget(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": "x"}], key_info=lambda: None)
        self.assertEqual(ai.remaining(), ai.limit())

    def test_model_list_is_checked_once_a_day_and_vanished_models_dropped(self):
        db = tmp_db()
        calls = []
        first = config.AI_MODEL_CHAIN[0]
        live = [m for m in config.AI_MODEL_CHAIN if m != first] + [
            "acme/big-chat:free", "acme/safety-guard:free", "acme/coder-7b:free", "acme/other-chat:free",
            "acme/third-chat:free", "acme/fourth-chat:free"]

        def models():
            calls.append(1)
            return live

        ai = make_ai(db, [{"content": "x"}], models_info=models)
        chain = ai.chain()
        self.assertNotIn(first, chain)
        self.assertEqual(chain[:len(config.AI_MODEL_CHAIN) - 1], [m for m in config.AI_MODEL_CHAIN if m != first])
        extras = chain[len(config.AI_MODEL_CHAIN) - 1:]
        self.assertEqual(extras, ["acme/big-chat:free", "acme/other-chat:free", "acme/third-chat:free"],
                         "safety / code models are never used, at most 3 extras")
        ai.chain()
        ai.chain()
        self.assertEqual(len(calls), 1, "one lookup per UTC day")
        self.assertTrue(any(e["kind"] == "ai_models_dropped" for e in db.recent_events(10)))

    def test_unreachable_model_list_keeps_the_configured_chain(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": "x"}], models_info=lambda: None)
        self.assertEqual(ai.chain(), list(config.AI_MODEL_CHAIN))


class TestLanguage(unittest.TestCase):
    def test_guess_polish_spanish_portuguese_english(self):
        from app.lang import guess_language
        self.assertEqual(guess_language("Czy macie jeszcze ten telefon? Ile kosztuje i kiedy będzie dostawa?")[0], "pl")
        self.assertEqual(guess_language("¿Cuánto cuesta y dónde están ubicados? Gracias")[0], "es")
        self.assertEqual(guess_language("não recebi meu pedido, péssimo atendimento")[0], "pt")
        self.assertEqual(guess_language("is it open on Sunday? what is the price")[0], "en")
        self.assertEqual(guess_language("👍👍")[0], None)

    def test_strong_evidence_corrects_a_wrong_ai_label_weak_evidence_does_not(self):
        from app.lang import resolve_language
        self.assertEqual(resolve_language("Dziękuję bardzo, wszystko dotarło szybko", "pt"), "pl")
        self.assertEqual(resolve_language("Compré el producto y nunca llegó, es una estafa", "pt"), "es")
        self.assertEqual(resolve_language("Qual o preço do iPhone?", "es"), "pt")
        self.assertEqual(resolve_language("hola", "es"), "es")
        self.assertEqual(resolve_language("top", "pt"), "pt", "ambiguous short text keeps the AI's label")
        self.assertEqual(resolve_language("???", None), None)
        self.assertEqual(resolve_language("Gostaria de saber o valor", "xx"), None, "unknown label ignored")

    def test_the_draft_prompt_names_the_language_and_forbids_the_context_language(self):
        db = tmp_db()
        db.set_setting("company_context", "Opening hours: Monday to Saturday 8-18.")      # owner wrote it in English
        ai = make_ai(db, [{"content": "Dziękujemy! Zapraszamy od poniedziałku do soboty."}])
        reply, _m, _e = ai.draft_reply({"text": "O której godzinie otwieracie? Czy jest parking?", "lang": "pt",
                                        "username": "ania", "category": "other"})
        system = ai.calls[0]["system"]
        self.assertIn("Polish", system, "the AI's wrong 'pt' label was corrected offline")
        self.assertIn("ALWAYS in the language of the customer's comment", system)
        self.assertEqual(reply, "Dziękujemy! Zapraszamy od poniedziałku do soboty.")
        ai2 = make_ai(db, [{"content": "¡Gracias!"}])
        ai2.draft_reply({"text": "¿Cuánto cuesta el envío?", "lang": "es", "username": "x"})
        self.assertIn("Spanish", ai2.calls[0]["system"])
        ai3 = make_ai(db, [{"content": "ok"}])
        ai3.draft_reply({"text": "👍", "lang": None, "username": "x"})
        self.assertIn("same language as the comment", ai3.calls[0]["system"])

    def test_polish_negative_wording_is_caught_without_the_ai(self):
        fb = heuristics.fallback_classify("To jest oszustwo, nigdy więcej nie kupię!")
        self.assertEqual((fb["sentiment"], fb["severity"], fb["category"]), ("NEG", 3, "scam"))
        self.assertEqual(heuristics.fallback_classify("Nie polecam, paczka nie dotarła")["category"], "delivery")
        self.assertIsNotNone(heuristics.screen("Świetny sklep, polecam!"))
        self.assertIsNone(heuristics.screen("Ile kosztuje?"))

    def test_polish_questions_pick_the_right_company_context_chunk(self):
        text = ("Example Store is a mall.\n\nOpening hours: Monday to Saturday 8am to 6pm.\n\n"
                "Shipping: pickup only.\n\nReturns: exchange within 7 days.\n\n" + "filler " * 200)
        out = ctx.select_context(text, "O której godzinie jesteście otwarci w niedzielę?", max_chars=300)
        self.assertIn("Opening hours", out)
        self.assertNotIn("Returns:", out)

    def test_approvals_store_the_resolved_language_and_cards_show_its_name(self):
        from app import notifier
        db = tmp_db()
        db.upsert_post({"video_id": "v1", "url": "u", "description": "d", "is_pinned": 0, "comment_count": 1})
        db.insert_new_comments("v1", [comment("p1", "To oszustwo! Nigdy więcej nie kupię w tym sklepie.")])
        db.update_comment("p1", status="negative", lang="pt", severity=3, category="scam")     # AI said 'pt': wrong
        ai = make_ai(db, [{"content": "Przykro nam z powodu tej sytuacji."}])
        approval_id = notifier.create_approval(db, ai, db.get_comment("p1"))
        a = db.get_approval(approval_id)
        self.assertEqual(a["lang"], "pl")
        self.assertIn("(Polish)", notifier.format_card(db, a, "en"))


class TestAnalysis(unittest.TestCase):
    def test_free_paths_cost_nothing(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": ""}])
        stats = analysis.ingest_comments(db, "v1", [comment("1", "😍😍"), comment("2", "Amei!"),
                                                    comment("3", "É golpe, nunca mais!")], first_scan=False)
        self.assertEqual((stats["new"], stats["heuristic"], stats["pending"]), (3, 2, 1))
        self.assertEqual(db.usage_for_day()["calls"], 0)

    def test_classify_then_cache_means_no_second_call(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": "1|NEG|pt|3|scam|accuses the shop of fraud"}])
        analysis.ingest_comments(db, "v1", [comment("1", "É golpe, nunca mais!")], first_scan=False)
        st = analysis.classify_pending(db, ai)
        self.assertEqual(st["classified"], 1)
        row = db.get_comment("1")
        self.assertEqual((row["status"], row["severity"], row["category"], row["source"]), ("negative", 3, "scam", "ai"))
        # the SAME text on another post: served from the cache, zero AI requests
        stats = analysis.ingest_comments(db, "v2", [comment("9", "é golpe, nunca  mais!")], first_scan=False)
        self.assertEqual(stats["cache"], 1)
        self.assertEqual(db.get_comment("9")["status"], "negative")
        self.assertEqual(len(ai.calls), 1)

    def test_known_comments_are_never_analysed_again(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": "1|OK|pt|0|other|x"}])
        c = comment("1", "Qual o preço do iPhone?")
        analysis.ingest_comments(db, "v1", [c], first_scan=False)
        analysis.classify_pending(db, ai)
        stats = analysis.ingest_comments(db, "v1", [c], first_scan=False)
        self.assertEqual(stats["new"], 0)
        self.assertEqual(analysis.classify_pending(db, ai)["candidates"], 0)
        self.assertEqual(len(ai.calls), 1)

    def test_our_own_accounts_are_never_analysed_or_announced(self):
        db = tmp_db()
        own = analysis.own_usernames(db)
        self.assertIn("examplebrand", own)
        stats = analysis.ingest_comments(db, "v1", [
            {**comment("a", "Obrigado pela atenção, clientes!", username="ExampleBrand")},
            {**comment("b", "É golpe, nunca mais!", username="alguem")}], first_scan=False)
        self.assertEqual(db.get_comment("a")["status"], "skipped")
        self.assertEqual(db.get_comment("b")["status"], "new")
        self.assertEqual(stats["pending"], 1)

    def test_first_scan_only_looks_back_a_week(self):
        db = tmp_db()
        old = comment("o", "É golpe!", create_time=int(time.time()) - 40 * 86400)
        new = comment("n", "É golpe!!", create_time=int(time.time()) - 3600)
        stats = analysis.ingest_comments(db, "v1", [old, new], first_scan=True)
        self.assertEqual((stats["legacy"], stats["pending"]), (1, 1))
        self.assertEqual(db.get_comment("o")["status"], "legacy")

    def test_ai_down_strong_negative_falls_back_others_wait(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": None, "http": 500, "error": "http 500", "body": ""}])
        analysis.ingest_comments(db, "v1", [comment("1", "isso é uma fraude, golpe!"),
                                            comment("2", "gostaria de saber mais sobre vocês")], first_scan=False)
        st = analysis.classify_pending(db, ai)
        self.assertEqual(st["fallback"], 1)
        self.assertEqual(db.get_comment("1")["source"], "fallback")
        self.assertEqual(db.get_comment("2")["status"], "new", "harmless unknowns just wait for the AI")
        self.assertEqual(db.get_comment("2")["attempts"], 0, "an AI outage is not the comment's fault")

    def test_unparseable_answers_give_up_after_three_tries(self):
        db = tmp_db()
        ai = make_ai(db, [{"content": "I cannot do that."}])
        analysis.ingest_comments(db, "v1", [comment("1", "texto estranho aqui")], first_scan=False)
        for _ in range(3):
            analysis.classify_pending(db, ai)
        self.assertEqual(db.get_comment("1")["status"], "skipped")
        self.assertEqual(analysis.classify_pending(db, ai)["candidates"], 0)


if __name__ == "__main__":
    unittest.main()
