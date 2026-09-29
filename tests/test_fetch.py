"""The tier chain, retry policy, parsing hazards and source cooldowns."""

from __future__ import annotations

import json
import unittest
import urllib.error

from scraper.config import CollectionConfig
from scraper.fetch import (
    BinanceClient,
    CoinGeckoClient,
    FetchError,
    HyperliquidClient,
    PriceFetcher,
    _finite,
)
from tests.helpers import FakeOpener, FakeResponse, RecordingSleep, api_config, http_error

COINS = {"BTC": "bitcoin", "ETH": "ethereum", "HYPE": "hyperliquid"}
BY_ID = {v: k for k, v in COINS.items()}

BASE_URL = "https://api.coingecko.com/api/v3"
BINANCE_URL = "https://api.binance.com"

#: CoinGecko still sends cap and volume; the parser ignores them now, and these
#: payloads keep that refusal honest rather than assuming a tidier response.
FULL_PAYLOAD = {
    "bitcoin": {"usd": 84386, "usd_market_cap": 1.695e12, "usd_24h_vol": 2.197e10},
    "ethereum": {"usd": 2685.13, "usd_market_cap": 3.27e11, "usd_24h_vol": 8.8e9},
    "hyperliquid": {"usd": 91.22, "usd_market_cap": 2.03e10, "usd_24h_vol": 7.5e8},
}

BATCH = [
    {"symbol": "BTCUSDT", "price": "84394.01000000"},
    {"symbol": "ETHUSDT", "price": "2685.35000000"},
    {"symbol": "HYPEUSDT", "price": "91.30150000"},
]


def client(opener, sleep=None, **overrides):
    settings = dict(
        opener=opener,
        sleep=sleep or RecordingSleep(),
        timeout=2.5,
        max_attempts=2,
        backoff_initial=0.5,
        backoff_multiplier=2.0,
        backoff_max=1.0,
        retry_after_cap=2.0,
        backoff_budget=3.0,
    )
    settings.update(overrides)
    return CoinGeckoClient(BASE_URL, "usd", **settings)


def json_response(payload) -> FakeResponse:
    return FakeResponse(json.dumps(payload))


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class UrlTests(unittest.TestCase):
    def test_builds_the_documented_url(self):
        # Kept literal: the commas must stay unencoded or CoinGecko parses one
        # giant coin id instead of a list.
        self.assertEqual(
            CoinGeckoClient.build_url(BASE_URL, list(BY_ID), "usd"),
            f"{BASE_URL}/simple/price?ids=bitcoin,ethereum,hyperliquid&vs_currencies=usd",
        )

    def test_trailing_slash_on_the_base_url_is_tolerated(self):
        self.assertIn("/simple/price?", CoinGeckoClient.build_url(BASE_URL + "/", ["bitcoin"], "usd"))


