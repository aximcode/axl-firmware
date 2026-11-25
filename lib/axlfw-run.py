"""build.sh's launcher for the axlfw package: python axlfw-run.py <command>.

Run as a script, its directory (lib/) is on sys.path, so the package
imports from here whatever the working directory.
"""

from __future__ import annotations

import sys

from axlfw.__main__ import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
