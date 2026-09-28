"""Shared fakes and fixtures. No network, no real sleeping."""

from __future__ import annotations

import email.message
import io
import urllib.error

from scraper.config import (
    ApiConfig,
    CollectionConfig,
    Config,
    LoggingConfig,
    StorageConfig,
)
from scraper.fetch import Observation


def api_config(**overrides) -> ApiConfig:
    values = dict(
        base_url="https://api.coingecko.com/api/v3",
        vs_currency="usd",
        timeout_seconds=10.0,
        max_attempts=3,
        backoff_initial=1.0,
        backoff_multiplier=2.0,
        backoff_max=10.0,
        retry_after_cap=60.0,
        backoff_budget=75.0,
        user_agent="test-agent/1.0",
    )
    values.update(overrides)
    return ApiConfig(**values)


def make_config(tmp_path, *, enable_failover: bool = True, poll_seconds: int = 300) -> Config:
    """A fully formed Config pointing at a temporary directory."""
    return Config(
        source_path=tmp_path / "config.ini",
        base_dir=tmp_path,
        api=api_config(),
        collection=CollectionConfig(
            poll_seconds=poll_seconds,
            binance_base_url="https://api.binance.com",
            hyperliquid_url="https://api.hyperliquid.example/info",
            enable_failover=enable_failover,
            coins={"BTC": "bitcoin", "ETH": "ethereum", "HYPE": "hyperliquid"},
        ),
        storage=StorageConfig(
            data_dir=tmp_path / "data",
            delete_source=True,
            compresslevel=9,
            # No real delays in tests; the retry loop still executes each pass.
            remove_retry_delays=(0.0,),
            lock_wait_seconds=0.0,
        ),
        logging=LoggingConfig(
            log_dir=tmp_path / "logs",
            filename="test.log",
            console_level=20,  # logging.INFO
            file_level=10,  # logging.DEBUG
            max_bytes=65536,
            backup_count=1,
        ),
    )


class FakeResponse:
    """Stands in for the object ``urlopen`` yields."""

    def __init__(self, body: bytes | str, status: int = 200) -> None:
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status

    def read(self, *args) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class FakeOpener:
    """A scripted transport.

    Each item is returned in turn; an item that is an exception is raised
    instead. Records every call so tests can assert on retry counts and on the
    timeout that actually reached the socket layer.
    """

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls: list[tuple[object, float | None]] = []

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        if not self.results:
            raise AssertionError(f"unexpected extra request to {request.full_url!r}")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def urls(self) -> list[str]:
        return [request.full_url for request, _ in self.calls]


class RecordingSleep:
    """Captures requested delays instead of sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def http_error(
    code: int, *, retry_after: str | None = None, body: bytes = b"{}"
) -> urllib.error.HTTPError:
    raw = f"Retry-After: {retry_after}\r\n" if retry_after is not None else ""
    headers = email.message_from_string(raw)
    return urllib.error.HTTPError("https://example.test", code, "err", headers, io.BytesIO(body))


def observation(symbol="BTC", price=100.0, *, cap=None, vol=None, source="coingecko") -> Observation:
    return Observation(
        symbol=symbol,
        price_usd=price,
        coin_id="",
        market_cap_usd=cap,
        vol_24h_usd=vol,
        source=source,
    )


class FakeFetcher:
    """A PriceFetcher stand-in that returns a fixed set of observations."""

    def __init__(self, observations=(), missing=()) -> None:
        self.observations = list(observations)
        self.missing = list(missing)
        self.calls: list[dict] = []

    def fetch_all(self, coins: dict[str, str]):
        self.calls.append(dict(coins))
        return list(self.observations), list(self.missing)