class ParsingTests(unittest.TestCase):
    def parse(self, payload, symbol_by_id=None):
        return CoinGeckoClient.parse_payload(payload, symbol_by_id or BY_ID, "usd")

    def test_parses_a_full_response(self):
        observations, missing = self.parse(FULL_PAYLOAD)
        self.assertEqual(missing, [])
        self.assertEqual(len(observations), 3)
        bitcoin = next(o for o in observations if o.symbol == "BTC")
        # An integer price must arrive as a float, uniformly.
        self.assertIsInstance(bitcoin.price_usd, float)
        self.assertEqual(bitcoin.price_usd, 84386.0)
        self.assertEqual(bitcoin.source, "coingecko")

    def test_market_cap_and_volume_are_dropped_not_carried(self):
        # No keyless venue API publishes global market cap, and Binance's
        # quoteVolume is Binance-only -- about 27x narrower than CoinGecko's
        # global figure -- so the column would silently change meaning the
        # moment the primary changed. It is gone, not merely unpopulated.
        bitcoin = next(o for o in self.parse(FULL_PAYLOAD)[0] if o.symbol == "BTC")
        self.assertEqual(sorted(vars(bitcoin)), ["price_usd", "source", "symbol"])

    def test_a_missing_coin_is_reported_not_fatal(self):
        payload = {"bitcoin": FULL_PAYLOAD["bitcoin"], "ethereum": FULL_PAYLOAD["ethereum"]}
        observations, missing = self.parse(payload)
        self.assertEqual(missing, ["hyperliquid"])
        self.assertEqual({o.symbol for o in observations}, {"BTC", "ETH"})

    def test_an_error_body_is_a_failure_not_three_missing_coins(self):
        # HTTP 200 carrying an error object. Without this check it looks exactly
        # like every coin was delisted, and the run records nothing while
        # appearing healthy.
        with self.assertRaises(FetchError) as ctx:
            self.parse({"status": {"error_code": 10002, "error_message": "key required"}})
        self.assertIn("10002", str(ctx.exception))

    def test_rejects_unusable_prices(self):
        cases = {
            "null": None,
            "boolean": True,  # isinstance(True, int) is True, so this needs care
            "string": "84386",
            "zero": 0,
            "negative": -1,
            "list": [1],
        }
        for label, value in cases.items():
            with self.subTest(case=label):
                observations, missing = self.parse(
                    {"bitcoin": {"usd": value}}, {"bitcoin": "BTC"}
                )
                self.assertEqual(observations, [])
                self.assertEqual(missing, ["bitcoin"])

    def test_rejects_nan_and_infinity(self):
        for label, value in {"nan": float("nan"), "inf": float("inf")}.items():
            with self.subTest(case=label):
                observations, missing = self.parse(
                    {"bitcoin": {"usd": value}}, {"bitcoin": "BTC"}
                )
                self.assertEqual(observations, [])
                self.assertEqual(missing, ["bitcoin"])

    def test_a_non_usd_key_is_not_accepted(self):
        observations, missing = self.parse({"bitcoin": {"eur": 1}}, {"bitcoin": "BTC"})
        self.assertEqual(observations, [])
        self.assertEqual(missing, ["bitcoin"])

    def test_non_object_payload_is_rejected(self):
        with self.assertRaises(FetchError):
            self.parse(["not", "an", "object"])


class WireParsingTests(unittest.TestCase):
    def test_bare_nan_literal_is_rejected_at_the_wire(self):
        # Python's json accepts NaN as a non-standard extension. A NaN in the
        # price column would poison every downstream mean.
        opener = FakeOpener(*[FakeResponse('{"bitcoin": {"usd": NaN}}')] * 2)
        with self.assertRaises(FetchError) as ctx:
            client(opener).fetch(BY_ID)
        self.assertIn("non-finite", str(ctx.exception))

    def test_malformed_json_reports_the_body(self):
        opener = FakeOpener(*[FakeResponse("<html>proxy error</html>")] * 2)
        with self.assertRaises(FetchError) as ctx:
            client(opener).fetch(BY_ID)
        self.assertIn("proxy error", str(ctx.exception))


