"""
Insider Trading Scanner — tkinter GUI
Launch:  python gui.py   or   python main.py --gui
"""
import logging
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

sys.path.insert(0, str(Path(__file__).parent))
import config

# ── Queue-based logging handler ───────────────────────────────────────────────

class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


# ── Colour palette ────────────────────────────────────────────────────────────

BG       = "#1e1e2e"
PANEL    = "#2a2a3e"
ACCENT   = "#7c83fd"
SUCCESS  = "#a6e3a1"
WARNING  = "#f9e2af"
ERROR    = "#f38ba8"
FG       = "#cdd6f4"
FG_DIM   = "#6c7086"
BTN_BG   = "#45475a"
BTN_FG   = "#cdd6f4"
RED_ROW  = "#f38ba8"


# ── Main application window ───────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Insider Trading Scanner")
        self.geometry("860x720")
        self.minsize(700, 580)
        self.configure(bg=BG)

        self._log_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._output_path: str | None = None

        self._setup_style()
        self._build_ui()
        self._poll()

    # ── Style ─────────────────────────────────────────────────────────────────

    def _setup_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".",
                     background=BG, foreground=FG,
                     fieldbackground=PANEL, troughcolor=PANEL,
                     selectbackground=ACCENT, selectforeground=BG,
                     font=("Segoe UI", 10))

        s.configure("TFrame",     background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabelframe", background=PANEL, foreground=FG, bordercolor=ACCENT)
        s.configure("TLabelframe.Label", background=PANEL, foreground=ACCENT,
                     font=("Segoe UI", 10, "bold"))

        s.configure("TLabel",    background=BG,    foreground=FG)
        s.configure("Dim.TLabel", background=BG,   foreground=FG_DIM,
                     font=("Segoe UI", 9))
        s.configure("Panel.TLabel", background=PANEL, foreground=FG)
        s.configure("Accent.TLabel", background=BG, foreground=ACCENT,
                     font=("Segoe UI", 13, "bold"))
        s.configure("Status.TLabel", background=PANEL, foreground=FG,
                     font=("Segoe UI", 9))

        s.configure("TEntry",    fieldbackground=PANEL, foreground=FG,
                     insertcolor=FG, bordercolor=ACCENT)

        s.configure("TRadiobutton", background=BG, foreground=FG,
                     indicatorcolor=ACCENT)
        s.map("TRadiobutton", background=[("active", BG)])

        s.configure("TButton",   background=BTN_BG, foreground=BTN_FG,
                     borderwidth=0, focusthickness=0,
                     font=("Segoe UI", 10))
        s.map("TButton",
              background=[("active", ACCENT), ("disabled", PANEL)],
              foreground=[("active", BG),     ("disabled", FG_DIM)])

        s.configure("Start.TButton", background=ACCENT, foreground=BG,
                     font=("Segoe UI", 11, "bold"))
        s.map("Start.TButton",
              background=[("active", "#9ba3fd"), ("disabled", PANEL)],
              foreground=[("active", BG),        ("disabled", FG_DIM)])

        s.configure("Stop.TButton", background="#f38ba8", foreground=BG,
                     font=("Segoe UI", 11, "bold"))
        s.map("Stop.TButton",
              background=[("active", "#f5a3b8"), ("disabled", PANEL)],
              foreground=[("active", BG),        ("disabled", FG_DIM)])

        s.configure("TProgressbar",
                     troughcolor=PANEL, background=ACCENT,
                     bordercolor=PANEL, lightcolor=ACCENT, darkcolor=ACCENT)

        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])

    # ── UI layout ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        root_pad = {"padx": 14, "pady": 10}

        # ── Title ─────────────────────────────────────────────────────────────
        ttk.Label(self, text="Insider Trading Scanner",
                  style="Accent.TLabel").pack(**root_pad, anchor="w")

        # ── Config panel ──────────────────────────────────────────────────────
        cfg = ttk.LabelFrame(self, text="Scan Configuration", padding=12)
        cfg.pack(fill="x", padx=14, pady=(0, 6))

        # Mode row
        mode_row = ttk.Frame(cfg)
        mode_row.pack(fill="x", pady=(0, 8))
        ttk.Label(mode_row, text="Mode:", width=10).pack(side="left")
        self._mode = tk.StringVar(value="watchlist")
        ttk.Radiobutton(mode_row, text="Watchlist Stocks",
                         variable=self._mode, value="watchlist",
                         command=self._on_mode_change).pack(side="left", padx=(0, 20))
        ttk.Radiobutton(mode_row, text="Full Market Scan",
                         variable=self._mode, value="market",
                         command=self._on_mode_change).pack(side="left")

        # Tickers row
        ticker_row = ttk.Frame(cfg)
        ticker_row.pack(fill="x", pady=(0, 8))
        ttk.Label(ticker_row, text="Tickers:", width=10).pack(side="left")
        self._ticker_var = tk.StringVar(value="AAPL, MSFT, NVDA")
        self._ticker_entry = ttk.Entry(ticker_row, textvariable=self._ticker_var, width=55)
        self._ticker_entry.pack(side="left", fill="x", expand=True)
        ttk.Label(ticker_row, text=" comma-separated",
                  style="Dim.TLabel").pack(side="left")

        # Date range row
        date_row = ttk.Frame(cfg)
        date_row.pack(fill="x", pady=(0, 8))
        ttk.Label(date_row, text="From:", width=10).pack(side="left")
        default_from = (datetime.now() - timedelta(days=config.LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        default_to   = datetime.now().strftime("%Y-%m-%d")
        self._from_var = tk.StringVar(value=default_from)
        self._to_var   = tk.StringVar(value=default_to)
        ttk.Entry(date_row, textvariable=self._from_var, width=13).pack(side="left")
        ttk.Label(date_row, text="  To:", width=5).pack(side="left")
        ttk.Entry(date_row, textvariable=self._to_var, width=13).pack(side="left")
        ttk.Label(date_row, text="  (YYYY-MM-DD)",
                  style="Dim.TLabel").pack(side="left")

        # Output file row
        out_row = ttk.Frame(cfg)
        out_row.pack(fill="x", pady=(0, 4))
        ttk.Label(out_row, text="Output:", width=10).pack(side="left")
        self._out_var = tk.StringVar(value=config.OUTPUT_FILE)
        ttk.Entry(out_row, textvariable=self._out_var, width=52).pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Browse", width=8,
                   command=self._browse_output).pack(side="left", padx=(6, 0))

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=14, pady=6)
        self._start_btn = ttk.Button(btn_row, text="▶  Start Scan",
                                      style="Start.TButton", width=18,
                                      command=self._start)
        self._start_btn.pack(side="left", ipady=5)
        self._stop_btn = ttk.Button(btn_row, text="■  Stop",
                                     style="Stop.TButton", width=12,
                                     command=self._stop, state="disabled")
        self._stop_btn.pack(side="left", padx=(10, 0), ipady=5)

        # ── Progress ──────────────────────────────────────────────────────────
        prog_frame = ttk.Frame(self, style="Panel.TFrame")
        prog_frame.pack(fill="x", padx=14, pady=(4, 0))
        self._prog = ttk.Progressbar(prog_frame, mode="indeterminate", length=400)
        self._prog.pack(side="left", fill="x", expand=True, padx=(8, 8), pady=8)
        self._pct_label = ttk.Label(prog_frame, text="  0%  ",
                                     style="Panel.TLabel", width=6)
        self._pct_label.pack(side="left")
        self._status_label = ttk.Label(prog_frame, text="Idle",
                                        style="Status.TLabel", width=30)
        self._status_label.pack(side="left", padx=(4, 8))

        # ── Log area ──────────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self, text="Log Output", padding=4)
        log_frame.pack(fill="both", expand=True, padx=14, pady=6)

        self._log_text = scrolledtext.ScrolledText(
            log_frame, height=14, wrap="word",
            bg=PANEL, fg=FG, insertbackground=FG,
            font=("Consolas", 9), borderwidth=0,
            state="disabled",
        )
        self._log_text.pack(fill="both", expand=True)
        # Tag colours
        self._log_text.tag_configure("INFO",    foreground=FG)
        self._log_text.tag_configure("WARNING", foreground=WARNING)
        self._log_text.tag_configure("ERROR",   foreground=ERROR)
        self._log_text.tag_configure("found",   foreground=SUCCESS)

        # ── Results bar ───────────────────────────────────────────────────────
        res_frame = ttk.Frame(self, style="Panel.TFrame")
        res_frame.pack(fill="x", padx=14, pady=(0, 8))

        self._results_label = ttk.Label(
            res_frame,
            text="Scanned: —   |   Buys Found: —   |   Cluster Buys: —",
            style="Panel.TLabel",
        )
        self._results_label.pack(side="left", padx=10, pady=6)

        self._open_btn = ttk.Button(res_frame, text="Open Report",
                                     command=self._open_output, state="disabled")
        self._open_btn.pack(side="right", padx=10, pady=6)

        self._on_mode_change()

    # ── Mode toggle ───────────────────────────────────────────────────────────

    def _on_mode_change(self):
        if self._mode.get() == "watchlist":
            self._ticker_entry.configure(state="normal")
        else:
            self._ticker_entry.configure(state="disabled")

    # ── Browse ────────────────────────────────────────────────────────────────

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
            initialfile=os.path.basename(self._out_var.get()),
            initialdir=os.path.dirname(self._out_var.get()),
        )
        if path:
            self._out_var.set(path)

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def _start(self):
        # Validate dates
        try:
            date_from = datetime.strptime(self._from_var.get().strip(), "%Y-%m-%d")
            date_to   = datetime.strptime(self._to_var.get().strip(), "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("Invalid dates",
                                  "Dates must be in YYYY-MM-DD format.")
            return
        if date_from > date_to:
            messagebox.showerror("Invalid range", "'From' must be before 'To'.")
            return

        tickers = None
        if self._mode.get() == "watchlist":
            raw = self._ticker_var.get().strip()
            if not raw:
                messagebox.showerror("No tickers", "Enter at least one ticker symbol.")
                return
            tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]

        output_path = self._out_var.get().strip()
        if not output_path:
            messagebox.showerror("No output file", "Specify an output Excel path.")
            return

        # Reset UI
        self._clear_log()
        self._set_status("Connecting to SEC EDGAR ...", 0, total=0)
        self._results_label.config(
            text="Scanned: —   |   Buys Found: —   |   Cluster Buys: —"
        )
        self._open_btn.config(state="disabled")
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._output_path = output_path

        # Wire up log handler
        self._stop_event.clear()
        handler = _QueueHandler(self._log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                                datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

        # Launch background thread
        self._worker = threading.Thread(
            target=self._thread_run,
            args=(date_from, date_to, tickers, output_path),
            daemon=True,
        )
        self._worker.start()

    def _stop(self):
        self._stop_event.set()
        self._set_status("Stopping — waiting for current requests ...", -1)
        self._stop_btn.config(state="disabled")

    # ── Background thread ─────────────────────────────────────────────────────

    def _thread_run(self, date_from, date_to, tickers, output_path):
        from main import run_pipeline
        import config as _cfg
        orig_out = _cfg.OUTPUT_FILE
        _cfg.OUTPUT_FILE = output_path
        try:
            def _progress(done, total):
                self._log_queue.put(("progress", (done, total)))

            df, scanned = run_pipeline(
                date_from, date_to,
                tickers=tickers,
                progress_cb=_progress,
                stop_event=self._stop_event,
            )

            buys    = len(df)
            clusters = int(df["cluster_buy"].sum()) if not df.empty and "cluster_buy" in df.columns else 0
            self._log_queue.put(("done", (scanned, buys, clusters, output_path)))

        except Exception:
            import traceback
            self._log_queue.put(("error", traceback.format_exc()))
        finally:
            _cfg.OUTPUT_FILE = orig_out

    # ── Polling loop (main thread) ─────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                kind, payload = self._log_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    done, total = payload
                    if total > 0:
                        pct = int(done / total * 100)
                        self._set_status(f"Parsing filings ... {done:,}/{total:,}", pct, total)
                    else:
                        self._set_status("Fetching filing index ...", -1)
                elif kind == "done":
                    scanned, buys, clusters, path = payload
                    self._on_done(scanned, buys, clusters, path)
                elif kind == "error":
                    self._on_error(payload)
        except queue.Empty:
            pass
        self.after(120, self._poll)

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _set_status(self, text: str, pct: int, total: int = 1):
        self._status_label.config(text=text[:48])
        if pct < 0:
            # Indeterminate
            self._prog.config(mode="indeterminate")
            self._prog.start(12)
            self._pct_label.config(text=" ... ")
        else:
            self._prog.stop()
            self._prog.config(mode="determinate", value=pct)
            self._pct_label.config(text=f"{pct:3d}%" if total > 0 else "  0%")

    def _append_log(self, msg: str):
        self._log_text.config(state="normal")
        tag = "INFO"
        if "[WARNING]" in msg:
            tag = "WARNING"
        elif "[ERROR]" in msg or "Error" in msg or "Failed" in msg:
            tag = "ERROR"
        elif "FOUND" in msg or "qualifying" in msg or "cluster" in msg.lower():
            tag = "found"
        self._log_text.insert("end", msg + "\n", tag)
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _clear_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    def _on_done(self, scanned, buys, clusters, path):
        self._prog.stop()
        self._prog.config(mode="determinate", value=100)
        self._pct_label.config(text="100%")
        self._set_status("Complete", 100)
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._results_label.config(
            text=f"Scanned: {scanned:,}   |   Buys Found: {buys:,}   |   Cluster Buys: {clusters:,}"
        )
        if os.path.exists(path):
            self._open_btn.config(state="normal")
        self._remove_log_handler()

    def _on_error(self, tb: str):
        self._prog.stop()
        self._prog.config(mode="determinate", value=0)
        self._set_status("Error — see log", 0)
        self._append_log("[ERROR] Pipeline failed:\n" + tb)
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._remove_log_handler()

    def _remove_log_handler(self):
        if hasattr(self, "_log_handler"):
            logging.getLogger().removeHandler(self._log_handler)
            del self._log_handler

    def _open_output(self):
        path = self._output_path or self._out_var.get()
        if os.path.exists(path):
            os.startfile(path)
        else:
            messagebox.showwarning("File not found", f"Cannot open:\n{path}")

    # ── Window close ──────────────────────────────────────────────────────────

    def destroy(self):
        self._stop_event.set()
        super().destroy()


# ── Entry points ──────────────────────────────────────────────────────────────

def launch_gui():
    from main import setup_logging
    setup_logging()
    app = App()
    app.mainloop()


if __name__ == "__main__":
    launch_gui()
