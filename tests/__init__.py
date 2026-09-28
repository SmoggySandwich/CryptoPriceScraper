"""Test package.

Run the suite from the repository root::

    python -m unittest discover -s tests -t .

The ``-t .`` matters: without it, discovery uses ``tests/`` as the top-level
directory and ``import scraper`` fails because the repository root never lands
on ``sys.path``.

Every test here runs without network access -- HTTP, sleeping and the clock are
all injected.
"""

import logging

# The package logger stays unconfigured under test, so warnings would fall
# through to logging's lastResort handler and clutter the run. A NullHandler
# absorbs them; assertLogs still captures records, because it attaches its own
# handler rather than relying on the hierarchy.
logging.getLogger("scraper").addHandler(logging.NullHandler())