class RetryTests(unittest.TestCase):
    def test_retries_then_succeeds(self):
        sleep = RecordingSleep()
        opener = FakeOpener(
            urllib.error.URLError("dns"),
            urllib.error.URLError("dns"),
            json_response(FULL_PAYLOAD),
        )
        observations, _ = client(
            opener, sleep, max_attempts=3, backoff_initial=1.0, backoff_max=10.0
        ).fetch(BY_ID)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(len(observations), 3)
        # Exponential: 1.0 then 2.0.
        self.assertEqual(sleep.delays, [1.0, 2.0])

    def test_backoff_is_capped_so_it_cannot_eat_a_slot(self):
        sleep = RecordingSleep()
        opener = FakeOpener(
            urllib.error.URLError("dns"),
            urllib.error.URLError("dns"),
            json_response(FULL_PAYLOAD),
        )
        client(opener, sleep, max_attempts=3, backoff_initial=0.5, backoff_max=0.75).fetch(BY_ID)
        self.assertEqual(sleep.delays, [0.5, 0.75])

    def test_honours_retry_after_on_429(self):
        sleep = RecordingSleep()
        opener = FakeOpener(
            http_error(429, retry_after="3"),
            json_response(FULL_PAYLOAD),
        )
        client(opener, sleep, retry_after_cap=60.0).fetch(BY_ID)
        self.assertEqual(sleep.delays, [3.0])

    def test_a_429_without_retry_after_still_parks_the_source(self):
        # No header is not permission to keep polling; fall back to the cap.
        clock = FakeClock()
        opener = FakeOpener(*[http_error(429)] * 4)
        subject = client(opener, RecordingSleep(), retry_after_cap=2.0, now=clock)
        with self.assertRaises(FetchError):
            subject.fetch(BY_ID)
        self.assertEqual(opener.call_count, 2)
        self.assertGreater(subject.unavailable_for(), 0.0)

    def test_a_long_retry_after_parks_the_source_and_ends_the_tick(self):
        # The pause is honoured in full, just not by sleeping through it:
        # capping it and carrying on polling is what escalates a 429 into a
        # 418 IP ban, so the source is parked and the loop keeps ticking.
        sleep = RecordingSleep()
        clock = FakeClock()
        opener = FakeOpener(*[http_error(429, retry_after="120")] * 4)
        subject = client(opener, sleep, retry_after_cap=2.0, now=clock)

        with self.assertRaises(FetchError):
            subject.fetch(BY_ID)

        # One attempt only: a second would be the hammering we were told to stop.
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(sleep.delays, [])
        self.assertAlmostEqual(subject.unavailable_for(), 120.0)

    def test_the_park_expires(self):
        clock = FakeClock()
        subject = client(FakeOpener(), now=clock)
        subject._park(30.0)
        clock.advance(29.0)
        self.assertAlmostEqual(subject.unavailable_for(), 1.0)
        clock.advance(1.0)
        self.assertEqual(subject.unavailable_for(), 0.0)

    def test_a_client_error_is_not_retried(self):
        # Retrying a bad symbol or a bad request just burns the request budget.
        opener = FakeOpener(http_error(404))
        with self.assertRaises(FetchError):
            client(opener).fetch(BY_ID)
        self.assertEqual(opener.call_count, 1)

    def test_a_non_retryable_error_carries_its_status(self):
        # The batch degrade hinges on telling "your symbol is wrong" from
        # "the venue is down"; without the status they are the same exception.
        opener = FakeOpener(http_error(400, body=b'{"msg":"Invalid symbol."}'))
        with self.assertRaises(FetchError) as ctx:
            client(opener).fetch(BY_ID)
        self.assertEqual(ctx.exception.status, 400)

    def test_server_errors_are_retried(self):
        opener = FakeOpener(http_error(503), json_response(FULL_PAYLOAD))
        client(opener).fetch(BY_ID)
        self.assertEqual(opener.call_count, 2)

    def test_gives_up_within_the_backoff_budget(self):
        # A tick must not be able to run into the next one.
        sleep = RecordingSleep()
        opener = FakeOpener(*[urllib.error.URLError("down")] * 5)
        with self.assertRaises(FetchError) as ctx:
            client(opener, sleep, backoff_budget=0.4, backoff_initial=1.0).fetch(BY_ID)
        self.assertIn("budget", str(ctx.exception))
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(sleep.delays, [])

    def test_timeout_reaches_the_transport(self):
        opener = FakeOpener(json_response(FULL_PAYLOAD))
        client(opener, timeout=1.25).fetch(BY_ID)
        self.assertEqual(opener.calls[0][1], 1.25)

    def test_sends_a_user_agent_and_no_gzip_request(self):
        opener = FakeOpener(json_response(FULL_PAYLOAD))
        client(opener).fetch(BY_ID)
        headers = opener.calls[0][0].headers
        self.assertTrue(headers.get("User-agent"))
        # urllib will not decompress gzip for us, so asking for it yields mojibake.
        self.assertIsNone(headers.get("Accept-encoding"))


