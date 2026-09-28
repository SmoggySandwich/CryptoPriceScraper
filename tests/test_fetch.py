"""Primary fetch, retry policy, parsing hazards and venue failover."""

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

FULL_PAYLOAD = {
    "bitcoin": {"usd": 84386, "usd_market_cap": 1.695e12, "usd_24h_vol": 2.197e10},
    "ethereum": {"usd": 2685.13, "usd_market_cap": 3.27e11, "usd_24h_vol": 8.8e9},
    "hyperliquid": {"usd": 91.22, "usd_market_cap": 2.03e10, "usd_24h_vol": 7.5e8},
}


def client(opener, sleep=None, **overrides):
    settings = dict(
        opener=opener,
        sleep=sleep or RecordingSleep(),
        timeout=10.0,
        max_attempts=3,
        backoff_initial=1.0,
        backoff_multiplier=2.0,
        backoff_max=10.0,
        retry_after_cap=60.0,
        backoff_budget=75.0,
    )
    settings.update(overrides)
    return CoinGeckoClient(BASE_URL, "usd", **settings)


def json_response(payload) -> FakeResponse:
    return FakeResponse(json.dumps(payload))


class UrlTests(unittest.TestCase):
    def test_builds_the_documented_url(self):
        # Kept literal: the commas must stay unencoded or CoinGecko parses one
        # giant coin id instead of a list.
        self.assertEqual(
            CoinGeckoClient.build_url(BASE_URL, list(BY_ID), "usd"),
            f"{BASE_URL}/simple/price?ids=bitcoin,ethereum,hyperliquid"
            f"&vs_currencies=usd&include_market_cap=true&include_24hr_vol=true",
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
        self.assertEqual(bitcoin.market_cap_usd, 1.695e12)
        self.assertEqual(bitcoin.source, "coingecko")

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

    def test_missing_optional_fields_leave_cap_and_volume_unset(self):
        observations, _ = self.parse({"bitcoin": {"usd": 1}})
        self.assertEqual(observations[0].price_usd, 1.0)
        self.assertIsNone(observations[0].market_cap_usd)
        self.assertIsNone(observations[0].vol_24h_usd)

    def test_a_real_zero_cap_is_preserved(self):
        observations, _ = self.parse({"bitcoin": {"usd": 1, "usd_market_cap": 0}})
        self.assertEqual(observations[0].market_cap_usd, 0.0)

    def test_non_object_payload_is_rejected(self):
        with self.assertRaises(FetchError):
            self.parse(["not", "an", "object"])


class WireParsingTests(unittest.TestCase):
    def test_bare_nan_literal_is_rejected_at_the_wire(self):
        # Python's json accepts NaN as a non-standard extension. A NaN in the
        # price column would poison every downstream mean.
        opener = FakeOpener(*[FakeResponse('{"bitcoin": {"usd": NaN}}')] * 3)
        with self.assertRaises(FetchError) as ctx:
            client(opener).fetch(BY_ID)
        self.assertIn("non-finite", str(ctx.exception))

    def test_malformed_json_reports_the_body(self):
        opener = FakeOpener(*[FakeResponse("<html>proxy error</html>")] * 3)
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
        observations, _ = client(opener, sleep).fetch(BY_ID)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(len(observations), 3)
        # Exponential: 1.0 then 2.0.
        self.assertEqual(sleep.delays, [1.0, 2.0])

    def test_honours_retry_after_on_429(self):
        sleep = RecordingSleep()
        opener = FakeOpener(
            http_error(429, retry_after="7"),
            json_response(FULL_PAYLOAD),
        )
        client(opener, sleep).fetch(BY_ID)
        self.assertEqual(sleep.delays, [7.0])

    def test_retry_after_is_capped(self):
        sleep = RecordingSleep()
        opener = FakeOpener(
            http_error(429, retry_after="9999"),
            json_response(FULL_PAYLOAD),
        )
        client(opener, sleep, retry_after_cap=60.0).fetch(BY_ID)
        self.assertEqual(sleep.delays, [60.0])

    def test_a_client_error_is_not_retried(self):
        # Retrying a bad coin id or a bad request just burns the request budget.
        opener = FakeOpener(http_error(404))
        with self.assertRaises(FetchError):
            client(opener).fetch(BY_ID)
        self.assertEqual(opener.call_count, 1)

    def test_server_errors_are_retried(self):
        opener = FakeOpener(http_error(503), json_response(FULL_PAYLOAD))
        client(opener).fetch(BY_ID)
        self.assertEqual(opener.call_count, 2)

    def test_gives_up_within_the_backoff_budget(self):
        # A tick must not be able to run into the next one.
        sleep = RecordingSleep()
        opener = FakeOpener(*[urllib.error.URLError("down")] * 5)
        with self.assertRaises(FetchError) as ctx:
            client(opener, sleep, backoff_budget=0.5).fetch(BY_ID)
        self.assertIn("budget", str(ctx.exception))
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(sleep.delays, [])

    def test_timeout_reaches_the_transport(self):
        opener = FakeOpener(json_response(FULL_PAYLOAD))
        client(opener, timeout=7.5).fetch(BY_ID)
        self.assertEqual(opener.calls[0][1], 7.5)

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


class VenueClientTests(unittest.TestCase):
    def test_binance_parses_a_string_price(self):
        opener = FakeOpener(FakeResponse('{"symbol":"BTCUSDT","lastPrice":"84394.01000000"}'))
        observation = BinanceClient("https://api.binance.com", opener=opener).fetch_symbol("BTC")
        self.assertEqual(observation.price_usd, 84394.01)
        self.assertEqual(observation.source, "binance")
        # Binance-only volume is deliberately not recorded; the column means
        # global volume everywhere else.
        self.assertIsNone(observation.vol_24h_usd)
        self.assertIsNone(observation.market_cap_usd)

    def test_binance_url_uses_the_usdt_pair(self):
        opener = FakeOpener(FakeResponse('{"lastPrice":"1"}'))
        BinanceClient("https://api.binance.com", opener=opener).fetch_symbol("ETH")
        self.assertEqual(
            opener.urls()[0], "https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDT"
        )

    def test_hyperliquid_parses_the_mid_map(self):
        opener = FakeOpener(FakeResponse('{"BTC":"84352.5","HYPE":"91.3015","BAD":"x"}'))
        mids = HyperliquidClient("https://hl.test/info", opener=opener).fetch_mids()
        self.assertEqual(mids, {"BTC": 84352.5, "HYPE": 91.3015})
        self.assertEqual(opener.calls[0][0].method, "POST")


class FailoverTests(unittest.TestCase):
    def build(self, opener, *, enable_failover=True):
        collection = CollectionConfig(
            poll_seconds=300,
            binance_base_url="https://api.binance.com",
            hyperliquid_url="https://hl.test/info",
            enable_failover=enable_failover,
            coins=dict(COINS),
        )
        return PriceFetcher(
            api_config(), collection, opener=opener, sleep=RecordingSleep()
        )

    def test_no_failover_calls_when_the_primary_succeeds(self):
        opener = FakeOpener(json_response(FULL_PAYLOAD))
        observations, missing = self.build(opener).fetch_all(COINS)
        self.assertEqual(len(observations), 3)
        self.assertEqual(missing, [])
        self.assertEqual(opener.call_count, 1)

    def test_an_omitted_coin_falls_over_per_coin(self):
        # CoinGecko returns BTC and ETH but not HYPE. Binance does not list
        # HYPE, so that one request 400s and drops through to Hyperliquid.
        partial = {k: FULL_PAYLOAD[k] for k in ("bitcoin", "ethereum")}
        opener = FakeOpener(
            json_response(partial),
            http_error(400, body=b'{"msg":"Invalid symbol."}'),
            FakeResponse('{"BTC":"84352.5","HYPE":"91.3015"}'),
        )
        observations, missing = self.build(opener).fetch_all(COINS)

        self.assertEqual(missing, [])
        by_symbol = {o.symbol: o for o in observations}
        self.assertEqual(by_symbol["BTC"].source, "coingecko")
        self.assertEqual(by_symbol["ETH"].source, "coingecko")
        self.assertEqual(by_symbol["HYPE"].source, "hyperliquid")
        self.assertEqual(by_symbol["HYPE"].price_usd, 91.3015)
        # Venue rows leave the global-only columns blank.
        self.assertIsNone(by_symbol["HYPE"].market_cap_usd)
        self.assertIsNone(by_symbol["HYPE"].vol_24h_usd)

    def test_total_primary_failure_fails_over_every_coin(self):
        opener = FakeOpener(
            *[urllib.error.URLError("down")] * 3,  # CoinGecko exhausts its attempts
            FakeResponse('{"lastPrice":"84394.01"}'),  # Binance BTC
            FakeResponse('{"lastPrice":"2685.35"}'),  # Binance ETH
            http_error(400),  # Binance has no HYPEUSDT
            FakeResponse('{"HYPE":"91.3015"}'),  # Hyperliquid
        )
        observations, missing = self.build(opener).fetch_all(COINS)
        sources = {o.symbol: o.source for o in observations}
        self.assertEqual(sources, {"BTC": "binance", "ETH": "binance", "HYPE": "hyperliquid"})
        self.assertEqual(missing, [])

    def test_a_coin_no_venue_carries_is_reported(self):
        partial = {k: FULL_PAYLOAD[k] for k in ("bitcoin", "ethereum")}
        opener = FakeOpener(
            json_response(partial),
            http_error(400),  # Binance: no HYPEUSDT
            FakeResponse('{"BTC":"84352.5"}'),  # Hyperliquid: no HYPE mid either
        )
        observations, missing = self.build(opener).fetch_all(COINS)
        self.assertEqual(missing, ["HYPE"])
        self.assertEqual({o.symbol for o in observations}, {"BTC", "ETH"})

    def test_failover_can_be_disabled(self):
        partial = {k: FULL_PAYLOAD[k] for k in ("bitcoin", "ethereum")}
        opener = FakeOpener(json_response(partial))
        observations, missing = self.build(opener, enable_failover=False).fetch_all(COINS)
        self.assertEqual(missing, ["HYPE"])
        self.assertEqual(opener.call_count, 1)


if __name__ == "__main__":
    unittest.main()
