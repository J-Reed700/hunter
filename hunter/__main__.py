"""`python -m hunter` → the CLI (the scheduler is `python -m hunter.scheduler`)."""
import sys

from hunter.cli import main

sys.exit(main())
