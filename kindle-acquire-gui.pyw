"""Double-click to open the kindle-acquire window from a checkout."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from kindle_acquire.gui import main  # noqa: E402

main()
