"""Price retrieval: Binance first, then CoinGecko and Hyperliquid as failover.

The tiers run in order and each one returns *everything still outstanding* in a
single request, so a healthy tick is one HTTP call regardless of how many coins
are configured:

1. **Binance** -- ``/api/v3/ticker/price?symbols=[...]``, keyless and
   unthrottled for this workload.  Measured at one request per 10 seconds it
   sits at about 0.4% of the published weight budget, and successive responses
   genuinely differ, which is the whole reason it is the primary.
2. **CoinGecko** -- whatever tier 1 could not price.
3. **Hyperliquid** -- whatever is still outstanding.

CoinGecko is no longer primary because it *cannot* be: ``simple/price`` is
cached server-side for one to two minutes, so polling it every ten seconds
writes roughly six duplicate rows per real observation.  A file like that looks
like ten-second resolution while carrying one-minute information, which is worse
than an obviously coarse one.

The fallback is per symbol, not all-or-nothing, so one unlisted coin does not
drag the others off the primary source.  Every observation records which venue
produced it, because the venues do not agree: measured within seconds of each
other, CoinGecko and CoinPaprika differed by ~0.04% on BTC.  A source change
mid-series is therefore a real, visible discontinuity rather than a silent one.

Network resilience amounts to: retry only what could plausibly succeed later.
A 429 or a 5xx is retried with exponential backoff; a 400 or 404 is not, because
retrying a bad symbol just burns the request budget.  At a ten-second cadence a
long ``Retry-After`` cannot be slept through -- it would block several slots --
and ignoring it is what escalates a 429 into a 418 IP ban, so the source is
*parked* for the interval instead and the loop keeps ticking.
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
from .logging_setup import RateLimitedLogger

logger = logging.getLogger("scraper.fetch")

#: Statuses worth another attempt. Everything else is a definitive answer, and
#: retrying it wastes the shared request budget.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Per-symbol gaps repeat on every tick for as long as they last, and a symbol
#: that is unavailable is usually unavailable for hours. Keyed by (symbol, venue)
#: so one dead symbol cannot mask a second one appearing.
_REPEAT = RateLimitedLogger(every=300.0)


class FetchError(Exception):
    """A source could not be read, or returned something unusable.

    ``status`` carries the HTTP status when there was one, and that distinction
    is load-bearing.  A 400 from Binance's batch endpoint means "one of those
    symbols is not listed" and should degrade to per-symbol requests; a 5xx or a
    network error means the venue is down and the whole tier should be skipped.
    Without it the two are the same exception, and one typo in ``[coins]`` is
    indistinguishable from an outage.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Observation:
    """One price reading for one coin at one instant.

    Only the price and its provenance: no keyless venue API publishes a *global*
    market cap, and Binance's ``quoteVolume`` is Binance-only -- about 27x
    narrower than CoinGecko's global figure -- so a volume column would silently
    change meaning the moment the primary changed.  ``source`` is what makes a
    venue switch visible in the data instead of looking like a price move.
    """

    symbol: str
    price_usd: float
    source: str


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
    finally:
        # An HTTPError owns the response body. Nothing else closes it, so at one
        # request every ten seconds an unclosed error leaks a handle per 429.
        try:
            error.close()
        except Exception:  # noqa: BLE001 - cleanup must never mask the error
            pass


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
        now=time.monotonic,
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
        # Monotonic, not wall clock: a clock step must not strand a source in a
        # cooldown that never expires, or expire one that has not elapsed.
        self._now = now
        self._cooldown_until = 0.0
        self._logger = logger

    def _backoff_delay(self, attempt: int) -> float:
        delay = self._backoff_initial * (self._backoff_multiplier ** (attempt - 1))
        return min(delay, self._backoff_max)

    def unavailable_for(self) -> float:
        """Seconds this source is parked for. ``0.0`` means it is usable now."""
        return max(0.0, self._cooldown_until - self._now())

    def _park(self, seconds: float) -> None:
        self._cooldown_until = max(self._cooldown_until, self._now() + seconds)

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
        last_status: int | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    raw = response.read()
                return self._decode(raw)
            except urllib.error.HTTPError as exc:
                detail = _safe_body(exc)
                last_status = exc.code
                if exc.code not in _RETRYABLE_STATUS:
                    raise FetchError(
                        f"HTTP {exc.code} from {url}: {detail}", status=exc.code
                    ) from None
                last_error = f"HTTP {exc.code}: {detail}"
                retry_after = _retry_after_seconds(exc)
                if retry_after is None and exc.code == 429:
                    # A 429 with no Retry-After still means "stop"; fall back to
                    # the cap so the source is parked for something rather than
                    # polled again immediately.
                    retry_after = self._retry_after_cap
                if retry_after is not None:
                    # The pause the server asked for is honoured in full, just not
                    # by sleeping through it: at a 10s cadence a 60s Retry-After
                    # is six whole slots, and capping it while carrying on polling
                    # is exactly what escalates a 429 into a 418 IP ban. Park the
                    # source and let the loop keep time.
                    self._park(retry_after)
                    if retry_after > self._retry_after_cap:
                        # Longer than this tick could usefully wait for, so trying
                        # again now would be the hammering we were told to stop.
                        last_error = f"{last_error} (source parked for {retry_after:.0f}s)"
                        break
                # Anything reaching here is at or under the cap, so it is short
                # enough to sleep; a longer one already broke out above.
                delay = (
                    retry_after if retry_after is not None else self._backoff_delay(attempt)
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
                # Keep a single tick bounded: the next slot is ten seconds away,
                # and running into it costs a row that nothing will backfill.
                last_error = f"{last_error} (gave up: retry exceeds backoff budget)"
                break
            self._logger.debug(
                "retrying %s in %.1fs (attempt %d/%d)", url, delay, attempt, self._max_attempts
            )
            self._sleep(delay)
            slept += delay

        raise FetchError(
            f"all {self._max_attempts} attempts failed for {url}: {last_error}",
            status=last_status,
        )

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
        # No include_market_cap / include_24hr_vol: neither column is kept, and
        # asking for them would not make this endpoint any fresher -- the
        # one-to-two minute server-side cache is why this is the failover.
        query = urllib.parse.urlencode(
            {"ids": ",".join(ids), "vs_currencies": vs_currency},
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
                _REPEAT.log(
                    logger,
                    logging.WARNING,
                    (symbol, "coingecko"),
                    "coingecko returned no entry for %s (%s)",
                    symbol,
                    coin_id,
                )
                missing.append(coin_id)
                continue
            price = _finite(entry.get(vs_currency))
            if price is None:
                _REPEAT.log(
                    logger,
                    logging.WARNING,
                    (symbol, "coingecko"),
                    "coingecko returned no usable %s price for %s (%r)",
                    vs_currency,
                    symbol,
                    entry.get(vs_currency),
                )
                missing.append(coin_id)
                continue
            observations.append(
                Observation(symbol=symbol, price_usd=price, source="coingecko")
            )
        return observations, missing


class BinanceClient(_HttpClient):
    """Failover for symbols Binance lists, as ``<SYMBOL>USDT`` spot pairs."""

    def __init__(self, base_url: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._base_url = base_url.rstrip("/")

    def fetch_batch(self, symbols: list[str]) -> tuple[list[Observation], list[str]]:
        """Price every symbol in one request. Returns ``(observations, missing)``.

        ``ticker/price`` is the right endpoint here rather than ``ticker/24hr``:
        it carries no venue timestamp and no volume, so it is the cheapest call
        Binance publishes (weight 4 for the whole batch) and it is not cached,
        which is exactly what ten-second resolution needs.

        A symbol Binance does not list makes the *entire* batch answer
        ``400 -1121`` without naming the offender, so a 400 or 404 degrades to
        one request per symbol. That costs more but happens only when something
        is genuinely misconfigured, and it drops the one bad symbol instead of
        the whole venue.
        """
        if not symbols:
            return [], []

        # separators=(",", ":") is required, not cosmetic: json.dumps' default
        # is ", " with a space, and Binance's symbol validator rejects the space
        # with "400 -1100 Illegal characters".
        query = urllib.parse.quote(
            json.dumps([f"{symbol}USDT" for symbol in symbols], separators=(",", ":")),
            safe="",
        )
        url = f"{self._base_url}/api/v3/ticker/price?symbols={query}"

        try:
            payload = self._request_json(url)
        except FetchError as exc:
            if exc.status not in (400, 404):
                raise
            self._logger.warning(
                "binance rejected the whole symbol batch (%s); retrying one at a time",
                exc,
            )
            return self._fetch_individually(symbols)

        if not isinstance(payload, list):
            raise FetchError(
                f"binance returned {type(payload).__name__}, expected a list"
            )

        priced: dict[str, Observation] = {}
        for row in payload:
            pair = row.get("symbol") if isinstance(row, dict) else None
            if not isinstance(pair, str) or not pair.endswith("USDT"):
                continue
            # Prices arrive as JSON strings ("84386.01000000"), not numbers.
            price = _finite(row.get("price"), allow_str=True)
            if price is None:
                continue
            symbol = pair[: -len("USDT")]
            priced[symbol] = Observation(symbol=symbol, price_usd=price, source="binance")

        # Emit in the order asked for, and report anything the response omitted.
        # A symbol that came back unusable is as missing as one that did not
        # come back at all, and both belong to the next tier.
        observations: list[Observation] = []
        missing: list[str] = []
        for symbol in symbols:
            if symbol in priced:
                observations.append(priced[symbol])
            else:
                missing.append(symbol)
        return observations, missing

    def _fetch_individually(
        self, symbols: list[str]
    ) -> tuple[list[Observation], list[str]]:
        """Fall back to one request per symbol after a rejected batch."""
        observations: list[Observation] = []
        missing: list[str] = []
        for symbol in symbols:
            try:
                observations.append(self.fetch_symbol(symbol))
            except FetchError as exc:
                # This warning is the only place a misconfigured symbol is ever
                # named; the batch error above cannot say which one it was.
                _REPEAT.log(
                    self._logger,
                    logging.WARNING,
                    (symbol, "binance"),
                    "binance has no %sUSDT (%s)",
                    symbol,
                    exc,
                )
                missing.append(symbol)
        return observations, missing

    def fetch_symbol(self, symbol: str) -> Observation:
        url = f"{self._base_url}/api/v3/ticker/price?symbol={symbol}USDT"
        # A symbol Binance does not list answers 400, which is non-retryable and
        # becomes a FetchError; the caller then tries Hyperliquid.
        payload = self._request_json(url)
        if not isinstance(payload, dict):
            raise FetchError(f"binance returned {type(payload).__name__}, expected an object")
        price = _finite(payload.get("price"), allow_str=True)
        if price is None:
            raise FetchError(f"binance returned no usable price for {symbol}USDT")
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
    api: ApiConfig, *, opener, sleep, now, logger: logging.Logger, max_attempts: int | None = None
) -> dict:
    """Transport settings for one client.

    Each client gets its own dict rather than sharing one.  The primary is worth
    a retry; a failover is not.  A failover that failed has already told you
    everything it can, and the next tick is ten seconds away -- spending the
    slot on a second attempt only pushes the *next* primary request late, which
    is a row that nothing will ever backfill.
    """
    return {
        "opener": opener,
        "sleep": sleep,
        "now": now,
        "logger": logger,
        "timeout": api.timeout_seconds,
        "max_attempts": api.max_attempts if max_attempts is None else max_attempts,
        "backoff_initial": api.backoff_initial,
        "backoff_multiplier": api.backoff_multiplier,
        "backoff_max": api.backoff_max,
        "retry_after_cap": api.retry_after_cap,
        "backoff_budget": api.backoff_budget,
        "user_agent": api.user_agent,
    }


class PriceFetcher:
    """Binance, then CoinGecko, then Hyperliquid -- batched at every tier."""

    def __init__(
        self,
        api: ApiConfig,
        collection: CollectionConfig,
        *,
        opener=urllib.request.urlopen,
        sleep=time.sleep,
        now=time.monotonic,
        logger: logging.Logger = logger,
    ) -> None:
        primary = _transport_kwargs(api, opener=opener, sleep=sleep, now=now, logger=logger)
        failover = _transport_kwargs(
            api,
            opener=opener,
            sleep=sleep,
            now=now,
            logger=logger,
            max_attempts=api.failover_max_attempts,
        )
        self._binance = BinanceClient(collection.binance_base_url, **primary)
        self._coingecko = CoinGeckoClient(api.base_url, api.vs_currency, **failover)
        self._hyperliquid = HyperliquidClient(collection.hyperliquid_url, **failover)
        self._failover_enabled = collection.enable_failover
        self._logger = logger

    def fetch_all(self, coins: dict[str, str]) -> tuple[list[Observation], list[str]]:
        """Return ``(observations, symbols that yielded no price at all)``.

        Normally exactly one HTTP request.  Each tier takes everything still
        outstanding, so the cost does not grow with the number of coins.
        """
        found: dict[str, Observation] = {}
        pending = list(coins)

        pending = self._tier_binance(pending, found)
        if pending and self._failover_enabled:
            pending = self._tier_coingecko(pending, coins, found)
        if pending and self._failover_enabled:
            pending = self._tier_hyperliquid(pending, found)

        # Rebuilt in configured order so the tick's output is deterministic
        # regardless of which tier happened to answer for which coin.
        observations = [found[symbol] for symbol in coins if symbol in found]
        missing = [symbol for symbol in coins if symbol not in found]
        return observations, missing

    def _tier_binance(
        self, pending: list[str], found: dict[str, Observation]
    ) -> list[str]:
        wait = self._binance.unavailable_for()
        if wait:
            self._logger.info(
                "binance is rate limited for another %.0fs; %d symbol(s) go to the next tier",
                wait,
                len(pending),
            )
            return pending
        try:
            observations, missing = self._binance.fetch_batch(pending)
        except FetchError as exc:
            self._logger.warning(
                "binance unavailable (%s); %d symbol(s) go to the next tier",
                exc,
                len(pending),
            )
            return pending
        found.update((obs.symbol, obs) for obs in observations)
        return missing

    def _tier_coingecko(
        self, pending: list[str], coins: dict[str, str], found: dict[str, Observation]
    ) -> list[str]:
        wait = self._coingecko.unavailable_for()
        if wait:
            self._logger.info(
                "coingecko is rate limited for another %.0fs; %d symbol(s) go to the next tier",
                wait,
                len(pending),
            )
            return pending
        # CoinGecko keys its payload by coin id, so the mapping is inverted here
        # rather than stored on Observation, which no longer carries one.
        symbol_by_id = {coins[symbol]: symbol for symbol in pending}
        try:
            observations, missing_ids = self._coingecko.fetch(symbol_by_id)
        except FetchError as exc:
            self._logger.warning("coingecko unavailable (%s)", exc)
            return pending
        found.update((obs.symbol, obs) for obs in observations)
        return [symbol_by_id[cid] for cid in missing_ids if cid in symbol_by_id]

    def _tier_hyperliquid(
        self, pending: list[str], found: dict[str, Observation]
    ) -> list[str]:
        wait = self._hyperliquid.unavailable_for()
        if wait:
            self._logger.info(
                "hyperliquid is rate limited for another %.0fs; %d symbol(s) unresolved",
                wait,
                len(pending),
            )
            return pending
        try:
            mids = self._hyperliquid.fetch_mids()
        except FetchError as exc:
            self._logger.warning("hyperliquid unavailable (%s)", exc)
            return pending

        unresolved: list[str] = []
        for symbol in pending:
            price = mids.get(symbol)
            if price is None:
                _REPEAT.log(
                    self._logger,
                    logging.WARNING,
                    (symbol, "hyperliquid"),
                    "hyperliquid listed no mid price for %s",
                    symbol,
                )
                unresolved.append(symbol)
            else:
                found[symbol] = Observation(
                    symbol=symbol, price_usd=price, source="hyperliquid"
                )
        return unresolved
