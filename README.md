# Crypto Price Scraper

Builds a historical USD price dataset for **BTC, ETH and HYPE** by polling free
APIs, writing one CSV per coin per trading day, and zipping each day's CSV once
the day ends. Defaults to one observation every **10 seconds**.

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
python main.py --once      # one poll, then exit (diagnostic)
python main.py             # run forever, every 10 seconds
```

Start here rather than with a scheduler: `--once` writes three CSVs in about a
second and tells you immediately whether the network, the API and the paths all
work.

## Where the prices come from

**Binance is the primary source**, and a single request per tick covers all three
coins (`GET /api/v3/ticker/price?symbols=[...]`). It needs no API key. If it
fails, the tick falls back to **CoinGecko**, then to **Hyperliquid** — each also
one request for whatever is still missing.

Which source produced each row is recorded in the `source` column.

### Why not CoinGecko first

CoinGecko's keyless `simple/price` endpoint is **cached server-side for one to two
minutes**. That is a cache, not a rate limit, so no amount of throttling or
retrying can defeat it: two probes ten seconds apart returned *identical values
for all three coins*. Polling it every ten seconds would have produced a file
that looks like ten-second resolution while carrying one-to-two-minute
information — roughly six duplicate rows per real observation. It is a fine
source, just not at this cadence, so it sits in the failover tier where its cache
costs nothing.

### What Binance costs

`/api/v3/ticker/price` is **weight 4** against a per-IP budget of **6,000 per
minute**. One request every ten seconds is 4 weight per tick, 24 per minute —
**0.4% of the budget**. The headroom here is not a coincidence to be trimmed
later; it is what makes the ten-second cadence safe without a key.

### `timestamp_utc` is when we looked, not when the trade printed

Binance's `ticker/price` returns only a symbol and a price. It carries **no venue
timestamp**, and neither does the failover payload. So `timestamp_utc` records
when the scraper observed the price, and a row is one observation rather than one
trade. At ten-second resolution against a liquid pair the two are close, but they
are not the same thing and the column should not be read as an execution time.

## What each row means

```csv
timestamp_utc,price_usd,source
2026-09-28T00:35:52Z,84342.0,binance
```

| Column | Meaning |
|---|---|
| `timestamp_utc` | When the price was observed, ISO 8601 UTC, second precision. |
| `price_usd` | Spot price in USD. Never blank. |
| `source` | Which API produced this row: `binance`, `coingecko` or `hyperliquid`. |

**The `source` column is not decoration.** The three venues do not agree exactly
— measured seconds apart, CoinGecko and CoinPaprika differed by about 0.04% on
BTC. Without `source` you would see a small discontinuity appear mid-series with
no explanation. With it, you can filter to a single venue for a clean backtest,
or measure the spread between them.

There are no `market_cap_usd` or `vol_24h_usd` columns, and their absence is
deliberate rather than an omission. No keyless venue API publishes a *global*
market cap, and Binance's `quoteVolume` is *Binance only* (BTC ≈ 814 M) against
CoinGecko's global figure (BTC ≈ 21.9 B) — a ~27× difference for the same
concept. A column that silently changes meaning depending on which source
answered is worse than no column, so both were dropped and only `price_usd` — the
one number that is genuinely comparable across venues — is recorded.

## Configuration

`config.ini` sits beside `main.py`. Every setting is optional — defaults live in
`scraper/config.py`, so the file documents the schema rather than defining it,
and deleting a line restores its default.

The interval you asked about:

```ini
[collection]
poll_seconds = 10         ; 10 = ten seconds (default)
```

Polling is aligned to the wall-clock grid (`:00`, `:10`, `:20`…) rather than
counted from process start, so timestamps land on a predictable grid. The first
poll happens immediately on start; no reason to wait a full interval just because
the process started mid-slot.

The grid is recomputed from the clock every iteration rather than accumulated
from the previous tick. If the machine suspends for nine hours, exactly one sleep
happens on wake — an accumulated schedule would have queued ~3,240 immediate
polls.

| Section | Keys |
|---|---|
| `[api]` | `base_url`, `vs_currency`, `timeout_seconds`, `max_attempts`, `failover_max_attempts`, `backoff_initial_seconds`, `backoff_multiplier`, `backoff_max_seconds`, `retry_after_cap_seconds`, `backoff_budget_seconds`, `user_agent` |
| `[collection]` | `poll_seconds`, `enable_failover`, `binance_base_url`, `hyperliquid_url` |
| `[storage]` | `data_dir`, `delete_source_csv`, `compresslevel`, `remove_retry_delays`, `lock_wait_seconds` |
| `[logging]` | `log_dir`, `log_filename`, `console_level`, `file_level`, `max_bytes`, `backup_count` |
| `[coins]` | `SYMBOL = coingecko_id` pairs |

Adding a coin to `[coins]` starts a new CSV mid-day. Removing one does not orphan
its existing file, because the rollover scans the data directory rather than the
coin list. Keys are case-insensitive, so `BTC` and `btc` in the same file is a
loud error rather than a silent override.

The retry budget is sized for a ten-second slot, not for a five-minute one: a
timeout of 2.5 s and a single retry on the primary, one attempt on each failover.
`urllib`'s timeout applies *per socket operation*, not to the request as a whole,
so a single attempt can cost about twice the configured value — the numbers are
chosen pessimistically for that reason. A failover that fails has already told
you everything the tick needs to know, and the next tick is ten seconds away.

Setting `poll_seconds` below 5 is rejected. The shipped value of 10 does not
warn; anything below it does.

## Running it

**Loop mode is the deployment.** `python main.py` is the only supported way to
run this at ten seconds, because no scheduler can express that cadence:

* **cron** floors at one minute by construction.
* **Task Scheduler** rejects a sub-minute repetition outright (HRESULT
  `0x80041318`).

So a scheduled `--once` runs at the *scheduler's* floor, not at `poll_seconds`.
Someone who set `poll_seconds = 10` and scheduled `--once` every minute would
believe they had ten-second data while collecting sixty-second data. To make that
impossible to miss, `--once` warns on startup whenever `poll_seconds < 60`.
`--once` remains available as a first-run diagnostic: it writes three CSVs in
about a second and proves the network, the API and the paths all work.

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install-windows-task.ps1
```

