"""Both windows start through their real entry points.

Skipped where there is no display or no Qt binding.
"""

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindle_acquire import jobs  # noqa: E402


class WindowTests(unittest.TestCase):
    def test_assets_are_shipped(self):
        self.assertTrue(os.path.isfile(jobs.asset("keat.png")))
        self.assertTrue(os.path.isfile(jobs.asset("keat.ico")))

    def test_tkinter_window_starts(self):
        import tkinter as tk
        from kindle_acquire import gui
        real_tk = tk.Tk

        def short_lived(*args, **kwargs):
            root = real_tk(*args, **kwargs)
            root.after(400, root.destroy)
            return root

        gui.tk.Tk = short_lived
        try:
            self.assertEqual(gui.main(), 0)
        except tk.TclError as exc:
            self.skipTest("no display: %s" % exc)
        finally:
            gui.tk.Tk = real_tk

    def test_qt_window_builds(self):
        try:
            from kindle_acquire import gui_qt
        except ImportError:
            self.skipTest("PySide6 or PyQt6 is not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = gui_qt.QtWidgets.QApplication.instance() or gui_qt.QtWidgets.QApplication([])
        gui_qt.apply_style(app)
        window = gui_qt.Window()
        window.show()
        deadline = time.time() + 60
        app.processEvents()
        while window.detecting and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        self.assertFalse(window.detecting)
        self.assertEqual(window.stack.count(), 5)
        self.assertIn("KEAT", window.crumbs[0].text())
        for page in (window.page_files, window.page_settings, window.page_results,
                     window.page_main):
            window._show(page)
            app.processEvents()
        window.close()


if __name__ == "__main__":
    unittest.main()
