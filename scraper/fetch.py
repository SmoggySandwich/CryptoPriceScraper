"""Price retrieval: CoinGecko first, then live venue failover.

CoinGecko's ``simple/price`` returns all configured coins in a single request,
so a normal tick costs one HTTP call.  Any coin it fails to return -- or every
coin, if the request itself fails -- falls back per coin:

* Binance, for symbols it lists (BTC, ETH), and
* Hyperliquid, for symbols Binance does not carry (HYPE).

The fallback is per coin, not all-or-nothing, so one missing coin does not drag
the others off the primary source.  Every observation records which venue
produced it, because the venues do not agree: measured within seconds of each
other, CoinGecko and CoinPaprika differed by ~0.04% on BTC.  A source change
mid-series is therefore a real, visible discontinuity rather than a silent one.

Network resilience amounts to: retry only what could plausibly succeed later.
A 429 or a 5xx is retried with exponential backoff; a 400 or 404 is not, because
retrying a bad coin id just burns the request budget.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import ApiConfig, CollectionConfig

logger = logging.getLogger("scraper.fetch")

#: Statuses worth another attempt. Everything else is a definitive answer, and
#: retrying it wastes the shared request budget.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class FetchError(Exception):
    """A source could not be read, or returned something unusable."""


@dataclass(frozen=True)
class Observation:
    """One price reading for one coin at one instant."""

    symbol: str
    price_usd: float
    #: Populated for CoinGecko rows; empty for venue rows, which have no id.
    coin_id: str = ""
    market_cap_usd: float | None = None
    vol_24h_usd: float | None = None
    source: str = "coingecko"


def _reject_constant(name: str):
    """Reject the bare ``NaN``/``Infinity`` literals Python's json accepts.

    They are a non-standard extension, and a ``NaN`` written into the price
    column would silently poison every later mean, plot and backtest.
    """
    raise ValueError(f"non-finite JSON constant {name!r}")


def _finite(value, *, allow_zero: bool = False, allow_str: bool = False) -> float | None:
    """Coerce a JSON value to a usable positive float, or ``None``.

    ``bool`` is rejected explicitly: ``isinstance(True, int)`` is ``True`` in
    Python, so ``"usd": true`` would otherwise become ``1.0``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not allow_str:
            return None
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if number < 0 or (number == 0.0 and not allow_zero):
        return None
    return number


def _retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    headers = getattr(error, "headers", None)
    raw = headers.get("Retry-After") if headers else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except (AttributeError, ValueError):
        # The HTTP-date form; fall back to ordinary backoff.
        return None


