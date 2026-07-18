"""`python3 -m helm` — same entry as bin/helm."""
import sys

from .cli import main

sys.exit(main())
