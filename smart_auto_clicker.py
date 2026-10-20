import ctypes
import json
import logging
import logging.handlers
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import pyautogui
from pynput import keyboard
from PIL import Image, ImageDraw

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
if not IS_MAC:
    import pystray

# The PyAutoGUI corner fail-safe is completely disabled: FAILSAFE is off AND its list of
# trigger points is emptied, so touching any screen corner can never stop a run.
# Ways to stop: F6, the tray menu, or (optionally) the Esc key.
pyautogui.FAILSAFE = False
pyautogui.FAILSAFE_POINTS.clear()
pyautogui.PAUSE = 0.001

MIN_INTERVAL = 0.01
MAX_INTERVAL = 86400.0        # 24 hours in seconds (24 * 60 * 60)
POLL_SEC = 0.05               # how often to wake during delays to check for a stop request
UI_POLL_MS = 25               # how often the main thread drains the UI callback queue
MAX_CONSECUTIVE_FAILURES = 5  # abort only after this many failed clicks in a row
MAX_STEPS = 1000              # sanity limit when loading presets

UNIT_MULTIPLIERS = {
    "sec": 1.0,
    "min": 60.0,
    "hr": 3600.0,
}


def setup_logging():
    """Log to %LOCALAPPDATA%\\SmartAutoClicker\\log.txt (a windowed .exe has no console to print to)."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(logging.NullHandler())  # safe even if no file can be opened
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        log_dir = os.path.join(base, "SmartAutoClicker")
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "log.txt"), maxBytes=500_000, backupCount=1, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root_logger.addHandler(handler)
    except OSError:
        pass


def format_duration(seconds):
    """Format seconds into a readable string (e.g., 1h 30m 5s or 0.5s)."""
    if seconds < 60:
        return f"{seconds:g}s"
    total_sec = int(round(seconds))
    hrs, rem = divmod(total_sec, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if hrs:
        parts.append(f"{hrs}h")
    if mins:
        parts.append(f"{mins}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def format_countdown(seconds):
    """Format remaining seconds as HH:MM:SS for the live status bar."""
    total_sec = max(0, int(seconds))
    hrs, rem = divmod(total_sec, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


class AutoClickerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Smart Auto Clicker")
        self.root.attributes("-topmost", True)

        self.ui_queue = queue.Queue()
        self.closing = False

        self.stop_event = None
        self.worker_thread = None

        self.tray_icon = None
        self.listener = None
        self.picking = False
        self.steps = []

        # Plain bool mirror of the Esc checkbox: the hotkey listener runs on another
        # thread and must not touch Tk variables.
        self.esc_enabled = False
        self._minimized_for_run = False

        # Anything raised inside a Tk callback gets logged instead of vanishing.
        self.root.report_callback_exception = self._log_tk_exception

        if IS_MAC:
            self.root.protocol("WM_DELETE_WINDOW", self.quit_app)
        else:
            self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self.build_ui()

        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth() + 16, self.root.winfo_reqheight() + 16)

        self.root.after(UI_POLL_MS, self._process_ui_queue)

        self.start_hotkey_listener()
        if not IS_MAC:
            self.init_system_tray()

    def _log_tk_exception(self, exc_type, exc_value, exc_tb):
        logging.error("Unhandled error in Tk callback", exc_info=(exc_type, exc_value, exc_tb))

    @property
    def running(self):
        return (
            self.worker_thread is not None
            and self.worker_thread.is_alive()
            and self.stop_event is not None
            and not self.stop_event.is_set()
        )

    @property
    def worker_busy(self):
        return self.worker_thread is not None and self.worker_thread.is_alive()

    # ---------------------------------------------------- Windows Anti-Sleep
    def _set_keep_awake(self, keep_awake):
        """Prevent Windows from sleeping during multi-hour delays."""
        if not IS_WIN:
            return
        try:
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if keep_awake else ES_CONTINUOUS
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
        except Exception:
            pass

    # ---------------------------------------------------- Thread-Safe UI Queue
    def _schedule(self, fn):
        if not self.closing:
            self.ui_queue.put(fn)

    def _process_ui_queue(self):
        if self.closing:
            return
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                logging.exception("Error in queued UI callback")
        if not self.closing:
            self.root.after(UI_POLL_MS, self._process_ui_queue)

    # ---------------------------------------------------------------- UI
    def build_ui(self):
        pad = {"padx": 12, "pady": 6}

        # --- Add / Edit Step Section ---
        input_frame = ttk.LabelFrame(self.root, text="Add or Edit Click Step (Up to 24h)")
        input_frame.pack(fill="x", **pad)

        ttk.Label(input_frame, text="X:").grid(row=0, column=0, padx=(8, 2), pady=6, sticky="e")
        self.x_var = tk.StringVar(value="500")
        self.x_entry = ttk.Entry(input_frame, textvariable=self.x_var, width=6)
        self.x_entry.grid(row=0, column=1, padx=2, pady=6, sticky="w")

        ttk.Label(input_frame, text="Y:").grid(row=0, column=2, padx=(6, 2), pady=6, sticky="e")
        self.y_var = tk.StringVar(value="500")
        self.y_entry = ttk.Entry(input_frame, textvariable=self.y_var, width=6)
        self.y_entry.grid(row=0, column=3, padx=2, pady=6, sticky="w")

        ttk.Label(input_frame, text="Delay:").grid(row=0, column=4, padx=(6, 2), pady=6, sticky="e")
        self.delay_var = tk.StringVar(value="0.5")
        self.delay_entry = ttk.Entry(input_frame, textvariable=self.delay_var, width=6)
        self.delay_entry.grid(row=0, column=5, padx=2, pady=6, sticky="w")

        self.unit_var = tk.StringVar(value="sec")
        self.unit_combo = ttk.Combobox(
            input_frame, textvariable=self.unit_var, values=("sec", "min", "hr"), width=4, state="readonly"
        )
        self.unit_combo.grid(row=0, column=6, padx=(2, 8), pady=6, sticky="w")

        action_frame = ttk.Frame(input_frame)
        action_frame.grid(row=1, column=0, columnspan=7, pady=(2, 8))

        self.pick_btn = ttk.Button(
            action_frame, text="Record Pos & Add (3s)", command=self.pick_and_add_location
        )
        self.pick_btn.grid(row=0, column=0, padx=4)

        self.add_btn = ttk.Button(
            action_frame, text="Add Manual Step", command=self.add_manual_step
        )
        self.add_btn.grid(row=0, column=1, padx=4)

        self.update_btn = ttk.Button(
            action_frame, text="Update Selected", command=self.update_selected_step
        )
        self.update_btn.grid(row=0, column=2, padx=4)

        # --- Sequence List (Treeview) ---
        list_frame = ttk.LabelFrame(self.root, text="Click Sequence Queue")
        list_frame.pack(fill="both", expand=True, **pad)

        columns = ("step", "x", "y", "delay")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=5, selectmode="browse")
        self.tree.heading("step", text="#")
        self.tree.heading("x", text="X Pos")
        self.tree.heading("y", text="Y Pos")
        self.tree.heading("delay", text="Delay After")

        self.tree.column("step", width=40, anchor="center")
        self.tree.column("x", width=75, anchor="center")
        self.tree.column("y", width=75, anchor="center")
        self.tree.column("delay", width=110, anchor="center")

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        scrollbar.pack(side="right", fill="y", padx=(0, 8), pady=6)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        # --- Queue Management & File Buttons ---
        queue_btn_frame = ttk.Frame(self.root)
        queue_btn_frame.pack(fill="x", padx=12, pady=2)

        self.up_btn = ttk.Button(queue_btn_frame, text="▲ Move Up", width=10, command=lambda: self.move_step(-1))
        self.up_btn.pack(side="left", padx=2)

        self.down_btn = ttk.Button(queue_btn_frame, text="▼ Move Down", width=10, command=lambda: self.move_step(1))
        self.down_btn.pack(side="left", padx=2)

        self.del_btn = ttk.Button(queue_btn_frame, text="Remove", width=8, command=self.remove_selected_step)
        self.del_btn.pack(side="left", padx=2)

        self.clear_btn = ttk.Button(queue_btn_frame, text="Clear", width=7, command=self.clear_steps)
        self.clear_btn.pack(side="left", padx=2)

        self.save_btn = ttk.Button(queue_btn_frame, text="Save", width=8, command=self.save_preset)
        self.save_btn.pack(side="right", padx=2)

        self.load_btn = ttk.Button(queue_btn_frame, text="Load", width=8, command=self.load_preset)
        self.load_btn.pack(side="right", padx=2)

        # --- Loop Settings Section ---
        loop_frame = ttk.LabelFrame(self.root, text="Loop Settings")
        loop_frame.pack(fill="x", **pad)

        self.loop_var = tk.BooleanVar(value=True)
        self.loop_check = ttk.Checkbutton(
            loop_frame,
            text="Repeat sequence continuously",
            variable=self.loop_var,
            command=self.toggle_loop_inputs,
        )
        self.loop_check.grid(row=0, column=0, columnspan=3, padx=8, pady=(6, 2), sticky="w")

        ttk.Label(loop_frame, text="Delay Between Loops:").grid(row=1, column=0, padx=(8, 4), pady=(2, 8), sticky="w")
        self.loop_delay_var = tk.StringVar(value="0")
        self.loop_delay_entry = ttk.Entry(loop_frame, textvariable=self.loop_delay_var, width=8)
        self.loop_delay_entry.grid(row=1, column=1, padx=2, pady=(2, 8), sticky="w")

        self.loop_unit_var = tk.StringVar(value="sec")
        self.loop_unit_combo = ttk.Combobox(
            loop_frame, textvariable=self.loop_unit_var, values=("sec", "min", "hr"), width=4, state="readonly"
        )
        self.loop_unit_combo.grid(row=1, column=2, padx=(2, 8), pady=(2, 8), sticky="w")

        # --- Stop Options Section ---
        safety_frame = ttk.LabelFrame(self.root, text="Stop Options")
        safety_frame.pack(fill="x", **pad)

        self.esc_var = tk.BooleanVar(value=False)
        self.esc_check = ttk.Checkbutton(
            safety_frame,
            text="Esc key stops the clicker (works in any app)",
            variable=self.esc_var,
            command=self._sync_safety_options,
        )
        self.esc_check.pack(anchor="w", padx=8, pady=6)

        # --- Status & Main Controls ---
        self.status_label = ttk.Label(
            self.root, text="Status: STOPPED", foreground="#d9534f", font=("Segoe UI", 10, "bold")
        )
        self.status_label.pack(pady=6)

        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(pady=4)

        self.toggle_btn = ttk.Button(btn_frame, text="Start / Stop (F6)", command=self.toggle_clicking)
        self.toggle_btn.grid(row=0, column=0, padx=6)

        if IS_MAC:
            ttk.Button(btn_frame, text="Quit", command=self.quit_app).grid(row=0, column=1, padx=6)
        else:
            ttk.Button(btn_frame, text="Minimize to Tray", command=self.hide_to_tray).grid(row=0, column=1, padx=6)

        ttk.Label(
            self.root,
            text="Press F6 (or use the tray menu) to start/stop. Screen corners never stop it.",
            font=("Segoe UI", 8), justify="center", foreground="gray",
        ).pack(padx=12, pady=(4, 10))

    def _sync_safety_options(self):
        self.esc_enabled = bool(self.esc_var.get())

    def toggle_loop_inputs(self):
        if self.worker_busy:
            return
        enabled = self.loop_var.get()
        self.loop_delay_entry.config(state="normal" if enabled else "disabled")
        self.loop_unit_combo.config(state="readonly" if enabled else "disabled")

    # --------------------------------------------------- Presets (Save / Load)
    def save_preset(self):
        if self.worker_busy:
            return
        if not self.steps:
            messagebox.showwarning("Empty Queue", "Add at least one step before saving.")
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON Preset", "*.json"), ("All Files", "*.*")],
            title="Save Click Sequence Preset"
        )
        if not file_path:
            return

        try:
            data = {
                "steps": self.steps,
                "loop_enabled": self.loop_var.get(),
                "loop_delay": self.loop_delay_var.get(),
                "loop_unit": self.loop_unit_var.get(),
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            messagebox.showinfo("Success", "Preset saved successfully!")
        except Exception as e:
            messagebox.showerror("Save Error", f"Could not save file:\n{e}")

    def _validate_loaded_step(self, step):
        """Return a clean step dict or raise ValueError. Never trusts the file's types."""
        if not isinstance(step, dict):
            raise ValueError("not a valid step object.")
        x, y, delay = step.get("x"), step.get("y"), step.get("delay")
        for name, v in (("x", x), ("y", y)):
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"'{name}' must be a whole number.")
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay):
            raise ValueError("'delay' must be a number.")
        if not (MIN_INTERVAL <= delay <= MAX_INTERVAL):
            raise ValueError("'delay' must be between 0.01 seconds and 24 hours.")
        self._check_on_screen(x, y)
        return {"x": x, "y": y, "delay": float(delay)}

    def load_preset(self):
        if self.worker_busy:
            return

        file_path = filedialog.askopenfilename(
            filetypes=[("JSON Preset", "*.json"), ("All Files", "*.*")],
            title="Load Click Sequence Preset"
        )
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                raise ValueError("Invalid preset file format.")

            raw_steps = data.get("steps")
            if not isinstance(raw_steps, list) or not raw_steps:
                raise ValueError("The preset contains no steps.")
            if len(raw_steps) > MAX_STEPS:
                raise ValueError(f"Too many steps (limit is {MAX_STEPS}).")

            # Validate everything first; only touch app state if the whole file is good.
            new_steps = []
            for i, raw in enumerate(raw_steps, start=1):
                try:
                    new_steps.append(self._validate_loaded_step(raw))
                except ValueError as e:
                    raise ValueError(f"Step {i}: {e}")

            loop_enabled = data.get("loop_enabled", True)
            if not isinstance(loop_enabled, bool):
                raise ValueError("'loop_enabled' must be true or false.")

            loop_unit = data.get("loop_unit", "sec")
            if loop_unit not in UNIT_MULTIPLIERS:
                raise ValueError("'loop_unit' must be sec, min or hr.")

            loop_delay = str(data.get("loop_delay", "0"))
            try:
                loop_seconds = float(loop_delay) * UNIT_MULTIPLIERS[loop_unit]
            except ValueError:
                raise ValueError("'loop_delay' must be a number.")
            if not (0.0 <= loop_seconds <= MAX_INTERVAL):  # also rejects NaN
                raise ValueError("'loop_delay' must be between 0 seconds and 24 hours.")

            self.steps = new_steps
            self.loop_var.set(loop_enabled)
            self.loop_delay_var.set(loop_delay)
            self.loop_unit_var.set(loop_unit)

            self.refresh_tree()
            self.toggle_loop_inputs()
            messagebox.showinfo("Success", "Preset loaded successfully!")
        except Exception as e:
            logging.exception("Preset load failed")
            messagebox.showerror("Load Error", f"Could not load file:\n{e}")

    # --------------------------------------------------- Step & Loop Validation
    def get_delay_in_seconds(self):
        try:
            raw_val = float(self.delay_var.get())
        except ValueError:
            raise ValueError("Please enter a valid number for Step Delay.")

        unit = self.unit_var.get()
        multiplier = UNIT_MULTIPLIERS.get(unit, 1.0)
        delay_sec = raw_val * multiplier

        if not (MIN_INTERVAL <= delay_sec <= MAX_INTERVAL):
            raise ValueError("Step Delay must be between 0.01 seconds and 24 hours (86,400s).")
        return delay_sec

    def get_loop_delay_in_seconds(self):
        if not self.loop_var.get():
            return 0.0
        try:
            raw_val = float(self.loop_delay_var.get())
        except ValueError:
            raise ValueError("Please enter a valid number for Delay Between Loops.")

        unit = self.loop_unit_var.get()
        multiplier = UNIT_MULTIPLIERS.get(unit, 1.0)
        loop_delay_sec = raw_val * multiplier

        if not (0.0 <= loop_delay_sec <= MAX_INTERVAL):
            raise ValueError("Delay Between Loops must be between 0 seconds and 24 hours (86,400s).")
        return loop_delay_sec

    def _check_on_screen(self, x, y):
        width, height = pyautogui.size()
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(
                f"Coordinates ({x}, {y}) must be inside the primary screen (0-{width - 1}, 0-{height - 1})."
            )

    def parse_step_inputs(self, custom_xy=None):
        delay = self.get_delay_in_seconds()

        if custom_xy is not None:
            x, y = custom_xy
        else:
            try:
                x, y = int(self.x_var.get()), int(self.y_var.get())
            except ValueError:
                raise ValueError("Please enter whole numbers for X and Y.")

        self._check_on_screen(x, y)

        return {"x": x, "y": y, "delay": delay}

    def refresh_tree(self, select_idx=None):
        self.tree.delete(*self.tree.get_children())
        for idx, step in enumerate(self.steps):
            self.tree.insert(
                "", "end", iid=str(idx),
                values=(idx + 1, step["x"], step["y"], format_duration(step["delay"]))
            )
        if select_idx is not None and 0 <= select_idx < len(self.steps):
            self.tree.selection_set(str(select_idx))

    def add_manual_step(self):
        if self.worker_busy:
            return
        try:
            step = self.parse_step_inputs()
        except ValueError as e:
            messagebox.showerror("Invalid Step", str(e))
            return
        self.steps.append(step)
        self.refresh_tree(len(self.steps) - 1)

    def update_selected_step(self):
        if self.worker_busy:
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select a Step", "Click a step in the list first to update it.")
            return
        idx = int(sel[0])
        try:
            step = self.parse_step_inputs()
        except ValueError as e:
            messagebox.showerror("Invalid Step", str(e))
            return
        self.steps[idx] = step
        self.refresh_tree(idx)

    def remove_selected_step(self):
        if self.worker_busy:
            return
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        del self.steps[idx]
        self.refresh_tree(min(idx, len(self.steps) - 1))

    def clear_steps(self):
        if self.worker_busy:
            return
        self.steps.clear()
        self.refresh_tree()

    def move_step(self, direction):
        if self.worker_busy:
            return
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        new_idx = idx + direction
        if 0 <= new_idx < len(self.steps):
            self.steps[idx], self.steps[new_idx] = self.steps[new_idx], self.steps[idx]
            self.refresh_tree(new_idx)

    def on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        step = self.steps[int(sel[0])]
        self.x_var.set(str(step["x"]))
        self.y_var.set(str(step["y"]))
        delay = step["delay"]
        if delay >= 3600 and delay % 3600 == 0:
            self.delay_var.set(f"{delay / 3600:g}")
            self.unit_var.set("hr")
        elif delay >= 60 and delay % 60 == 0:
            self.delay_var.set(f"{delay / 60:g}")
            self.unit_var.set("min")
        else:
            self.delay_var.set(f"{delay:g}")
            self.unit_var.set("sec")

    # ------------------------------------------------- Position Recording
    def pick_and_add_location(self):
        if self.picking or self.worker_busy:
            return
        try:
            self.get_delay_in_seconds()
        except ValueError as e:
            messagebox.showerror("Invalid Delay", str(e))
            return

        self.picking = True
        self.pick_btn.config(state="disabled")
        self._countdown(3)

    def _countdown(self, remaining):
        if remaining > 0:
            self.pick_btn.config(text=f"Move mouse... {remaining}s")
            self.root.after(1000, lambda: self._countdown(remaining - 1))
            return

        x, y = pyautogui.position()
        self.pick_btn.config(text="Record Pos & Add (3s)", state="normal")
        self.picking = False

        try:
            step = self.parse_step_inputs(custom_xy=(x, y))
            self.x_var.set(str(x))
            self.y_var.set(str(y))
            self.steps.append(step)
            self.refresh_tree(len(self.steps) - 1)
        except ValueError as e:
            messagebox.showerror("Invalid Position", str(e))

    # --------------------------------------------------------- Clicking Loop
    def set_inputs_state(self, state):
        for widget in (
            self.x_entry, self.y_entry, self.delay_entry,
            self.pick_btn, self.add_btn, self.update_btn,
            self.up_btn, self.down_btn, self.del_btn, self.clear_btn,
            self.save_btn, self.load_btn, self.loop_check,
        ):
            widget.config(state=state)
        self.unit_combo.config(state="readonly" if state == "normal" else "disabled")

        if state == "normal":
            self.toggle_loop_inputs()
        else:
            self.loop_delay_entry.config(state="disabled")
            self.loop_unit_combo.config(state="disabled")

    def toggle_clicking(self):
        if self.running:
            self.stop_clicking()
        elif not self.worker_busy:
            self.start_clicking()

    def start_clicking(self):
        if self.worker_busy or self.picking:
            return
        if not self.steps:
            messagebox.showwarning("Empty Queue", "Please add at least one click position to the sequence first.")
            return

        try:
            loop_delay = self.get_loop_delay_in_seconds()
        except ValueError as e:
            messagebox.showerror("Invalid Loop Delay", str(e))
            return

        # Belt and braces: make sure nothing re-enabled the corner fail-safe.
        pyautogui.FAILSAFE = False
        pyautogui.FAILSAFE_POINTS.clear()

        steps_snapshot = [dict(s) for s in self.steps]
        loop_forever = self.loop_var.get()

        event = threading.Event()
        self.stop_event = event
        self.set_inputs_state("disabled")
        self._set_keep_awake(True)
        self.status_label.config(text="Status: RUNNING", foreground="#5cb85c")

        # Get the always-on-top window out of the way so it can't intercept clicks.
        # (Skipped if it's already hidden in the tray.)
        if self.root.state() == "normal":
            self.root.iconify()
            self._minimized_for_run = True

        self.worker_thread = threading.Thread(
            target=self.click_loop,
            args=(event, steps_snapshot, loop_forever, loop_delay),
            daemon=True,
        )
        self.worker_thread.start()

    def stop_clicking(self):
        if self.stop_event is not None and not self.stop_event.is_set():
            self.stop_event.set()
            if self.worker_busy:
                self.status_label.config(text="Status: STOPPING...", foreground="#f0ad4e")

    def _wait(self, event, delay, status_prefix):
        """Sleep for `delay` seconds; returns True if a stop was requested."""
        if delay <= 0:
            return event.is_set()

        deadline = time.monotonic() + delay
        last_shown_sec = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False

            if delay >= 1.0:
                rem_int = int(remaining) + 1
                if rem_int != last_shown_sec:
                    last_shown_sec = rem_int
                    countdown_str = format_countdown(rem_int)
                    self._schedule(
                        lambda p=status_prefix, c=countdown_str: self.status_label.config(
                            text=f"Status: RUNNING ({p} — {c} left)",
                            foreground="#5cb85c",
                        ) if (self.stop_event is event and not event.is_set()) else None
                    )

            if event.wait(min(remaining, POLL_SEC)):
                return True

    def click_loop(self, event, steps, loop_forever, loop_delay):
        error = None
        total = len(steps)
        failures = 0  # consecutive failed clicks
        try:
            while not event.is_set():
                for idx, step in enumerate(steps, start=1):
                    if event.is_set():
                        break
                    step_label = f"Step {idx}/{total}"
                    self._schedule(
                        lambda lbl=step_label: self.status_label.config(
                            text=f"Status: RUNNING ({lbl})", foreground="#5cb85c"
                        ) if (self.stop_event is event and not event.is_set()) else None
                    )

                    try:
                        pyautogui.click(step["x"], step["y"])
                        failures = 0
                    except Exception as e:
                        # A single failed click (e.g. a momentary OS hiccup) shouldn't end a
                        # multi-hour run: pause briefly, skip this click, and carry on.
                        failures += 1
                        logging.warning("Click failed (%d in a row): %s", failures, e)
                        if failures >= MAX_CONSECUTIVE_FAILURES:
                            raise RuntimeError(
                                f"{failures} clicks failed in a row. Last error: {e}"
                            ) from e
                        if event.wait(1.0):
                            break
                        continue

                    if self._wait(event, step["delay"], step_label):
                        break

                if not loop_forever or event.is_set():
                    break

                if loop_delay > 0:
                    self._schedule(
                        lambda: self.status_label.config(
                            text="Status: RUNNING (Loop Delay)", foreground="#5cb85c"
                        ) if (self.stop_event is event and not event.is_set()) else None
                    )
                    if self._wait(event, loop_delay, "Loop Delay"):
                        break
        except Exception as e:
            logging.exception("Click loop aborted")
            error = ("Clicker Error", f"Clicking stopped due to an error:\n{e}", "error")
        finally:
            event.set()
            self._schedule(lambda: self._on_loop_finished(event, error))

    def _on_loop_finished(self, event, error):
        if self.stop_event is event:
            self.stop_event = None
            self.worker_thread = None
            self._set_keep_awake(False)
            self.set_inputs_state("normal")
            self.status_label.config(text="Status: STOPPED", foreground="#d9534f")
            if self._minimized_for_run:
                self._minimized_for_run = False
                self.root.deiconify()
        if error:
            title, msg, kind = error
            (messagebox.showinfo if kind == "info" else messagebox.showerror)(title, msg)

    # ---------------------------------------------------- Global Hotkeys
    def start_hotkey_listener(self):
        def on_press(key):
            if key == keyboard.Key.f6:
                self._schedule(self.toggle_clicking)
            elif key == keyboard.Key.esc and self.esc_enabled and self.running:
                self._schedule(self.stop_clicking)

        self.listener = keyboard.Listener(on_press=on_press)
        self.listener.daemon = True
        self.listener.start()

    # ------------------------------------------------------ System Tray
    def create_tray_image(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((4, 4, 60, 60), fill=(33, 150, 243), outline=(255, 255, 255), width=4)
        draw.ellipse((22, 22, 42, 42), fill=(255, 255, 255))
        return img

    def init_system_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Open Settings", self.show_from_tray, default=True),
            pystray.MenuItem("Start / Stop (F6)", lambda icon, item: self._schedule(self.toggle_clicking)),
            pystray.MenuItem("Quit Completely", self.quit_app),
        )
        self.tray_icon = pystray.Icon("smart_auto_clicker", self.create_tray_image(), "Smart Auto Clicker (F6)", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def hide_to_tray(self):
        self.root.withdraw()

    def show_from_tray(self, icon=None, item=None):
        self._schedule(self.root.deiconify)

    def quit_app(self, icon=None, item=None):
        # May be called from the tray thread, so hand the real work to the main thread.
        self.ui_queue.put(self._shutdown)

    def _shutdown(self):
        if self.closing:
            return
        self.closing = True
        self._set_keep_awake(False)
        if self.stop_event is not None:
            self.stop_event.set()
        if self.listener:
            self.listener.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()


if __name__ == "__main__":
    setup_logging()
    root = tk.Tk()
    app = AutoClickerApp(root)
    root.mainloop()