No administrator prompt is needed. This registers a task that starts at logon and
then loops forever; a logon trigger is a *one-shot* trigger, so the sub-minute
restriction never applies to it. It runs `pythonw.exe`, so no console window
appears.

Two settings in that script are load-bearing:

* `ExecutionTimeLimit` is set to `PT0S` — **no limit**. The Task Scheduler default
  is three days, so leaving it unset would kill a perfectly healthy scraper every
  third day, silently, as a "task stopped" event nobody is watching.
* `RestartOnFailure` restarts it three times, one minute apart. That only fires on
  a non-zero exit, and the program exits `4` solely after thirty consecutive
  failed ticks — about five minutes of collecting nothing. Transient failures are
  ridden out in-process and never reach the scheduler.

### Linux: systemd

See `deploy/crypto-price-scraper.service`; install instructions are in its header.
`Restart=always` there is a backstop rather than the primary recovery path, for
the same reason described above.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Ran; rows written, or a benign no-op. |
| `1` | Configuration or startup error. Nothing attempted. |
| `2` | Another instance holds the lock (**loop mode only**). |
| `3` | Every source failed; no rows written (`--once` only). |
| `4` | Gave up after 30 consecutive failed ticks, or an unexpected crash. |

A scheduled `--once` that finds the lock held exits **`0`**, not `2`: under a
scheduler that only means the loop already has it, which is not a failure and
should not pollute the task's failure history.

In loop mode a tick that raises is caught, logged and retried on the next slot
rather than ending the run. The most plausible trigger is a Windows sharing
violation — an AV scanner or sync client holding today's CSV open. Without that
guard a single transient error would end a run that no scheduler is watching, and
the loss would surface as a gap in the data days later. Thirty consecutive
failures is the point at which looking alive while collecting nothing becomes
worse than exiting for a supervisor to notice.

## How the rollover works

There is no state file and no in-memory "yesterday". Every tick scans the data
directory and archives any CSV whose *filename date* is earlier than today's UTC
date. Loop mode, one-shot mode and crash recovery therefore run the same code
path, and running it twice is harmless.

That statelessness is no longer *forced* — it was originally required because
`--once` exited every five minutes, and `--once` is now only a diagnostic — but
it is kept. The scan is a few milliseconds, and the alternative (gating it on a
day-change flag) reintroduces in-memory state and splits loop mode, one-shot mode
and crash recovery into three paths that can drift apart. Not worth it at this
price.

