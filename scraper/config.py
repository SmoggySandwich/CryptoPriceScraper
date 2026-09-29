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

#: Below this the tick cannot finish inside its own slot once a request has to
#: time out, so refuse rather than warn.
MIN_POLL_SECONDS = 5
#: Warn below this: the retry budget cannot fit, so a slow tick starts eating
#: the next slot. The shipped default of 10 sits at the threshold.
WARN_POLL_SECONDS = 10

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
    #: Failover venues get this instead of ``max_attempts`` -- normally 1.
    failover_max_attempts: int
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
    #: Non-fatal problems found while parsing, carried rather than logged here.
    #: ``load_config`` runs before ``setup_logging``, so anything it logged
    #: would go to the root logger's stderr fallback: it would reach a console
    #: but never ``logs/scraper.log``, and under ``pythonw.exe`` -- which is how
    #: the scheduled task runs -- ``sys.stderr`` is None and it would vanish
    #: completely. The caller emits these once the configured handlers exist.
    warnings: tuple[str, ...] = ()

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

    # These defaults are sized for a ten-second slot, not the old five-minute
    # one. The binding constraint is that a slow tick must not run into the slot
    # after it, because nothing backfills a missed interval: a /ticker/price
    # response is under a kilobyte, so a 10s timeout would spend the entire slot
    # waiting before the first retry even began. Note that urllib applies its
    # timeout per socket operation rather than as a deadline, so one "attempt"
    # can cost roughly twice the configured value.
    api = ApiConfig(
        base_url=reader.string("api", "base_url", "https://api.coingecko.com/api/v3").rstrip("/"),
        vs_currency=reader.string("api", "vs_currency", "usd").lower(),
        timeout_seconds=reader.number("api", "timeout_seconds", 2.5),
        max_attempts=reader.integer("api", "max_attempts", 2),
        failover_max_attempts=reader.integer("api", "failover_max_attempts", 1),
        backoff_initial=reader.number("api", "backoff_initial_seconds", 0.5),
        backoff_multiplier=reader.number("api", "backoff_multiplier", 2.0),
        backoff_max=reader.number("api", "backoff_max_seconds", 1.0),
        retry_after_cap=reader.number("api", "retry_after_cap_seconds", 2.0),
        backoff_budget=reader.number("api", "backoff_budget_seconds", 3.0),
        user_agent=reader.string("api", "user_agent", "CryptoPriceScraper/1.0"),
    )
    if api.timeout_seconds <= 0:
        raise ConfigError(f"{source}: [api] timeout_seconds must be positive")
    if api.max_attempts < 1:
        raise ConfigError(f"{source}: [api] max_attempts must be at least 1")
    if api.failover_max_attempts < 1:
        raise ConfigError(f"{source}: [api] failover_max_attempts must be at least 1")
    if api.backoff_initial < 0 or api.backoff_max < 0 or api.backoff_multiplier < 1:
        raise ConfigError(
            f"{source}: [api] backoff_initial_seconds and backoff_max_seconds must be "
            f"non-negative and backoff_multiplier must be at least 1"
        )

    poll_seconds = reader.integer("collection", "poll_seconds", 10)
    if poll_seconds < MIN_POLL_SECONDS:
        raise ConfigError(
            f"{source}: [collection] poll_seconds={poll_seconds} is too low; "
            f"the minimum is {MIN_POLL_SECONDS}"
        )
    warnings: list[str] = []
    if poll_seconds < WARN_POLL_SECONDS:
        # Reworded from the original, which blamed "free tiers rate limit per IP
        # and short intervals are the usual cause of 429s". That was CoinGecko
        # reasoning; the primary is now Binance, measured at well under 1% of its
        # published weight budget at this cadence. The real risk is arithmetic:
        # the retry budget no longer fits inside one slot.
        warnings.append(
            f"poll_seconds={poll_seconds} is below {WARN_POLL_SECONDS}; the retry "
            f"budget (up to {api.backoff_budget:.1f}s of backoff plus "
            f"{api.timeout_seconds:.1f}s per timed-out attempt) may not fit inside a slot"
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
        # Tightened from (0.2, 0.4, 0.8, 1.6) = 3.0s, which was affordable at a
        # five-minute interval and is not at ten seconds. These delays now also
        # gate the append open, so a locked CSV spends at most 0.75s before the
        # row is given up on rather than the whole slot. Sharing violations are
        # held for milliseconds in practice; the retries are there to ride out a
        # scanner's momentary claim, not to wait out a wedged editor.
        remove_retry_delays=reader.floats(
            "storage", "remove_retry_delays", (0.05, 0.1, 0.2, 0.4)
        ),
        lock_wait_seconds=reader.number("storage", "lock_wait_seconds", 10.0),
    )

    log_cfg = LoggingConfig(
        log_dir=reader.path("logging", "log_dir", "logs", base_dir=base_dir),
        filename=reader.string("logging", "log_filename", "scraper.log"),
        console_level=reader.level("logging", "console_level", logging.INFO),
        # INFO rather than DEBUG: the per-tick success line is now a debug
        # detail and the loop writes a summary every ten minutes instead. The
        # old 1 MiB x 5 was sized for 288 ticks a day and holds barely three
        # days at 8,640 -- a problem noticed on Monday had already rotated out.
        # 4 MiB x 10 is about 24 days. Larger files also shrink the window for
        # the Windows rotation hazard, where a rename fails because something
        # holds scraper.log open.
        file_level=reader.level("logging", "file_level", logging.INFO),
        max_bytes=reader.integer("logging", "max_bytes", 4 * 1_048_576),
        backup_count=reader.integer("logging", "backup_count", 10),
    )

    return Config(
        source_path=source,
        base_dir=base_dir,
        api=api,
        collection=collection,
        storage=storage,
        logging=log_cfg,
        warnings=tuple(warnings),
    )