class FiniteTests(unittest.TestCase):
    def test_rejects_booleans_and_accepts_real_numbers(self):
        self.assertIsNone(_finite(True))
        self.assertIsNone(_finite(False))
        self.assertEqual(_finite(5), 5.0)

    def test_string_mode_for_venues(self):
        self.assertIsNone(_finite("1.5"))
        self.assertEqual(_finite("1.5", allow_str=True), 1.5)
        self.assertIsNone(_finite("not-a-number", allow_str=True))

    def test_zero_only_allowed_where_asked(self):
        self.assertIsNone(_finite(0))
        self.assertEqual(_finite(0, allow_zero=True), 0.0)


class BinanceTests(unittest.TestCase):
    def batch(self, symbols, opener, **kwargs):
        return BinanceClient(BINANCE_URL, opener=opener, **kwargs).fetch_batch(symbols)

    def test_prices_every_symbol_in_one_request(self):
        opener = FakeOpener(json_response(BATCH))
        observations, missing = self.batch(["BTC", "ETH", "HYPE"], opener)

        self.assertEqual(opener.call_count, 1)
        self.assertEqual(missing, [])
        self.assertEqual(
            [(o.symbol, o.source) for o in observations],
            [("BTC", "binance"), ("ETH", "binance"), ("HYPE", "binance")],
        )
        # Prices arrive as JSON strings and must come out as floats.
        self.assertEqual(observations[0].price_usd, 84394.01)
        self.assertIsInstance(observations[0].price_usd, float)

    def test_the_symbol_list_carries_no_spaces(self):
        # Load-bearing, and it cost a live 400 to find: json.dumps' default
        # separator is ", " with a space, and Binance answers
        # "400 -1100 Illegal characters" for it. The whole query is
        # percent-encoded, so the encoded form is what must be asserted.
        opener = FakeOpener(json_response(BATCH))
        self.batch(["BTC", "ETH"], opener)
        self.assertEqual(
            opener.urls()[0],
            f"{BINANCE_URL}/api/v3/ticker/price?symbols=%5B%22BTCUSDT%22%2C%22ETHUSDT%22%5D",
        )

    def test_an_empty_symbol_list_makes_no_request(self):
        opener = FakeOpener()
        self.assertEqual(self.batch([], opener), ([], []))
        self.assertEqual(opener.call_count, 0)

    def test_a_symbol_the_batch_omits_is_reported_missing(self):
        opener = FakeOpener(json_response(BATCH[:2]))
        observations, missing = self.batch(["BTC", "ETH", "HYPE"], opener)
        self.assertEqual(missing, ["HYPE"])
        self.assertEqual([o.symbol for o in observations], ["BTC", "ETH"])

    def test_a_row_that_cannot_be_used_is_missing_not_wrong(self):
        payload = [
            {"symbol": "BTCUSDT", "price": "84394.01000000"},
            {"symbol": "ETHUSDT", "price": "not-a-number"},
            {"symbol": "HYPEUSDT", "price": "0.00000000"},
            {"symbol": "XRPUSDT", "price": "1.0"},  # not asked for
            "not a dict",
        ]
        opener = FakeOpener(json_response(payload))
        observations, missing = self.batch(["BTC", "ETH", "HYPE"], opener)
        self.assertEqual([o.symbol for o in observations], ["BTC"])
        self.assertEqual(missing, ["ETH", "HYPE"])

    def test_a_rejected_batch_degrades_to_one_request_per_symbol(self):
        # One unlisted symbol makes the *whole* batch answer 400 without naming
        # the offender, so the only way to isolate it is to ask again singly.
        opener = FakeOpener(
            http_error(400, body=b'{"code":-1121,"msg":"Invalid symbol."}'),
            FakeResponse('{"symbol":"BTCUSDT","price":"84394.01000000"}'),
            FakeResponse('{"symbol":"ETHUSDT","price":"2685.35000000"}'),
            http_error(400, body=b'{"code":-1121,"msg":"Invalid symbol."}'),
        )
        observations, missing = self.batch(["BTC", "ETH", "NOPE"], opener)

        self.assertEqual(opener.call_count, 4)
        self.assertEqual([o.symbol for o in observations], ["BTC", "ETH"])
        # Only the genuinely unlisted symbol drops through to the next tier.
        self.assertEqual(missing, ["NOPE"])

    def test_a_server_error_does_not_degrade_to_per_symbol_requests(self):
        # 5xx means the venue is down, not that a symbol is wrong. Degrading
        # here would turn one failure into one failure per configured coin.
        opener = FakeOpener(*[http_error(503)] * 2)
        with self.assertRaises(FetchError) as ctx:
            self.batch(["BTC", "ETH", "HYPE"], opener, max_attempts=2)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(ctx.exception.status, 503)

    def test_fetch_symbol_uses_the_cheap_endpoint(self):
        opener = FakeOpener(FakeResponse('{"symbol":"ETHUSDT","price":"2685.35"}'))
        observation = BinanceClient(BINANCE_URL, opener=opener).fetch_symbol("ETH")
        self.assertEqual(observation.price_usd, 2685.35)
        self.assertEqual(observation.source, "binance")
        # ticker/price rather than ticker/24hr: weight 2 instead of far more,
        # and it carries neither a venue timestamp nor a volume we would keep.
        self.assertEqual(
            opener.urls()[0], f"{BINANCE_URL}/api/v3/ticker/price?symbol=ETHUSDT"
        )


