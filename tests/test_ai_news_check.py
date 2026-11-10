"""Offline tests for the Claude news check. Nothing here calls Alpaca or Claude.

Run from the repo root:
    python -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ["APCA_API_BASE_URL"] = "https://paper-api.alpaca.markets/v2"
os.environ.setdefault("TRADING_DB_PATH", str(Path(tempfile.gettempdir()) / "ai_news_check_test.db"))

import ai_news_check  # noqa: E402
import autonomous_trader  # noqa: E402


def candidate(symbol, score=7):
    return {
        "symbol": symbol, "score": score, "latest": 100.0, "sma20": 99.0, "sma50": 98.0,
        "return_5": 0.01, "return_20": 0.03, "atr": 0.5, "atr_pct": 0.5,
        "reasons": ["price above SMA20", "SMA20 above SMA50", "usable volatility"],
    }


CANDIDATES = [candidate("NVDA", 8), candidate("AAPL", 7), candidate("SPY", 5)]


def claude_response(payload=None, stop_reason="end_turn", text=None):
    body = text if text is not None else json.dumps(payload)
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=body)],
        usage=SimpleNamespace(input_tokens=400, output_tokens=60),
    )


def fake_client(response=None, error=None):
    create = mock.Mock(side_effect=error) if error else mock.Mock(return_value=response)
    return SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(create=create)),
        messages=SimpleNamespace(create=create),
    ), create


ALLOW = {"verdict": "allow", "risk_flags": [], "confidence": 0.8, "reason": "Routine news only."}
VETO = {"verdict": "veto", "risk_flags": ["earnings_tomorrow"], "confidence": 0.9, "reason": "Earnings are due tomorrow."}


class Harness(unittest.TestCase):
    """Runs run_entry with Alpaca, the database and files mocked into a temp folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.old_cwd = os.getcwd()
        os.chdir(tmp)  # paper_bot.log_trade writes trade_log.csv to the cwd
        self.ai_config = dict(ai_news_check.DEFAULTS)
        self.patches = [
            mock.patch.object(ai_news_check, "REVIEW_FILE", tmp / "ai_review_log.json"),
            mock.patch.object(autonomous_trader, "STATE_FILE", tmp / "autonomous_state.json"),
            mock.patch.object(ai_news_check, "settings", lambda: dict(self.ai_config)),
            mock.patch.object(ai_news_check, "fetch_headlines", mock.Mock(return_value=[
                {"time": "2026-11-09T14:00:00Z", "source": "test", "headline": "Company news", "summary": ""},
            ])),
            mock.patch.object(ai_news_check, "NEWS_STATE_FILE", tmp / "ai_news_scan_state.json"),
            mock.patch.object(ai_news_check, "fetch_news_items", mock.Mock(side_effect=self.news_items)),
        ]
        self.news_feed = [
            {"id": "n1", "time": "2026-11-09T14:00:00Z", "symbols": ["AAPL"], "source": "test",
             "headline": "Apple beats earnings and raises guidance", "summary": ""},
        ]
        for patch in self.patches:
            patch.start()
        self.submit = mock.Mock(return_value={"id": "order-1", "status": "accepted"})
        self.db_trade = mock.Mock()
        self.db_decision = mock.Mock()
        self.make_client = mock.Mock(side_effect=AssertionError("Claude should not be called"))

    def news_items(self, symbols, limit, lookback_hours):
        return [item for item in self.news_feed if set(item["symbols"]) & set(symbols)]

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def run_entry(self, module=autonomous_trader, candidates=CANDIDATES):
        state = module.default_state()
        state["enabled"] = True
        with mock.patch.object(module, "scan_candidates", return_value=(True, [dict(c) for c in candidates], "regime ok")), \
             mock.patch.object(module.paper_bot, "submit_order", self.submit), \
             mock.patch.object(module.trading_database, "log_trade", self.db_trade), \
             mock.patch.object(module.trading_database, "log_decision", self.db_decision), \
             mock.patch.object(ai_news_check, "make_client", self.make_client):
            events = module.run_entry(state, [], [], {}, {"is_open": True})
        return state, events

    def trade_log_rows(self):
        path = Path("trade_log.csv")
        return path.read_text(encoding="utf-8").splitlines()[1:] if path.exists() else []


