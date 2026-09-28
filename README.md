# Crypto Price Scraper

Builds a historical USD price dataset for **BTC, ETH and HYPE** by polling free
APIs, writing one CSV per coin per trading day, and zipping each day's CSV once
the day ends.

Pure Python standard library. No `pip install`, no virtualenv, no build step.

```
data/
  2026-09-28_BTC.csv        <- today, being appended to
  2026-09-28_ETH.csv
  2026-09-28_HYPE.csv
  2026-09-27_BTC.csv.zip    <- yesterday, archived at 00:00 UTC
  2026-09-27_ETH.csv.zip
  2026-09-27_HYPE.csv.zip
```

## Requirements

Python **3.11 or newer** (uses `datetime.UTC` and `logging.getLevelNamesMapping`).
Developed and tested on 3.14 on Windows 11; also runs on Linux.

## Quick start

```bash
python main.py --once      # one poll, then exit
python main.py             # run forever, every 5 minutes
```

Start here rather than with a scheduler: `--once` writes three CSVs in about a
second and tells you immediately whether the network, the API and the paths all
work.

## What each row means

```csv
timestamp_utc,price_usd,market_cap_usd,vol_24h_usd,source
2026-09-28T00:35:52Z,84342.0,1695235215318.6294,22467410452.158356,coingecko
```

| Column | Meaning |
|---|---|
| `timestamp_utc` | When the price was observed, ISO 8601 UTC, second precision. |
| `price_usd` | Spot price in USD. Never blank. |
| `market_cap_usd` | **Global** market cap. Blank if the source does not publish it. |
| `vol_24h_usd` | **Global** 24h volume. Blank if the source does not publish it. |
| `source` | Which API produced this row. |

Three things follow from the `source` column, and they matter:

**Blank is not zero.** A blank means "this source does not publish that number",
which is a different claim from a real zero. A genuine zero is written `0.0`.

**The `source` column is not decoration.** When CoinGecko fails, the bot fails
over to Binance (BTC, ETH) or Hyperliquid (HYPE). Those venues do not agree
exactly — measured seconds apart, CoinGecko and CoinPaprika differed by about
0.04% on BTC. Without `source` you would see a small discontinuity appear
mid-series with no explanation. With it, you can filter to a single venue for a
clean backtest, or measure the spread between them.

**`vol_24h_usd` has one meaning across the whole dataset.** CoinGecko reports
*global* volume (BTC ≈ 21.9 B) while Binance's `quoteVolume` is *Binance only*
(BTC ≈ 814 M) — a ~27× difference for the same concept. Rather than mixing them
silently, failover rows leave `market_cap_usd` and `vol_24h_usd` blank and carry
only the price. Only the price is truly comparable between venues.

## Configuration

`config.ini` sits beside `main.py`. Every setting is optional — defaults live in
`scraper/config.py`, so the file documents the schema rather than defining it,
and deleting a line restores its default.

The interval you asked about:

```ini
[collection]
poll_seconds = 300        ; 300 = 5 minutes (default)
```

Polling is aligned to the wall-clock grid (00:00, 00:05, 00:10 UTC) rather than
counted from process start, so timestamps land on a predictable grid. The first
poll happens immediately on start; no reason to wait a full interval just
because the process started mid-slot.

| Section | Keys |
|---|---|
| `[api]` | `base_url`, `vs_currency`, `timeout_seconds`, `max_attempts`, `backoff_initial_seconds`, `backoff_multiplier`, `backoff_max_seconds`, `retry_after_cap_seconds`, `backoff_budget_seconds`, `user_agent` |
| `[collection]` | `poll_seconds`, `enable_failover`, `binance_base_url`, `hyperliquid_url` |
| `[storage]` | `data_dir`, `delete_source_csv`, `compresslevel`, `remove_retry_delays`, `lock_wait_seconds` |
| `[logging]` | `log_dir`, `log_filename`, `console_level`, `file_level`, `max_bytes`, `backup_count` |
| `[coins]` | `SYMBOL = coingecko_id` pairs |

Adding a coin to `[coins]` starts a new CSV mid-day. Removing one does not
orphan its existing file, because the rollover scans the data directory rather
than the coin list. Keys are case-insensitive, so `BTC` and `btc` in the same
file is a loud error rather than a silent override.

## Running it on a schedule

Pick **one** approach. Running two at once is safe — the single-instance lock
prevents corruption — but only one will do useful work.

### Windows: Task Scheduler

`schtasks` has no working-directory option, and `python.exe` flashes a console
window every five minutes. `pythonw.exe` avoids the flash, and every path inside
the scraper resolves relative to `main.py`, so neither the CWD nor a relative
`--config` is needed:

```bash
schtasks /Create /TN "CryptoPriceScraper" /F /SC MINUTE /MO 5 \
  /TR "\"C:\Python314\pythonw.exe\" \"C:\Users\Mark-PC\Documents\CryptoPriceScraper\main.py\" --once"
```

Then tighten the defaults, which the `schtasks` flags cannot express:

```powershell
$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
     -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
     -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Set-ScheduledTask -TaskName "CryptoPriceScraper" -Settings $s
```

`ExecutionTimeLimit` matters: its default is measured in *days*, which is
useless at a five-minute cadence and would let a wedged run overlap every
subsequent run. Three minutes never fires spuriously — one tick is bounded by
the retry budget — but catches a genuinely stuck process.

Task Scheduler runs jobs a few seconds late, so rows land at roughly `:05:03`
rather than exactly `:05:00`. The timestamp records when the price was actually
observed, which is the honest value.

