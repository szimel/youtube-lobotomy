"""Runs the whole suite as if the machine had just booted.

The suite passes on a developer's machine and failed on CI because of one
difference between them: time.monotonic() counts from boot, so it is 855302
seconds here and about 40 seconds on a fresh runner. Anything that compares a
clock reading against a constant therefore behaves differently in the two
places -- which is how an empty watch-history cache came to pass for a
freshly-read one, and four tests passed locally while failing on every push.

Run this the same way as the normal suite (from the repository root):

    python tests/run_as_freshly_booted.py

It shifts the clock by a constant rather than freezing it, so durations stay
correct and only the absolute reading changes.
"""

import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

REAL_MONOTONIC = time.monotonic
OFFSET = REAL_MONOTONIC() - 12.0


def freshly_booted() -> float:
    return REAL_MONOTONIC() - OFFSET


time.monotonic = freshly_booted

if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=1).run(
        unittest.TestLoader().discover("tests")
    )
    sys.exit(0 if result.wasSuccessful() else 1)
