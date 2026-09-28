"""Configuration loading and validation.

Reads an INI file with :mod:`configparser`.  Every setting is optional: the
defaults live here, so the shipped ``config.ini`` documents the schema rather
than defining it, and an empty file is a valid config.

Three configparser behaviours are handled deliberately:

* ``interpolation=None``.  The default ``BasicInterpolation`` treats ``%`` as a
  substitution marker and raises on any value containing one.
* No ``[DEFAULT]`` section support.  Its keys leak into *every* section, which
  makes an unrelated typo silently legal.
* Keys are lowercased by ``optionxform``.  That is kept, so ``BTC = ...`` and
  ``btc = ...`` in one file collide loudly rather than one silently winning.
"""

from __future__ import annotations

import configparser
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .timeutil import BASE_DIR

logger = logging.getLogger(__name__)

#: Ticker symbols become part of filenames, so they are restricted to a shape
#: that cannot escape the data directory.
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,15}$")
#: CoinGecko ids appear in a URL query string.
COIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: Polling faster than this is hostile to a free API, so refuse rather than warn.
MIN_POLL_SECONDS = 10
#: The free tier's per-minute budget is shared per IP, so warn before this.
WARN_POLL_SECONDS = 60

DEFAULT_COINS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "HYPE": "hyperliquid",
}


class ConfigError(Exception):
    """The config file is missing, malformed or self-contradictory."""


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    vs_currency: str
    timeout_seconds: float
    max_attempts: int
    backoff_initial: float
    backoff_multiplier: float
    backoff_max: float
    retry_after_cap: float
    backoff_budget: float
    user_agent: str


@dataclass(frozen=True)
class CollectionConfig:
    poll_seconds: int
    binance_base_url: str
    hyperliquid_url: str
    enable_failover: bool
    #: Ticker symbol -> CoinGecko coin id.
    coins: dict[str, str]


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path
    delete_source: bool
    compresslevel: int
    remove_retry_delays: tuple[float, ...]
    lock_wait_seconds: float


@dataclass(frozen=True)
class LoggingConfig:
    log_dir: Path
    filename: str
    console_level: int
    file_level: int
    max_bytes: int
    backup_count: int


@dataclass(frozen=True)
class Config:
    source_path: Path
    base_dir: Path
    api: ApiConfig
    collection: CollectionConfig
    storage: StorageConfig
    logging: LoggingConfig

    @property
    def data_dir(self) -> Path:
        return self.storage.data_dir