class HyperliquidTests(unittest.TestCase):
    def test_hyperliquid_parses_the_mid_map(self):
        opener = FakeOpener(FakeResponse('{"BTC":"84352.5","HYPE":"91.3015","BAD":"x"}'))
        mids = HyperliquidClient("https://hl.test/info", opener=opener).fetch_mids()
        self.assertEqual(mids, {"BTC": 84352.5, "HYPE": 91.3015})
        self.assertEqual(opener.calls[0][0].method, "POST")


class TierTests(unittest.TestCase):
    """The chain is Binance -> CoinGecko -> Hyperliquid, batched at every tier."""

    def build(self, opener, *, enable_failover=True, now=None, **api_overrides):
        collection = CollectionConfig(
            poll_seconds=10,
            binance_base_url=BINANCE_URL,
            hyperliquid_url="https://hl.test/info",
            enable_failover=enable_failover,
            coins=dict(COINS),
        )
        kwargs = {"opener": opener, "sleep": RecordingSleep()}
        if now is not None:
            kwargs["now"] = now
        return PriceFetcher(api_config(**api_overrides), collection, **kwargs)

    def test_a_healthy_tick_is_one_request(self):
        opener = FakeOpener(json_response(BATCH))
        observations, missing = self.build(opener).fetch_all(COINS)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(missing, [])
        self.assertEqual([o.symbol for o in observations], ["BTC", "ETH", "HYPE"])
        self.assertEqual({o.source for o in observations}, {"binance"})

    def test_configured_order_is_preserved_regardless_of_which_tier_answered(self):
        # BTC from Binance, HYPE from Hyperliquid. The output order must follow
        # the config, not the order the tiers happened to return.
        opener = FakeOpener(
            json_response([{"symbol": "BTCUSDT", "price": "84394.01"}]),
            json_response({"hyperliquid": {"usd": 91.22}}),
            FakeResponse("{}"),  # Hyperliquid: no ETH mid either
        )
        observations, missing = self.build(opener).fetch_all(COINS)
        self.assertEqual([o.symbol for o in observations], ["BTC", "HYPE"])
        self.assertEqual(missing, ["ETH"])

    def test_a_symbol_binance_omits_falls_through_to_coingecko_only(self):
        opener = FakeOpener(
            json_response(BATCH[:2]),
            json_response({"hyperliquid": {"usd": 91.22}}),
        )
        observations, missing = self.build(opener).fetch_all(COINS)

        self.assertEqual(missing, [])
        self.assertEqual(opener.call_count, 2)
        # Only the outstanding symbol is requested from CoinGecko, not all three.
        self.assertIn("ids=hyperliquid", opener.urls()[1])
        by_symbol = {o.symbol: o for o in observations}
        self.assertEqual(by_symbol["HYPE"].source, "coingecko")
        self.assertEqual(by_symbol["HYPE"].price_usd, 91.22)

    def test_the_chain_ends_at_hyperliquid(self):
        opener = FakeOpener(
            *[urllib.error.URLError("down")] * 2,  # Binance exhausts its two attempts
            urllib.error.URLError("down"),  # CoinGecko gets its single failover attempt
            FakeResponse('{"BTC":"84352.5"}'),  # Hyperliquid
        )
        observations, missing = self.build(opener).fetch_all({"BTC": "bitcoin"})

        # Binance is down and CoinGecko is down, so Hyperliquid carries it.
        self.assertEqual([o.source for o in observations], ["hyperliquid"])
        self.assertEqual(observations[0].price_usd, 84352.5)
        self.assertEqual(missing, [])
        # Three requests, not five: the failovers are not retried, because the
        # next tick is ten seconds away and this slot has to end.
        self.assertEqual(opener.call_count, 4)

    def test_a_coin_no_venue_carries_is_reported(self):
        opener = FakeOpener(
            json_response(BATCH[:2]),  # Binance: no HYPEUSDT
            json_response({"bitcoin": {"usd": 1}, "ethereum": {"usd": 2}}),  # no HYPE
            FakeResponse('{"BTC":"84352.5"}'),  # Hyperliquid: no HYPE mid either
        )
        observations, missing = self.build(opener).fetch_all(COINS)
        self.assertEqual(missing, ["HYPE"])
        self.assertEqual({o.symbol for o in observations}, {"BTC", "ETH"})

    def test_failover_can_be_disabled(self):
        opener = FakeOpener(json_response(BATCH[:2]))
        observations, missing = self.build(opener, enable_failover=False).fetch_all(COINS)
        self.assertEqual(missing, ["HYPE"])
        self.assertEqual(opener.call_count, 1)

    def test_a_parked_source_is_skipped_without_a_request(self):
        # The whole point of the cooldown: honour the pause without blocking.
        clock = FakeClock()
        opener = FakeOpener(
            http_error(429, retry_after="60"),  # Binance parks itself for a minute
            json_response({"bitcoin": {"usd": 1.0}}),  # CoinGecko carries the tick
        )
        fetcher = self.build(opener, now=clock, retry_after_cap=2.0)

        observations, _ = fetcher.fetch_all({"BTC": "bitcoin"})
        self.assertEqual([o.source for o in observations], ["coingecko"])
        self.assertEqual(opener.call_count, 2)

        # The parked tick asks CoinGecko only; Binance is not touched at all.
        opener.results.append(json_response({"bitcoin": {"usd": 1.0}}))
        observations, _ = fetcher.fetch_all({"BTC": "bitcoin"})
        self.assertEqual([o.source for o in observations], ["coingecko"])
        self.assertEqual(opener.call_count, 3)
        self.assertNotIn("binance", opener.urls()[-1])

        # Once the pause elapses, the primary is back without any intervention.
        clock.advance(61.0)
        opener.results.append(json_response([{"symbol": "BTCUSDT", "price": "2.0"}]))
        observations, _ = fetcher.fetch_all({"BTC": "bitcoin"})
        self.assertEqual([o.source for o in observations], ["binance"])


if __name__ == "__main__":
    unittest.main()