def _safe_body(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", "replace")[:200]
    except Exception:  # noqa: BLE001 - diagnostics must never mask the error
        return "<unreadable>"


class _HttpClient:
    """Shared JSON-over-HTTP behaviour: headers, retry, backoff, budget."""

    def __init__(
        self,
        *,
        opener=urllib.request.urlopen,
        sleep=time.sleep,
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff_initial: float = 1.0,
        backoff_multiplier: float = 2.0,
        backoff_max: float = 10.0,
        retry_after_cap: float = 60.0,
        backoff_budget: float = 75.0,
        user_agent: str = "CryptoPriceScraper/1.0",
        logger: logging.Logger = logger,
    ) -> None:
        self._opener = opener
        self._sleep = sleep
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff_initial = backoff_initial
        self._backoff_multiplier = backoff_multiplier
        self._backoff_max = backoff_max
        self._retry_after_cap = retry_after_cap
        self._backoff_budget = backoff_budget
        self._user_agent = user_agent
        self._logger = logger

    def _backoff_delay(self, attempt: int) -> float:
        delay = self._backoff_initial * (self._backoff_multiplier ** (attempt - 1))
        return min(delay, self._backoff_max)

    def _request_json(self, url: str, *, data: dict | None = None, method: str = "GET") -> dict:
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
            # Deliberately no Accept-Encoding: urllib will not decompress a
            # gzip response for us, so asking for one would yield mojibake.
        }
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        slept = 0.0
        last_error = "no attempt made"

        for attempt in range(1, self._max_attempts + 1):
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    raw = response.read()
                return self._decode(raw)
            except urllib.error.HTTPError as exc:
                detail = _safe_body(exc)
                if exc.code not in _RETRYABLE_STATUS:
                    raise FetchError(f"HTTP {exc.code} from {url}: {detail}") from None
                last_error = f"HTTP {exc.code}: {detail}"
                retry_after = _retry_after_seconds(exc)
                delay = (
                    min(retry_after, self._retry_after_cap)
                    if retry_after is not None
                    else self._backoff_delay(attempt)
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # URLError covers DNS failure, refused connections, TLS problems
                # (including antivirus HTTPS interception) and socket timeouts.
                last_error = f"{type(exc).__name__}: {exc}"
                delay = self._backoff_delay(attempt)
            except ValueError as exc:
                # json.JSONDecodeError is a ValueError subclass. Retry, because
                # a proxy returning an HTML error page is a transient condition.
                last_error = f"unreadable response: {exc}"
                delay = self._backoff_delay(attempt)

            if attempt == self._max_attempts:
                break
            if slept + delay > self._backoff_budget:
                # Keep a single tick bounded: the whole point of a 5 minute
                # interval is that one slow tick must not run into the next.
                last_error = f"{last_error} (gave up: retry exceeds backoff budget)"
                break
            self._logger.debug(
                "retrying %s in %.1fs (attempt %d/%d)", url, delay, attempt, self._max_attempts
            )
            self._sleep(delay)
            slept += delay

        raise FetchError(f"all {self._max_attempts} attempts failed for {url}: {last_error}")

    @staticmethod
    def _decode(raw: bytes) -> dict:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"response was not UTF-8 ({exc})") from None
        try:
            return json.loads(text, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            # The body matters: a corporate proxy or AV interception returns an
            # HTML page, which presents as "malformed JSON" and nothing else.
            raise ValueError(f"malformed JSON ({exc}); body starts {text[:200]!r}") from None


class CoinGeckoClient(_HttpClient):
    """The primary source: all configured coins in one request."""

    def __init__(self, base_url: str, vs_currency: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._base_url = base_url.rstrip("/")
        self._vs_currency = vs_currency

    @staticmethod
    def build_url(base_url: str, ids: list[str], vs_currency: str) -> str:
        query = urllib.parse.urlencode(
            {
                "ids": ",".join(ids),
                "vs_currencies": vs_currency,
                "include_market_cap": "true",
                "include_24hr_vol": "true",
            },
            safe=",",  # keep the id list readable, as CoinGecko documents it
        )
        return f"{base_url.rstrip('/')}/simple/price?{query}"

    def fetch(self, symbol_by_id: dict[str, str]) -> tuple[list[Observation], list[str]]:
        """Return ``(observations, missing_coin_ids)``.

        Raises :class:`FetchError` only when the request itself failed or the
        payload was an error object; per-coin gaps are reported as missing ids.
        """
        url = self.build_url(self._base_url, list(symbol_by_id), self._vs_currency)
        payload = self._request_json(url)
        return self.parse_payload(payload, symbol_by_id, self._vs_currency, self._logger)

    @staticmethod
    def parse_payload(
        payload: dict,
        symbol_by_id: dict[str, str],
        vs_currency: str,
        logger: logging.Logger = logger,
    ) -> tuple[list[Observation], list[str]]:
        if not isinstance(payload, dict):
            raise FetchError(f"expected a JSON object, got {type(payload).__name__}")

        status = payload.get("status")
        if isinstance(status, dict) and status.get("error_code") is not None:
            # HTTP 200 carrying an error body. Without this check, a quota or
            # auth error is indistinguishable from "every coin was delisted",
            # and the run would silently record nothing while looking healthy.
            raise FetchError(
                f"api error {status.get('error_code')}: {status.get('error_message')}"
            )

        observations: list[Observation] = []
        missing: list[str] = []
        for coin_id, symbol in symbol_by_id.items():
            entry = payload.get(coin_id)
            if not isinstance(entry, dict):
                logger.warning("coingecko returned no entry for %s (%s)", symbol, coin_id)
                missing.append(coin_id)
                continue
            price = _finite(entry.get(vs_currency))
            if price is None:
                logger.warning(
                    "coingecko returned no usable %s price for %s (%r)",
                    vs_currency,
                    symbol,
                    entry.get(vs_currency),
                )
                missing.append(coin_id)
                continue
            observations.append(
                Observation(
                    symbol=symbol,
                    price_usd=price,
                    coin_id=coin_id,
                    market_cap_usd=_finite(
                        entry.get(f"{vs_currency}_market_cap"), allow_zero=True
                    ),
                    vol_24h_usd=_finite(
                        entry.get(f"{vs_currency}_24h_vol"), allow_zero=True
                    ),
                    source="coingecko",
                )
            )
        return observations, missing


class BinanceClient(_HttpClient):
    """Failover for symbols Binance lists, as ``<SYMBOL>USDT`` spot pairs."""

    def __init__(self, base_url: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._base_url = base_url.rstrip("/")

    def fetch_symbol(self, symbol: str) -> Observation:
        url = f"{self._base_url}/api/v3/ticker/24hr?symbol={symbol}USDT"
        # A symbol Binance does not list answers 400, which is non-retryable and
        # becomes a FetchError; the caller then tries Hyperliquid.
        payload = self._request_json(url)
        if not isinstance(payload, dict):
            raise FetchError(f"binance returned {type(payload).__name__}, expected an object")
        price = _finite(payload.get("lastPrice"), allow_str=True)
        if price is None:
            raise FetchError(f"binance returned no usable price for {symbol}USDT")
        # quoteVolume is deliberately not recorded. It is Binance-only volume
        # (~814M for BTC) while CoinGecko's figure is global (~21.9B), a ~27x
        # difference for the same column name. Leaving it blank keeps the
        # column meaning one thing across the whole dataset.
        return Observation(symbol=symbol, price_usd=price, source="binance")


class HyperliquidClient(_HttpClient):
    """Failover for symbols Binance does not carry (HYPE)."""

    def __init__(self, url: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._url = url

    def fetch_mids(self) -> dict[str, float]:
        payload = self._request_json(self._url, data={"type": "allMids"}, method="POST")
        if not isinstance(payload, dict):
            raise FetchError(f"hyperliquid returned {type(payload).__name__}, expected an object")
        mids: dict[str, float] = {}
        for coin, raw in payload.items():
            price = _finite(raw, allow_str=True)
            if price is not None:
                mids[coin] = price
        return mids


def _transport_kwargs(
    api: ApiConfig, *, opener, sleep, logger: logging.Logger
) -> dict:
    return {
        "opener": opener,
        "sleep": sleep,
        "logger": logger,
        "timeout": api.timeout_seconds,
        "max_attempts": api.max_attempts,
        "backoff_initial": api.backoff_initial,
        "backoff_multiplier": api.backoff_multiplier,
        "backoff_max": api.backoff_max,
        "retry_after_cap": api.retry_after_cap,
        "backoff_budget": api.backoff_budget,
        "user_agent": api.user_agent,
    }


class PriceFetcher:
    """CoinGecko with per-coin failover to Binance and Hyperliquid."""

    def __init__(
        self,
        api: ApiConfig,
        collection: CollectionConfig,
        *,
        opener=urllib.request.urlopen,
        sleep=time.sleep,
        logger: logging.Logger = logger,
    ) -> None:
        transport = _transport_kwargs(api, opener=opener, sleep=sleep, logger=logger)
        self._coingecko = CoinGeckoClient(api.base_url, api.vs_currency, **transport)
        self._binance = BinanceClient(collection.binance_base_url, **transport)
        self._hyperliquid = HyperliquidClient(collection.hyperliquid_url, **transport)
        self._failover_enabled = collection.enable_failover
        self._logger = logger

    def fetch_all(self, coins: dict[str, str]) -> tuple[list[Observation], list[str]]:
        """Return ``(observations, symbols that yielded no price at all)``."""
        symbol_by_id = {coin_id: symbol for symbol, coin_id in coins.items()}

        try:
            observations, missing_ids = self._coingecko.fetch(symbol_by_id)
        except FetchError as exc:
            self._logger.warning("coingecko unavailable (%s); falling back to venues", exc)
            observations, missing_ids = [], list(symbol_by_id)

        missing_symbols = [symbol_by_id[cid] for cid in missing_ids if cid in symbol_by_id]
        if not missing_symbols:
            return observations, []
        if not self._failover_enabled:
            return observations, missing_symbols

        fallback, unresolved = self._failover(missing_symbols)
        return observations + fallback, unresolved

    def _failover(self, symbols: list[str]) -> tuple[list[Observation], list[str]]:
        observations: list[Observation] = []
        remaining: list[str] = []

        # Binance first: deeper venue. A symbol it does not list fails its own
        # request and drops through to Hyperliquid.
        for symbol in symbols:
            try:
                observations.append(self._binance.fetch_symbol(symbol))
            except FetchError as exc:
                self._logger.info(
                    "binance has no %sUSDT (%s); trying hyperliquid", symbol, exc
                )
                remaining.append(symbol)

        if not remaining:
            return observations, []

        try:
            mids = self._hyperliquid.fetch_mids()
        except FetchError as exc:
            self._logger.warning("hyperliquid unavailable (%s)", exc)
            return observations, remaining

        unresolved: list[str] = []
        for symbol in remaining:
            price = mids.get(symbol)
            if price is None:
                self._logger.warning("hyperliquid listed no mid price for %s", symbol)
                unresolved.append(symbol)
            else:
                observations.append(
                    Observation(symbol=symbol, price_usd=price, source="hyperliquid")
                )
        return observations, unresolved
