"""Config parsing, defaults and validation."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from scraper.config import ConfigError, load_config

MINIMAL = """
[coins]
BTC = bitcoin
ETH = ethereum
HYPE = hyperliquid
"""


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, body: str, name: str = "config.ini") -> Path:
        path = self.base / name
        path.write_text(body, encoding="utf-8")
        return path

    def load(self, body: str, name: str = "config.ini"):
        return load_config(self.write(body, name), base_dir=self.base)


class DefaultTests(ConfigTestCase):
    def test_defaults_apply_when_sections_are_missing(self):
        cfg = self.load(MINIMAL)
        self.assertEqual(cfg.collection.poll_seconds, 300)
        self.assertTrue(cfg.collection.enable_failover)
        self.assertTrue(cfg.storage.delete_source)
        self.assertEqual(cfg.api.vs_currency, "usd")
        self.assertEqual(cfg.api.max_attempts, 3)

    def test_repo_config_ini_loads(self):
        # The shipped file must stay valid; it is the documented schema.
        cfg = load_config(Path(__file__).resolve().parent.parent / "config.ini")
        self.assertEqual(cfg.collection.coins, {"BTC": "bitcoin", "ETH": "ethereum", "HYPE": "hyperliquid"})
        self.assertEqual(cfg.collection.poll_seconds, 300)

    def test_defaults_when_the_coins_section_is_absent(self):
        cfg = self.load("[collection]\npoll_seconds = 120\n")
        self.assertEqual(set(cfg.collection.coins), {"BTC", "ETH", "HYPE"})

    def test_interval_is_configurable(self):
        cfg = self.load(MINIMAL + "\n[collection]\npoll_seconds = 900\n")
        self.assertEqual(cfg.collection.poll_seconds, 900)

    def test_relative_paths_resolve_against_the_base_dir_not_the_cwd(self):
        cfg = self.load(MINIMAL + "\n[storage]\ndata_dir = mydata\n")
        self.assertEqual(cfg.storage.data_dir, (self.base / "mydata").resolve())
        self.assertTrue(cfg.storage.data_dir.is_absolute())

    def test_absolute_paths_are_kept(self):
        target = (self.base / "elsewhere").resolve()
        cfg = self.load(MINIMAL + f"\n[storage]\ndata_dir = {target}\n")
        self.assertEqual(cfg.storage.data_dir, target)


class CoinTests(ConfigTestCase):
    def test_symbols_are_uppercased(self):
        # optionxform lowercases keys on the way in, so symbols are normalised
        # back up on the way out.
        cfg = self.load("[coins]\nbtc = bitcoin\neth = ethereum\n")
        self.assertEqual(cfg.collection.coins, {"BTC": "bitcoin", "ETH": "ethereum"})

    def test_case_collision_is_an_error_not_a_silent_override(self):
        from configparser import DuplicateOptionError

        # configparser raises before we ever see the file; either way it is loud.
        with self.assertRaises((ConfigError, DuplicateOptionError)):
            self.load("[coins]\nBTC = bitcoin\nbtc = cardano\n")

    def test_two_symbols_for_one_coin_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load("[coins]\nBTC = bitcoin\nWBTC = bitcoin\n")
        self.assertIn("distinct id", str(ctx.exception))

    def test_path_traversal_in_a_symbol_is_rejected(self):
        # Symbols become filenames, so a shape that could escape data/ must die.
        for bad in ("../evil", "BTC/ETH", "", "A", "TOOLONGSYMBOLNAME"):
            with self.subTest(symbol=bad):
                with self.assertRaises(ConfigError):
                    self.load(f"[coins]\n{bad} = bitcoin\n")

    def test_a_bad_coin_id_is_rejected(self):
        for bad in ("../evil", "Bitcoin", "bitcoin!", ""):
            with self.subTest(coin_id=bad):
                with self.assertRaises(ConfigError):
                    self.load(f"[coins]\nBTC = {bad}\n")

    def test_empty_coins_section_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.load("[coins]\n")


class ValidationTests(ConfigTestCase):
    def test_percent_signs_are_not_interpolated(self):
        # BasicInterpolation would raise on any value containing "%".
        cfg = self.load(MINIMAL + "\n[api]\nuser_agent = 100%25-test\n")
        self.assertEqual(cfg.api.user_agent, "100%25-test")

    def test_poll_seconds_below_the_floor_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load(MINIMAL + "\n[collection]\npoll_seconds = 5\n")
        self.assertIn("too low", str(ctx.exception))

    def test_a_short_interval_warns(self):
        with self.assertLogs("scraper.config", level="WARNING"):
            self.load(MINIMAL + "\n[collection]\npoll_seconds = 30\n")

    def test_non_numeric_values_name_the_section_and_key(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load(MINIMAL + "\n[collection]\npoll_seconds = soon\n")
        message = str(ctx.exception)
        self.assertIn("[collection]", message)
        self.assertIn("poll_seconds", message)

    def test_bad_boolean_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "\n[collection]\nenable_failover = maybe\n")

    def test_bad_log_level_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "\n[logging]\nconsole_level = chatty\n")

    def test_compresslevel_out_of_range_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "\n[storage]\ncompresslevel = 42\n")

    def test_retry_delays_parse_as_a_list(self):
        cfg = self.load(MINIMAL + "\n[storage]\nremove_retry_delays = 0.1, 0.2 ,0.3\n")
        self.assertEqual(cfg.storage.remove_retry_delays, (0.1, 0.2, 0.3))

    def test_log_levels_are_parsed(self):
        cfg = self.load(MINIMAL + "\n[logging]\nconsole_level = debug\n")
        self.assertEqual(cfg.logging.console_level, logging.DEBUG)


class LoadFailureTests(ConfigTestCase):
    def test_a_missing_file_gives_an_actionable_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.base / "nope.ini", base_dir=self.base)
        self.assertIn("not found", str(ctx.exception))

    def test_a_malformed_file_is_reported_with_its_path(self):
        path = self.write("this is not ini at all\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path, base_dir=self.base)
        self.assertIn(str(path), str(ctx.exception))

    def test_an_empty_file_is_valid_and_uses_every_default(self):
        cfg = self.load("")
        self.assertEqual(cfg.collection.poll_seconds, 300)
        self.assertEqual(set(cfg.collection.coins), {"BTC", "ETH", "HYPE"})


if __name__ == "__main__":
    unittest.main()