### Linux: systemd (recommended) or cron

See `deploy/`. The systemd unit runs loop mode on an always-on host; the crontab
example runs `--once` every five minutes if you prefer.

## Exit codes

Both schedulers surface these, so they are worth knowing:

| Code | Meaning |
|---|---|
| `0` | Ran; rows written, or a benign no-op. |
| `1` | Configuration or startup error. Nothing attempted. |
| `2` | Another instance holds the lock (**loop mode only**). |
| `3` | Every source failed; no rows written. |
| `4` | Unexpected crash (traceback in `logs/main.crash.log`). |

A scheduled `--once` that finds the lock held exits **`0`**, not `2`: under a
scheduler that only means the loop already has it, which is not a failure and
should not pollute the task's failure history.

## How the rollover works

There is no state file and no in-memory "yesterday". Every tick scans the data
directory and archives any CSV whose *filename date* is earlier than today's UTC
date. This is forced by `--once`: the process exits every five minutes, so
anything held in memory is worthless, and a state file would be one more thing
to corrupt. Loop mode, one-shot mode and crash recovery therefore run the same
code path, and running it twice is harmless.

Three properties are load-bearing:

**An existing archive is never overwritten, only merged.** If the system clock
steps backwards, a day label gets reused and the sweep may find a CSV for a day
it already archived. Overwriting would silently destroy those rows. Instead the
rows are unioned and de-duplicated, so nothing is ever lost. The merge is
idempotent, so a crash between writing the archive and deleting the CSV just
re-does the same no-op next tick.

**A CSV is deleted only after its bytes are in a verified archive.** The zip is
built in a temp file, CRC-checked with `ZipFile.testzip()`, then moved into
place with `os.replace`. The invariant is: *if the final filename exists, the
archive is complete and valid.* A crash leaves only an ignorable temp file.

**Each CSV is opened, written, flushed, fsynced and closed within a single
tick.** `fsync` makes the row survive a power cut; closing immediately is what
lets the rollover delete its own file. Windows refuses to delete a file that any
handle holds open — *including the process's own* — so caching handles would
make the bot unable to archive its own CSV.

A missed interval stays missed. There is no backfill, by design: gaps are
visible and honest, and filled rows from a different endpoint would be of mixed
provenance.

## Troubleshooting

**Repeated HTTP 429s.** Each tick is one request, so the load is 288/day
(about 8,640/month). That is far inside the keyless per-minute budget of roughly
10–30 requests/min per IP, and this bot is deliberately keyless. If you still see
429s, the single lever available without an API key is to raise `poll_seconds` —
600 would halve the request rate. The affected intervals appear as gaps.

**`CERTIFICATE_VERIFY_FAILED` on every attempt.** Something is intercepting
HTTPS — usually antivirus or a corporate proxy, not CoinGecko. Do not disable
certificate verification; fix the interception.

**"malformed JSON" with an HTML snippet in the log.** Same cause: a proxy
returned an error page. The log includes the first 200 bytes of the body
precisely so this is diagnosable.

**`WinError 32` in the log.** A file is held open by something else — an editor,
Excel, or a sync client. The archive is written and the CSV is retained; the
sweep retries next tick. If `data/` or `logs/` is inside a OneDrive-synced
folder, exclude them: a sync client holding handles causes exactly this.

**Nothing in `logs/scraper.log` under Task Scheduler.** `pythonw.exe` sets
`sys.stdout` and `sys.stderr` to `None`, which is why the console handler is
only attached when a stream exists. Failures before logging starts go to
`logs/main.crash.log` instead.

Note that running `pythonw.exe main.py --once` *from a shell* does not
reproduce this: the shell hands the process real stdio handles, so logging
behaves normally and the check appears to pass without testing anything. The
`None` case only happens when Task Scheduler starts it with no console at all.

**On Linux, `data/` must not be on NFS.** `flock` is unreliable there, and the
instance lock depends on it.

**Row timestamps repeat for several intervals.** CoinGecko caches
`simple/price` for one to two minutes, and illiquid assets repeat for longer.
That is a real observation, not a bug, so identical values are written as-is
rather than de-duplicated.

## Development

```bash
python -m unittest discover -s tests -t .
```

The `-t .` is required — without it, discovery uses `tests/` as the top-level
directory and `import scraper` fails to resolve.

All 130 tests run without network access; HTTP, sleeping, the clock and the
lock are injected. The suite is platform-neutral, with one test gated to each
of Windows and POSIX for the "file is still open" behaviour, since the two
platforms genuinely disagree about whether an open file can be deleted. Exactly
one of those two skips, whichever platform you are on.

`test_logging_setup.py` covers the `pythonw` branch by setting `sys.stderr` to
`None` in-process, which is the only way to exercise it deterministically:
launching `pythonw.exe` from a shell still hands it valid stdio handles, so it
does *not* reproduce the Task Scheduler situation.

### Layout

```
main.py                  CLI, tick loop, exit codes, crash guard
config.ini               documented configuration
scraper/timeutil.py      UTC clock, day labels, grid alignment
scraper/config.py        INI parsing and validation
scraper/fetch.py         CoinGecko, Binance, Hyperliquid clients
scraper/store.py         CSV append, rollover sweep, zip/merge
scraper/locking.py       single-instance lock (msvcrt / fcntl)
scraper/logging_setup.py UTC formatter, rotating file + console handlers
deploy/                  systemd unit and crontab example
```
