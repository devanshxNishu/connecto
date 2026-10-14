"""``python -m connecto ...`` (what the daily schedule runs) is the same as the ``connecto`` command."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