def _as_int(raw: str, *, where: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{where}: expected a whole number, got {raw!r}") from None


def _as_float(raw: str, *, where: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{where}: expected a number, got {raw!r}") from None


def _as_bool(raw: str, *, where: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{where}: expected a boolean, got {raw!r}")


def _as_level(raw: str, *, where: str) -> int:
    name = raw.strip().upper()
    level = logging.getLevelNamesMapping().get(name)
    if not isinstance(level, int):
        raise ConfigError(f"{where}: unknown log level {raw!r}")
    return level


class _Reader:
    """Typed access to one parser, with the file/section/key in every error."""

    def __init__(self, parser: configparser.ConfigParser, path: Path) -> None:
        self._parser = parser
        self._path = path

    def _raw(self, section: str, key: str) -> str | None:
        if not self._parser.has_section(section):
            return None
        return self._parser.get(section, key, fallback=None)

    def _where(self, section: str, key: str) -> str:
        return f"{self._path}: [{section}] {key}"

    def _clean(self, section: str, key: str) -> str | None:
        raw = self._raw(section, key)
        if raw is None:
            return None
        raw = raw.strip()
        return raw or None

    def string(self, section: str, key: str, default: str) -> str:
        value = self._clean(section, key)
        return default if value is None else value

    def integer(self, section: str, key: str, default: int) -> int:
        value = self._clean(section, key)
        return default if value is None else _as_int(value, where=self._where(section, key))

    def number(self, section: str, key: str, default: float) -> float:
        value = self._clean(section, key)
        return default if value is None else _as_float(value, where=self._where(section, key))

    def boolean(self, section: str, key: str, default: bool) -> bool:
        value = self._clean(section, key)
        return default if value is None else _as_bool(value, where=self._where(section, key))

    def level(self, section: str, key: str, default: int) -> int:
        value = self._clean(section, key)
        return default if value is None else _as_level(value, where=self._where(section, key))

    def path(self, section: str, key: str, default: str, *, base_dir: Path) -> Path:
        value = self._clean(section, key) or default
        candidate = Path(value).expanduser()
        # Relative paths are resolved against the project root, never the CWD.
        return candidate if candidate.is_absolute() else (base_dir / candidate).resolve()

    def floats(self, section: str, key: str, default: tuple[float, ...]) -> tuple[float, ...]:
        value = self._clean(section, key)
        if value is None:
            return default
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not parts:
            raise ConfigError(f"{self._where(section, key)}: expected a comma-separated list")
        return tuple(_as_float(p, where=self._where(section, key)) for p in parts)


def _load_coins(reader: _Reader, path: Path) -> dict[str, str]:
    """Read ``[coins]`` as ``SYMBOL = coingecko_id`` pairs."""
    if not reader._parser.has_section("coins"):
        return dict(DEFAULT_COINS)

    coins: dict[str, str] = {}
    seen_ids: dict[str, str] = {}
    for raw_symbol, raw_id in reader._parser.items("coins"):
        # optionxform lowercased the key on the way in; symbols are uppercase.
        symbol = raw_symbol.strip().upper()
        where = f"{path}: [coins] {raw_symbol}"
        if not SYMBOL_RE.match(symbol):
            raise ConfigError(
                f"{where}: {symbol!r} is not a valid ticker symbol "
                f"(expected 2-15 characters, A-Z and 0-9 only)"
            )
        coin_id = raw_id.strip()
        if not COIN_ID_RE.match(coin_id):
            raise ConfigError(f"{where}: {coin_id!r} is not a valid CoinGecko coin id")
        if symbol in coins:
            raise ConfigError(f"{where}: duplicate symbol {symbol!r}")
        if coin_id in seen_ids:
            raise ConfigError(
                f"{where}: {symbol!r} and {seen_ids[coin_id]!r} both map to "
                f"{coin_id!r}; each coin needs a distinct id"
            )
        coins[symbol] = coin_id
        seen_ids[coin_id] = symbol

    if not coins:
        raise ConfigError(f"{path}: [coins] is empty; remove the section to use defaults")
    return coins


def load_config(path: Path | str | None = None, *, base_dir: Path = BASE_DIR) -> Config:
    """Load and validate the config file.

    ``path`` defaults to ``config.ini`` beside ``main.py``.  Raises
    :class:`ConfigError` with an actionable message for every failure mode.
    """
    source = Path(path).expanduser() if path is not None else base_dir / "config.ini"
    if not source.is_absolute():
        source = (base_dir / source).resolve()

    if not source.is_file():
        raise ConfigError(
            f"config file not found: {source}\n"
            f"Create one (see README.md) or pass --config PATH."
        )

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with source.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except configparser.Error as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{source}: not valid UTF-8 ({exc})") from exc

    reader = _Reader(parser, source)

    api = ApiConfig(
        base_url=reader.string("api", "base_url", "https://api.coingecko.com/api/v3").rstrip("/"),
        vs_currency=reader.string("api", "vs_currency", "usd").lower(),
        timeout_seconds=reader.number("api", "timeout_seconds", 10.0),
        max_attempts=reader.integer("api", "max_attempts", 3),
        backoff_initial=reader.number("api", "backoff_initial_seconds", 1.0),
        backoff_multiplier=reader.number("api", "backoff_multiplier", 2.0),
        backoff_max=reader.number("api", "backoff_max_seconds", 10.0),
        retry_after_cap=reader.number("api", "retry_after_cap_seconds", 60.0),
        backoff_budget=reader.number("api", "backoff_budget_seconds", 75.0),
        user_agent=reader.string("api", "user_agent", "CryptoPriceScraper/1.0"),
    )
    if api.timeout_seconds <= 0:
        raise ConfigError(f"{source}: [api] timeout_seconds must be positive")
    if api.max_attempts < 1:
        raise ConfigError(f"{source}: [api] max_attempts must be at least 1")
    if api.backoff_initial < 0 or api.backoff_max < 0 or api.backoff_multiplier < 1:
        raise ConfigError(
            f"{source}: [api] backoff_initial_seconds and backoff_max_seconds must be "
            f"non-negative and backoff_multiplier must be at least 1"
        )

    poll_seconds = reader.integer("collection", "poll_seconds", 300)
    if poll_seconds < MIN_POLL_SECONDS:
        raise ConfigError(
            f"{source}: [collection] poll_seconds={poll_seconds} is too low; "
            f"the minimum is {MIN_POLL_SECONDS}"
        )
    if poll_seconds < WARN_POLL_SECONDS:
        logger.warning(
            "poll_seconds=%d is below %d; free tiers rate limit per IP and short "
            "intervals are the usual cause of 429s",
            poll_seconds,
            WARN_POLL_SECONDS,
        )

    collection = CollectionConfig(
        poll_seconds=poll_seconds,
        binance_base_url=reader.string(
            "collection", "binance_base_url", "https://api.binance.com"
        ).rstrip("/"),
        hyperliquid_url=reader.string(
            "collection", "hyperliquid_url", "https://api.hyperliquid.xyz/info"
        ),
        enable_failover=reader.boolean("collection", "enable_failover", True),
        coins=_load_coins(reader, source),
    )

    compresslevel = reader.integer("storage", "compresslevel", 9)
    if not 0 <= compresslevel <= 9:
        raise ConfigError(f"{source}: [storage] compresslevel must be between 0 and 9")

    storage = StorageConfig(
        data_dir=reader.path("storage", "data_dir", "data", base_dir=base_dir),
        delete_source=reader.boolean("storage", "delete_source_csv", True),
        compresslevel=compresslevel,
        remove_retry_delays=reader.floats(
            "storage", "remove_retry_delays", (0.2, 0.4, 0.8, 1.6)
        ),
        lock_wait_seconds=reader.number("storage", "lock_wait_seconds", 10.0),
    )

    log_cfg = LoggingConfig(
        log_dir=reader.path("logging", "log_dir", "logs", base_dir=base_dir),
        filename=reader.string("logging", "log_filename", "scraper.log"),
        console_level=reader.level("logging", "console_level", logging.INFO),
        file_level=reader.level("logging", "file_level", logging.DEBUG),
        max_bytes=reader.integer("logging", "max_bytes", 1_048_576),
        backup_count=reader.integer("logging", "backup_count", 5),
    )

    return Config(
        source_path=source,
        base_dir=base_dir,
        api=api,
        collection=collection,
        storage=storage,
        logging=log_cfg,
    )
