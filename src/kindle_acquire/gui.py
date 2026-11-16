"""Desktop front end for kindle-acquire (Tkinter, standard library only).

Runs the same functions as the command line. Long operations run on a
worker thread and report progress through util.PROGRESS_HOOK.
"""

import ctypes
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import __version__, jobs, util
from .jobs import is_admin, relaunch_elevated
from .jobs import safe_label as _safe


class Cancelled(Exception):
    pass


class App:
    PAD = {"padx": 8, "pady": 4}

    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.found = None
        self.last_output = None
        self._last_progress = 0.0
        self.admin = is_admin()

        root.title("KEAT - Kindle E-Reader Acquisition Tool %s%s" % (
            __version__, "  (administrator)" if self.admin else ""))
        root.minsize(760, 640)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)

        self._build_device(root)
        self._build_case(root)
        self._build_actions(root)
        self._build_progress(root)
        self._build_log(root)

        root.protocol("WM_DELETE_WINDOW", self._close)
        root.after(100, self._pump)
        self.log("kindle-acquire %s. Copies data as stored. Does not remove DRM." % __version__)
        if not self.admin:
            self.log("Not running as administrator. Imaging the disk will offer to restart elevated.")
        self.detect()

    # --- layout ---------------------------------------------------------------
    def _build_device(self, root):
        box = ttk.LabelFrame(root, text="Device")
        box.grid(row=0, column=0, sticky="ew", **self.PAD)
        box.columnconfigure(0, weight=1)
        self.device_text = tk.StringVar(value="Looking for a Kindle...")
        ttk.Label(box, textvariable=self.device_text, justify="left",
                  font=("Consolas", 9)).grid(row=0, column=0, sticky="w", **self.PAD)
        self.detect_button = ttk.Button(box, text="Detect", command=self.detect)
        self.detect_button.grid(row=0, column=1, sticky="ne", **self.PAD)

    def _build_case(self, root):
        box = ttk.LabelFrame(root, text="Case")
        box.grid(row=1, column=0, sticky="ew", **self.PAD)
        box.columnconfigure(1, weight=1)
        box.columnconfigure(3, weight=1)
        self.case_id = tk.StringVar()
        self.evidence_id = tk.StringVar()
        self.examiner = tk.StringVar()
        self.notes = tk.StringVar()
        self.out_base = tk.StringVar(value=str(Path.home() / "Documents" / "Kindle_Acquisitions"))
        self.known = tk.StringVar()
        fields = (("Case number", self.case_id, 0, 0), ("Evidence item", self.evidence_id, 0, 2),
                  ("Examiner", self.examiner, 1, 0), ("Notes", self.notes, 1, 2))
        for label, var, row, col in fields:
            ttk.Label(box, text=label).grid(row=row, column=col, sticky="w", **self.PAD)
            ttk.Entry(box, textvariable=var).grid(row=row, column=col + 1, sticky="ew", **self.PAD)
        ttk.Label(box, text="Output folder").grid(row=2, column=0, sticky="w", **self.PAD)
        ttk.Entry(box, textvariable=self.out_base).grid(
            row=2, column=1, columnspan=2, sticky="ew", **self.PAD)
        ttk.Button(box, text="Browse...", command=self._pick_out).grid(
            row=2, column=3, sticky="w", **self.PAD)
        ttk.Label(box, text="Known hashes").grid(row=3, column=0, sticky="w", **self.PAD)
        ttk.Entry(box, textvariable=self.known).grid(
            row=3, column=1, columnspan=2, sticky="ew", **self.PAD)
        pick = ttk.Frame(box)
        pick.grid(row=3, column=3, sticky="w", **self.PAD)
        ttk.Button(pick, text="Folder...", command=self._pick_known_folder).pack(side="left")
        ttk.Button(pick, text="List file...", command=self._pick_known_file).pack(
            side="left", padx=(6, 0))

    def _build_actions(self, root):
        box = ttk.LabelFrame(root, text="Actions")
        box.grid(row=2, column=0, sticky="ew", **self.PAD)
        self.buttons = []

        def button(parent, text, command, **grid):
            b = ttk.Button(parent, text=text, command=command)
            b.grid(sticky="ew", **grid, **self.PAD)
            self.buttons.append(b)
            return b

        for col in range(3):
            box.columnconfigure(col, weight=1, uniform="actions")
        button(box, "Full acquisition\nimage, extract, parse", self.full, row=0, column=0, rowspan=2)
        button(box, "Image the disk", self.image, row=0, column=1)
        button(box, "Copy files from the mounted volume", self.logical, row=0, column=2)
        button(box, "Extract from an image...", self.extract, row=1, column=1)
        sub = ttk.Frame(box)
        sub.grid(row=1, column=2, sticky="ew")
        sub.columnconfigure(0, weight=1)
        sub.columnconfigure(1, weight=1)
        button(sub, "Parse...", self.parse, row=0, column=0)
        button(sub, "Verify...", self.verify, row=0, column=1)
        self.verify_source = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Read the device a second time and compare (doubles imaging time)",
                        variable=self.verify_source).grid(
            row=2, column=0, columnspan=3, sticky="w", **self.PAD)

    def _build_progress(self, root):
        box = ttk.Frame(root)
        box.grid(row=3, column=0, sticky="ew", **self.PAD)
        box.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(box, maximum=1000)
        self.bar.grid(row=0, column=0, sticky="ew")
        self.cancel_button = ttk.Button(box, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.grid(row=0, column=1, padx=(8, 0))
        self.status_text = tk.StringVar(value="Ready")
        ttk.Label(box, textvariable=self.status_text).grid(row=1, column=0, sticky="w")

    def _build_log(self, root):
        box = ttk.Frame(root)
        box.grid(row=4, column=0, sticky="nsew", **self.PAD)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.log_box = scrolledtext.ScrolledText(box, height=14, state="disabled",
                                                 font=("Consolas", 9), wrap="word")
        self.log_box.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.open_button = ttk.Button(box, text="Open output folder",
                                      command=self._open_output, state="disabled")
        self.open_button.grid(row=1, column=1, sticky="e", pady=(6, 0))

    # --- small helpers ----------------------------------------------------------
    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", "%s  %s\n" % (time.strftime("%H:%M:%S"), text))
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _pick_out(self):
        chosen = filedialog.askdirectory(title="Output folder")
        if chosen:
            self.out_base.set(os.path.normpath(chosen))

    def _pick_known_folder(self):
        chosen = filedialog.askdirectory(title="Folder of reference files")
        if chosen:
            self.known.set(os.path.normpath(chosen))

    def _pick_known_file(self):
        chosen = filedialog.askopenfilename(title="List of MD5 or SHA-256 values")
        if chosen:
            self.known.set(os.path.normpath(chosen))

    def _open_output(self):
        if self.last_output and os.path.isdir(self.last_output):
            os.startfile(self.last_output)

    def _case(self):
        return {"case_id": self.case_id.get().strip(), "evidence_id": self.evidence_id.get().strip(),
                "examiner": self.examiner.get().strip(), "notes": self.notes.get().strip()}

    def _device(self, mode="mass-storage"):
        """The single connected Kindle in the given mode, or None after a message."""
        devices = [d for d in (self.found or {}).get("devices", []) if d["mode"] == mode]
        if len(devices) != 1:
            messagebox.showwarning(
                "kindle-acquire",
                "Expected one connected Kindle in %s mode, found %d.\n"
                "Connect one device and press Detect." % (mode, len(devices)))
            return None
        device = dict(devices[0])
        device["usb_write_protect_policy"] = self.found.get("usb_write_protect_policy")
        return device

    def _new_out(self, kind, device=None):
        label = _safe(self.evidence_id.get().strip())
        if not label and device:
            label = "kindle_%s" % _safe(device["usb_serial"])
        name = "%s_%s_%s" % (label or "kindle", kind, time.strftime("%Y%m%d_%H%M%S"))
        return str(Path(self.out_base.get().strip()) / name)

    def _ask_folder(self, title):
        start = self.out_base.get().strip()
        return filedialog.askdirectory(
            title=title, initialdir=start if os.path.isdir(start) else None)

    # --- worker plumbing -----------------------------------------------------------
    def _hook(self, label, done, total, final):
        if self.cancel.is_set():
            raise Cancelled()
        now = time.monotonic()
        if total is not None and not final and now - self._last_progress < 0.2:
            return
        self._last_progress = now
        self.queue.put(("progress", (label, done, total)))

    def _say(self, text):
        self.queue.put(("log", text))

    def _start(self, title, steps):
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        for b in self.buttons + [self.detect_button]:
            b.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.log("--- %s ---" % title)
        self.status_text.set(title)
        self.bar.configure(mode="indeterminate")
        self.bar.start(40)

        def work():
            util.PROGRESS_HOOK = self._hook
            try:
                for step in steps:
                    if self.cancel.is_set():
                        raise Cancelled()
                    step()
                self.queue.put(("finished", "%s: finished" % title))
            except Cancelled:
                self.queue.put(("finished", "%s: CANCELLED. The output folder is incomplete "
                                            "and has no acquisition log." % title))
            except Exception as exc:  # shown to the user, never swallowed
                self.queue.put(("finished", "%s: FAILED. %s: %s" % (
                    title, type(exc).__name__, exc)))
            finally:
                util.PROGRESS_HOOK = None

        threading.Thread(target=work, daemon=True).start()

    def _pump(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "output":
                    self.last_output = payload
                    self.open_button.configure(state="normal")
                elif kind == "device":
                    self._show_devices(payload)
                elif kind == "progress":
                    label, done, total = payload
                    if total:
                        if str(self.bar.cget("mode")) != "determinate":
                            self.bar.stop()
                            self.bar.configure(mode="determinate")
                        self.bar.configure(value=1000.0 * done / total)
                        self.status_text.set("%s: %.1f%%   %.0f of %.0f MiB" % (
                            label, 100.0 * done / total, done / 1048576.0, total / 1048576.0))
                    else:
                        if str(self.bar.cget("mode")) != "indeterminate":
                            self.bar.configure(mode="indeterminate")
                            self.bar.start(40)
                        self.status_text.set(label)
                elif kind == "finished":
                    self.bar.stop()
                    self.bar.configure(mode="determinate", value=0)
                    self.status_text.set("Ready")
                    self.log(payload)
                    self.busy = False
                    for b in self.buttons + [self.detect_button]:
                        b.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
        except queue.Empty:
            pass
        finally:
            # Always reschedule, or one bad message would freeze the window.
            self.root.after(100, self._pump)

    def _cancel(self):
        if self.busy and messagebox.askyesno(
                "kindle-acquire", "Stop the running operation?\n"
                "The output folder will be incomplete."):
            self.cancel.set()
            self.status_text.set("Cancelling...")

    def _close(self):
        if self.busy and not messagebox.askyesno(
                "kindle-acquire", "An operation is running. Close and abandon it?"):
            return
        self.root.destroy()

    # --- device ----------------------------------------------------------------------
    def detect(self):
        def step():
            from .device import detect
            self.queue.put(("device", detect()))
        self.device_text.set("Looking for a Kindle...")
        self._start("Detect", [step])

    def _show_devices(self, found):
        from .device import describe
        self.found = found
        self.device_text.set(describe(found))

    # --- steps (run on the worker thread) ----------------------------------------------
    def _fresh(self, device):
        current, found = jobs.recheck(device)
        self.queue.put(("device", found))
        return current

    def _step_image(self, device, out, case, verify_source):
        def step():
            self.queue.put(("output", out))
            jobs.run_image(self._fresh(device), out, case, verify_source, self._say)
        return step

    def _step_logical(self, device, out, case):
        def step():
            self.queue.put(("output", out))
            jobs.run_logical(self._fresh(device), out, case, self._say)
        return step

    def _step_extract(self, source, out, case):
        def step():
            self.queue.put(("output", out))
            jobs.run_extract(source, out, case, self._say)
        return step

    def _step_parse(self, acquisition, known):
        def step():
            self.queue.put(("progress", ("Parsing %s" % acquisition, 0, None)))
            self.queue.put(("output", jobs.run_parse(acquisition, known, self._say)))
        return step

    def _step_verify(self, acquisition):
        def step():
            self.queue.put(("progress", ("Verifying %s" % acquisition, 0, None)))
            jobs.run_verify(acquisition, self._say)
        return step

    # --- actions (run on the main thread) --------------------------------------------------
    def _need_admin(self):
        if self.admin:
            return True
        if messagebox.askyesno(
                "kindle-acquire",
                "Imaging reads the raw disk and needs administrator rights.\n\n"
                "Restart kindle-acquire as administrator?"):
            if relaunch_elevated():
                self.root.destroy()
            else:
                messagebox.showerror("kindle-acquire", "The elevated restart was refused.")
        return False

    def image(self):
        device = self._device()
        if device and self._need_admin():
            out = self._new_out("image", device)
            self._start("Image the disk", [
                self._step_image(device, out, self._case(), self.verify_source.get())])

    def full(self):
        device = self._device()
        if device and self._need_admin():
            image_out = self._new_out("image", device)
            extract_out = self._new_out("extract", device)
            case = self._case()
            self._start("Full acquisition", [
                self._step_image(device, image_out, case, self.verify_source.get()),
                self._step_extract(image_out, extract_out, case),
                self._step_parse(extract_out, self.known.get().strip())])

    def logical(self):
        device = self._device()
        if not device:
            return
        if not device["volumes"]:
            messagebox.showwarning("kindle-acquire", "The Kindle disk has no mounted volume.")
            return
        protected = device.get("usb_write_protect_policy") == 1 or device["disk"]["is_read_only"]
        if not protected and not messagebox.askyesno(
                "kindle-acquire",
                "The volume is mounted read-write.\n\n"
                "Copying files through Windows changes access dates on the device. "
                "For evidence, use a write blocker and image the disk.\n\n"
                "Copy the files anyway?"):
            return
        self._start("Copy files", [
            self._step_logical(device, self._new_out("logical", device), self._case())])

    def extract(self):
        source = self._ask_folder("Image acquisition folder")
        if source:
            source = os.path.normpath(source)
            label = _safe(self.evidence_id.get().strip()) or os.path.basename(source)
            out = str(Path(self.out_base.get().strip())
                      / ("%s_extract_%s" % (label, time.strftime("%Y%m%d_%H%M%S"))))
            self._start("Extract from an image", [self._step_extract(source, out, self._case())])

    def parse(self):
        acquisition = self._ask_folder("Logical or extracted acquisition folder")
        if acquisition:
            self._start("Parse", [
                self._step_parse(os.path.normpath(acquisition), self.known.get().strip())])

    def verify(self):
        acquisition = self._ask_folder("Acquisition folder")
        if acquisition:
            self._start("Verify", [self._step_verify(os.path.normpath(acquisition))])


def main():
    jobs.set_app_id()
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    try:
        root.iconbitmap(jobs.asset("keat.ico"))
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
