"""Double-click to open the KEAT window from a checkout.

Uses the Qt window when PySide6 or PyQt6 is installed, else Tkinter.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from kindle_acquire.cli import open_window  # noqa: E402

open_window()