Four properties are load-bearing:

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
handle holds open — *including the process's own* — so caching handles would make
the bot unable to archive its own CSV.

**A CSV whose columns do not match this build is left untouched, and said so
once.** See below.

A missed interval stays missed. There is no backfill, by design: gaps are visible
and honest, and filled rows from a different endpoint would be of mixed
provenance.

### The schema guard

The dataset was previously five columns wide. Appending a three-field row under a
five-column header corrupts the file *without producing anything that looks
wrong*: read back, `price_usd` is correct, `source` silently becomes null, and
`market_cap_usd` holds the word `binance`. Every sanity check on the price
passes. That is the worst available failure shape — plausible data with corrupted
provenance — so both places that can create it now refuse instead.

* **On append**, if today's CSV exists and its header is not exactly
  `timestamp_utc,price_usd,source`, the file is skipped, left byte-for-byte
  untouched, and explained once in the log. The other two coins still get their
  rows, and the refusal is logged once per file per process rather than once per
  tick — a mismatched file stays mismatched all day, and 8,640 warning lines
  would be the single largest consumer of the log budget.
* **On merge**, if a CSV and the archive it would merge into have different
  headers, the merge is refused, both files survive, and the failure is reported
  in the rollover line. Auto-archiving on detection would *manufacture* the very
  corruption being guarded against: the partial day enters the zip, and at the
  next midnight sweep the fresh CSV merges into that same zip by filename.
  Refusing and waiting is strictly safer — at 00:00 UTC the old file is zipped
  with no merge at all, surviving as a clean five-column artifact.

## Troubleshooting

**Repeated HTTP 429s.** One request per tick is 8,640/day against Binance's
6,000-weight-per-minute budget — about 0.4% of it at weight 4 per request. If you
still see 429s, something else on the IP is consuming the budget. A source that
returns `Retry-After` is *parked* for that long and skipped without issuing a
request until the pause expires; the loop keeps ticking throughout, so honouring
the pause costs nothing and nothing blocks. Capping the pause and continuing to
poll is what escalates a 429 into an IP ban, which is why it is not done.

**`CERTIFICATE_VERIFY_FAILED` on every attempt.** Something is intercepting
HTTPS — usually antivirus or a corporate proxy, not the exchange. Do not disable
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

**No per-tick lines in the log.** That is intentional at ten seconds: an INFO
line per tick would be 8,640 lines and over a megabyte a day, more log than the
events it describes. The per-tick line still exists at DEBUG (set
`file_level = DEBUG`), and in its place the loop logs a **summary every 60 ticks**
— ten minutes — carrying ticks run, rows written, the per-symbol source mix,
missing symbols, the slowest tick and the consecutive-failure count. That is
strictly more useful than the raw stream, and it replaces the per-run history
`--once` used to get for free from the scheduler.

**On Linux, `data/` must not be on NFS.** `flock` is unreliable there, and the
instance lock depends on it.

## Development

```bash
python -m unittest discover -s tests -t .
```

The `-t .` is required — without it, discovery uses `tests/` as the top-level
directory and `import scraper` fails to resolve.

All 169 tests run without network access; HTTP, sleeping, the clock and the lock
are injected. The suite is platform-neutral, with one test gated to each of
Windows and POSIX for the "file is still open" behaviour, since the two platforms
genuinely disagree about whether an open file can be deleted. Exactly one of
those two skips, whichever platform you are on.

`test_logging_setup.py` covers the `pythonw` branch by setting `sys.stderr` to
`None` in-process, which is the only way to exercise it deterministically:
launching `pythonw.exe` from a shell still hands it valid stdio handles, so it
does *not* reproduce the Task Scheduler situation.

### Layout

```
main.py                  CLI, tick loop, exit codes, crash guard, summary
config.ini               documented configuration
scraper/timeutil.py      UTC clock, day labels, grid alignment
scraper/config.py        INI parsing and validation
scraper/fetch.py         Binance, CoinGecko and Hyperliquid clients
scraper/store.py         CSV append, rollover sweep, zip/merge, schema guards
scraper/locking.py       single-instance lock (msvcrt / fcntl)
scraper/logging_setup.py UTC formatter, rotating handlers, rate limiting
deploy/                  systemd unit, Windows task installer
```
