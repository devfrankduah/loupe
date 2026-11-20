"""python -m loupe_scan runs the loupe command, for when its script isn't on PATH."""

import sys

from loupe_scan.cli import main

sys.exit(main())