class AiOffTests(Harness):
    def test_ai_off_matches_original_code(self):
        self.ai_config["enabled"] = False
        original = load_original_trader()
        if original is None:
            self.skipTest("git history not available")

        state_new, events_new = self.run_entry()
        submit_new = self.submit.call_args_list[:]
        db_new = self.db_trade.call_args_list[:]
        log_new = self.trade_log_rows()

        Path("trade_log.csv").unlink()
        self.submit.reset_mock()
        self.db_trade.reset_mock()
        state_old, events_old = self.run_entry(module=original)

        self.assertEqual(events_new, events_old)
        self.assertEqual(submit_new, self.submit.call_args_list)
        self.assertEqual(db_new, self.db_trade.call_args_list)
        strip_time = lambda rows: [row.split(",", 1)[1] for row in rows]  # noqa: E731
        self.assertEqual(strip_time(log_new), strip_time(self.trade_log_rows()))
        for key in ("trades_today", "last_decision"):
            self.assertEqual(state_new[key], state_old[key])
        self.make_client.assert_not_called()
        self.submit.assert_called_once_with("NVDA", 10.0)


class AiOnTests(Harness):
    def setUp(self):
        super().setUp()
        self.ai_config["enabled"] = True

    def use_client(self, response=None, error=None):
        client, create = fake_client(response, error)
        self.make_client = mock.Mock(return_value=client)
        return create

    def test_allow_places_the_same_order(self):
        create = self.use_client(claude_response(ALLOW))
        state, events = self.run_entry()
        self.submit.assert_called_once_with("NVDA", 10.0)  # same symbol, same size
        create.assert_called_once()
        self.assertEqual(state["trades_today"], 1)
        self.assertIn("Claude allowed", events[0])

    def test_veto_skips_and_logs_everywhere(self):
        self.use_client(claude_response(VETO))
        state, events = self.run_entry()
        self.submit.assert_not_called()
        self.assertEqual(state["trades_today"], 0)
        self.assertIsNone(state["last_trade_at"])
        self.assertTrue(events[0].startswith("AI VETOED NVDA"))  # becomes the Discord message
        self.assertIn("Earnings are due tomorrow", events[0])
        rows = self.trade_log_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("ai_veto", rows[0])
        self.assertEqual(self.db_trade.call_args.kwargs["status"], "ai_veto")
        self.assertEqual(self.db_decision.call_args.args[1], "AI_VETO")

    def test_api_errors_fail_safe(self):
        import anthropic
        import httpx2 as httpx

        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        errors = [
            anthropic.APITimeoutError(request=request),
            anthropic.APIConnectionError(request=request),
            anthropic.InternalServerError("boom", response=httpx.Response(500, request=request), body=None),
            RuntimeError("unexpected"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.submit.reset_mock()
                ai_news_check.REVIEW_FILE.unlink(missing_ok=True)
                self.use_client(error=error)
                state, events = self.run_entry()
                self.submit.assert_not_called()
                self.assertEqual(state["trades_today"], 0)
                self.assertIn("SKIPPED (AI check failed)", events[0])
                self.assertEqual(self.db_trade.call_args.kwargs["status"], "ai_check_failed")

    def test_bad_outputs_fail_safe(self):
        cases = {
            "refusal": claude_response(ALLOW, stop_reason="refusal"),
            "max_tokens": claude_response(ALLOW, stop_reason="max_tokens"),
            "not_json": claude_response(text="sure, allow it"),
            "bad_verdict": claude_response(dict(ALLOW, verdict="buy_more")),
            "bad_confidence": claude_response(dict(ALLOW, confidence=7)),
            "missing_reason": claude_response({k: v for k, v in ALLOW.items() if k != "reason"}),
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                self.submit.reset_mock()
                ai_news_check.REVIEW_FILE.unlink(missing_ok=True)
                self.use_client(response)
                _, events = self.run_entry()
                self.submit.assert_not_called()
                self.assertIn("SKIPPED", events[0])

    def test_missing_key_fails_safe(self):
        self.make_client = mock.Mock(wraps=ai_news_check.make_client)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            _, events = self.run_entry()
        self.submit.assert_not_called()
        self.assertIn("ANTHROPIC_API_KEY", events[0])

    def test_news_fetch_failure_fails_safe(self):
        create = self.use_client(claude_response(ALLOW))
        ai_news_check.fetch_headlines.side_effect = RuntimeError("Alpaca down")
        try:
            _, events = self.run_entry()
        finally:
            ai_news_check.fetch_headlines.side_effect = None
        self.submit.assert_not_called()
        create.assert_not_called()
        self.assertIn("Alpaca down", events[0])

    def test_vetoed_symbol_is_not_rechecked_and_next_candidate_is_checked(self):
        self.use_client(claude_response(VETO))
        self.run_entry()
        create = self.use_client(claude_response(ALLOW))
        self.run_entry()
        # NVDA is held back without a new API call; the rules' next pick (AAPL) is checked.
        self.assertEqual(create.call_count, 1)
        self.assertIn("AAPL", create.call_args.kwargs["messages"][0]["content"])
        self.submit.assert_called_once_with("AAPL", 10.0)

    def test_daily_check_limit_fails_safe(self):
        self.ai_config["max_checks_per_day"] = 0
        create = self.use_client(claude_response(ALLOW))
        _, events = self.run_entry()
        create.assert_not_called()
        self.submit.assert_not_called()
        self.assertIn("Daily AI check limit", events[0])

    def test_existing_limits_still_block_before_ai(self):
        create = self.use_client(claude_response(ALLOW))
        state = autonomous_trader.default_state()
        state.update(enabled=True, trades_today=99)
        with mock.patch.object(autonomous_trader.paper_bot, "submit_order", self.submit), \
             mock.patch.object(ai_news_check, "make_client", self.make_client):
            events = autonomous_trader.run_entry(state, [], [], {}, {"is_open": True})
        self.assertEqual(events, [])
        self.assertEqual(state["last_decision"], "Daily trade limit reached.")
        create.assert_not_called()
        self.submit.assert_not_called()

    def test_recap_lines(self):
        self.use_client(claude_response(VETO))
        self.run_entry()
        lines = ai_news_check.recap_lines()
        self.assertIn("1 vetoed", lines[0])
        self.assertIn("NVDA VETO", lines[1])


WEAK = [candidate("SPY", 5), candidate("NVDA", 5), candidate("TSLA", 3)]  # none reach the rules' 6/8


def news_response(*picks):
    return claude_response({"picks": [
        {"symbol": s, "action": a, "confidence": c, "reason": f"{s} reason"} for s, a, c in picks
    ]})


def story(story_id, *symbols, headline="Company beats earnings and raises guidance"):
    return {"id": story_id, "time": "2026-11-09T14:00:00Z", "symbols": list(symbols), "source": "test",
            "headline": headline, "summary": ""}


class NewsEntryTests(Harness):
    def setUp(self):
        super().setUp()
        self.ai_config.update(enabled=True, news_entries_enabled=True)
        self.news_feed = [story("n1", "AAPL")]
        # Technical results the bot's own rules would produce for symbols with news.
        self.scores = {"AAPL": candidate("AAPL", 5), "MSFT": candidate("MSFT", 5)}
        self.extra = [
            mock.patch.object(autonomous_trader, "get_bars", mock.Mock(return_value=[])),
            mock.patch.object(autonomous_trader, "analyze_bars", side_effect=lambda s, b, r: self.scores.get(s)),
        ]
        for patch in self.extra:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.extra):
            patch.stop()
        super().tearDown()

    def use_client(self, *responses, error=None):
        create = mock.Mock(side_effect=error if error else list(responses))
        client = SimpleNamespace(
            beta=SimpleNamespace(messages=SimpleNamespace(create=create)),
            messages=SimpleNamespace(create=create),
        )
        self.make_client = mock.Mock(return_value=client)
        return create

    def test_news_buy_needs_catalyst_and_risk_check(self):
        create = self.use_client(news_response(("AAPL", "buy", 0.85)), claude_response(ALLOW))
        state, events = self.run_entry(candidates=WEAK)
        self.submit.assert_called_once_with("AAPL", 10.0)  # AAPL is not on the rules' watchlist
        self.assertEqual(state["trades_today"], 1)
        self.assertEqual(state["news_entries_today"], 1)
        self.assertTrue(events[0].startswith("AUTO NEWS BUY AAPL"))
        self.assertEqual(self.db_trade.call_args.kwargs["source"], "autonomous_news")
        self.assertEqual(create.call_count, 2)
        first = create.call_args_list[0].kwargs
        enum = first["output_config"]["format"]["schema"]["properties"]["picks"]["items"]["properties"]["symbol"]["enum"]
        self.assertEqual(enum, ["AAPL"])  # only stocks that passed every filter are offered
        self.assertEqual(first["output_config"]["effort"], "medium")

    def test_risk_check_veto_blocks_news_buy(self):
        self.use_client(news_response(("AAPL", "buy", 0.95)), claude_response(VETO))
        state, events = self.run_entry(candidates=WEAK)
        self.assertEqual(events, [])
        self.submit.assert_not_called()
        self.assertIn("blocked by the risk check", state["last_news_scan"])

    def test_weak_technicals_never_reach_claude(self):
        self.scores["AAPL"] = candidate("AAPL", 3)
        state, events = self.run_entry(candidates=WEAK)  # make_client raises if called
        self.assertEqual(events, [])
        self.assertIn("none passed the technical filter", state["last_news_scan"])

    def test_falling_price_never_reaches_claude(self):
        self.scores["AAPL"] = dict(candidate("AAPL", 6), return_5=-0.02)
        state, events = self.run_entry(candidates=WEAK)
        self.assertEqual(events, [])
        self.assertIn("none passed the technical filter", state["last_news_scan"])

    def test_below_sma20_never_reaches_claude(self):
        # Would be sold by the SMA20 exit on the very next cycle.
        self.scores["AAPL"] = dict(candidate("AAPL", 6), latest=98.0, sma20=99.0)
        state, events = self.run_entry(candidates=WEAK)
        self.assertEqual(events, [])
        self.assertIn("none passed the technical filter", state["last_news_scan"])

    def test_too_volatile_never_reaches_claude(self):
        self.scores["AAPL"] = dict(candidate("AAPL", 6), reasons=["price above SMA20", "SMA20 above SMA50"])
        state, events = self.run_entry(candidates=WEAK)
        self.assertEqual(events, [])
        self.assertIn("none passed the technical filter", state["last_news_scan"])

    def test_chasing_a_spike_never_reaches_claude(self):
        self.scores["AAPL"] = dict(candidate("AAPL", 7), return_5=0.045)  # +4.5% in 5 bars > 3% cap
        state, events = self.run_entry(candidates=WEAK)
        self.assertEqual(events, [])
        self.assertIn("none passed the technical filter", state["last_news_scan"])

    def test_news_scan_runs_when_rule_picks_were_all_vetoed(self):
        # NVDA qualifies for the rules but was vetoed recently; the news path should still get a turn.
        ai_news_check.save_review({
            "time": ai_news_check.now_utc().isoformat(), "symbol": "NVDA", "source": "autonomous",
            "allowed": False, "verdict": "veto", "api_called": True,
        })
        self.use_client(news_response(("AAPL", "buy", 0.9)), claude_response(ALLOW))
        state, events = self.run_entry(candidates=[candidate("SPY", 5), candidate("NVDA", 8)])
        self.submit.assert_called_once_with("AAPL", 10.0)
        self.assertTrue(events[0].startswith("AUTO NEWS BUY AAPL"))

    def test_holding_spy_does_not_disable_news_buys(self):
        self.use_client(news_response(("AAPL", "buy", 0.9)), claude_response(ALLOW))
        state = autonomous_trader.default_state()
        state["enabled"] = True
        spy_position = {"symbol": "SPY", "qty": "0.01", "market_value": "5", "current_price": "500", "avg_entry_price": "500"}
        with mock.patch.object(autonomous_trader, "scan_candidates", return_value=(True, [dict(c) for c in WEAK], "ok")), \
             mock.patch.object(autonomous_trader.paper_bot, "submit_order", self.submit), \
             mock.patch.object(autonomous_trader.trading_database, "log_trade", self.db_trade), \
             mock.patch.object(autonomous_trader.trading_database, "log_decision", self.db_decision), \
             mock.patch.object(ai_news_check, "make_client", self.make_client):
            events = autonomous_trader.run_entry(state, [spy_position], [], {}, {"is_open": True})
        self.submit.assert_called_once_with("AAPL", 10.0)
        self.assertTrue(events[0].startswith("AUTO NEWS BUY AAPL"))

    def test_review_log_write_is_atomic(self):
        ai_news_check.save_review({"time": "x", "symbol": "A"})
        self.assertFalse(ai_news_check.REVIEW_FILE.with_name(ai_news_check.REVIEW_FILE.name + ".tmp").exists())
        self.assertEqual(len(ai_news_check.load_reviews()), 1)

    def test_roundup_stories_are_ignored(self):
        self.news_feed = [story("r1", "AAPL", "MSFT", "NVDA", "AMZN", headline="5 stocks to watch")]
        state, events = self.run_entry(candidates=WEAK)
        self.assertEqual(events, [])
        self.assertIn("no new single-company headlines", state["last_news_scan"])

    def test_held_and_pending_symbols_are_excluded(self):
        seen = {}

        def capture(symbols, *args):
            seen["symbols"] = symbols
            return []

        ai_news_check.fetch_news_items.side_effect = capture
        try:
            ai_news_check.run_news_scan(lambda s: None, exclude={"AAPL", "NVDA"})
        finally:
            ai_news_check.fetch_news_items.side_effect = self.news_items
        self.assertNotIn("AAPL", seen["symbols"])
        self.assertNotIn("NVDA", seen["symbols"])
        self.assertIn("MSFT", seen["symbols"])
        self.assertTrue(set(seen["symbols"]) <= set(autonomous_trader.paper_bot.ALLOWED_SYMBOLS))

    def test_news_entries_off_never_calls_claude(self):
        self.ai_config["news_entries_enabled"] = False
        state, events = self.run_entry(candidates=WEAK)
        self.assertEqual(events, [])
        self.submit.assert_not_called()
        self.assertTrue(state["last_decision"].startswith("No entry. Best score was SPY 5/8"))

    def test_low_confidence_does_not_buy(self):
        create = self.use_client(news_response(("AAPL", "buy", 0.79)))
        _, events = self.run_entry(candidates=WEAK)
        self.assertEqual(events, [])
        self.submit.assert_not_called()
        self.assertEqual(create.call_count, 1)  # no risk check needed

    def test_unoffered_symbol_fails_safe(self):
        self.use_client(news_response(("TSLA", "buy", 0.99)))
        state, events = self.run_entry(candidates=WEAK)
        self.assertEqual(events, [])
        self.submit.assert_not_called()
        self.assertIn("not offered", state["last_news_scan"])

    def test_api_error_fails_safe(self):
        self.use_client(error=RuntimeError("timeout"))
        state, events = self.run_entry(candidates=WEAK)
        self.assertEqual(events, [])
        self.submit.assert_not_called()
        self.assertIn("timeout", state["last_news_scan"])

    def test_scan_interval_and_seen_headlines_skip_the_api(self):
        create = self.use_client(news_response(("AAPL", "none", 0.9)))
        self.run_entry(candidates=WEAK)
        self.run_entry(candidates=WEAK)  # inside the 30-minute interval: no scan
        self.assertEqual(create.call_count, 1)
        self.ai_config["news_scan_minutes"] = 0  # scan allowed, but the headline was already seen
        self.run_entry(candidates=WEAK)
        self.assertEqual(create.call_count, 1)
        self.submit.assert_not_called()

    def test_daily_scan_limit(self):
        self.ai_config["max_news_scans_per_day"] = 0
        _, events = self.run_entry(candidates=WEAK)
        self.assertEqual(events, [])
        self.submit.assert_not_called()

    def test_daily_news_entry_limit(self):
        state = autonomous_trader.default_state()
        state.update(enabled=True, news_entries_today=2, news_entries_date=autonomous_trader.today_utc())
        with mock.patch.object(autonomous_trader, "scan_candidates", return_value=(True, WEAK, "ok")), \
             mock.patch.object(autonomous_trader.paper_bot, "submit_order", self.submit), \
             mock.patch.object(ai_news_check, "make_client", self.make_client):
            events = autonomous_trader.run_entry(state, [], [], {}, {"is_open": True})
        self.assertEqual(events, [])
        self.submit.assert_not_called()

    def test_existing_limits_block_news_entries(self):
        state = autonomous_trader.default_state()
        state.update(enabled=True, realized_pl_today=-50.0)
        with mock.patch.object(autonomous_trader, "scan_candidates", return_value=(True, WEAK, "ok")), \
             mock.patch.object(autonomous_trader.paper_bot, "submit_order", self.submit), \
             mock.patch.object(ai_news_check, "make_client", self.make_client):
            events = autonomous_trader.run_entry(state, [], [], {}, {"is_open": True})
        self.assertEqual(events, [])
        self.assertEqual(state["last_decision"], "Daily loss limit reached.")

    def test_weak_market_blocks_news_entries(self):
        state = autonomous_trader.default_state()
        state["enabled"] = True
        with mock.patch.object(autonomous_trader, "scan_candidates", return_value=(False, WEAK, "SPY weak.")), \
             mock.patch.object(autonomous_trader.paper_bot, "submit_order", self.submit), \
             mock.patch.object(ai_news_check, "make_client", self.make_client):
            events = autonomous_trader.run_entry(state, [], [], {}, {"is_open": True})
        self.assertEqual(events, [])
        self.submit.assert_not_called()


class RuleGateTests(Harness):
    """The two stricter rule gates (AI off): price above SMA20 and the post-open wait."""

    def setUp(self):
        super().setUp()
        self.ai_config["enabled"] = False

    def test_rules_skip_candidates_below_sma20(self):
        below = dict(candidate("NVDA", 8), latest=98.0, sma20=99.0)
        state, events = self.run_entry(candidates=[below, candidate("AAPL", 7)])
        self.submit.assert_called_once_with("AAPL", 10.0)  # NVDA scored higher but sits below SMA20

    def test_sma20_gate_can_be_turned_off(self):
        below = dict(candidate("NVDA", 8), latest=98.0, sma20=99.0)
        real_cfg = autonomous_trader.cfg

        def cfg(name, default):
            return False if name == "require_price_above_sma20" else real_cfg(name, default)

        with mock.patch.object(autonomous_trader, "cfg", side_effect=cfg):
            self.run_entry(candidates=[below, candidate("AAPL", 7)])
        self.submit.assert_called_once_with("NVDA", 10.0)

    def test_minutes_until_entries_open(self):
        wait = autonomous_trader.minutes_until_entries_open
        self.assertEqual(wait({"timestamp": "2026-11-10T09:30:05.1-05:00"}), 15)
        self.assertEqual(wait({"timestamp": "2026-11-10T09:41:00-05:00"}), 4)
        self.assertEqual(wait({"timestamp": "2026-11-10T09:45:00-05:00"}), 0)
        self.assertEqual(wait({"timestamp": "2026-11-10T15:59:00-05:00"}), 0)
        self.assertEqual(wait({}), 0)  # unreadable clock never blocks

    def test_no_entries_right_after_open(self):
        state = autonomous_trader.default_state()
        state["enabled"] = True
        with mock.patch.object(autonomous_trader.paper_bot, "submit_order", self.submit):
            events = autonomous_trader.run_entry(
                state, [], [], {}, {"is_open": True, "timestamp": "2026-11-10T09:36:00-05:00"},
            )
        self.assertEqual(events, [])
        self.submit.assert_not_called()
        self.assertTrue(state["last_decision"].startswith("Waiting 9 more minute(s) after the open"))


class RealSdkRequestTest(unittest.TestCase):
    """Sends one request through the real anthropic SDK to a fake HTTP server."""

    def test_request_shape_and_parsing(self):
        import anthropic
        import httpx2 as httpx

        seen = {}

        def handler(request):
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": json.dumps(VETO)}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 500, "output_tokens": 80},
            })

        client = anthropic.Anthropic(
            api_key="test-key", base_url="https://example.test",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        config = dict(ai_news_check.DEFAULTS)
        response = ai_news_check.call_claude(client, config, ai_news_check.build_prompt(candidate("NVDA"), []))
        result = ai_news_check.parse_verdict(response)

        self.assertEqual(result["verdict"], "veto")
        body = seen["body"]
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(body["output_config"]["effort"], "low")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertIn("server-side-fallback-2026-07-01", seen["headers"]["anthropic-beta"])
        self.assertNotIn("thinking", body)


def load_original_trader():
    """Load the newest committed autonomous_trader.py from before the AI check existed."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout

    try:
        commits = git("rev-list", "HEAD", "--", "autonomous_trader.py").split()
        source = next(
            (text for text in (git("show", f"{sha}:autonomous_trader.py") for sha in commits)
             if "ai_news_check" not in text),
            None,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    if source is None:
        return None
    path = Path(tempfile.gettempdir()) / "autonomous_trader_original.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("autonomous_trader_original", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE_FILE = autonomous_trader.STATE_FILE
    return module


if __name__ == "__main__":
    unittest.main()
