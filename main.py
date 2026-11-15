#!/usr/bin/env python3
"""
Burn SRT subtitles into a video using FFmpeg + Tkinter GUI.
Fast path: hardware encoders (NVENC / QSV / AMF / VideoToolbox) when available,
audio stream copy, minimal filters.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path


# ---------------------------------------------------------------------------
# Subtitle styles (libass force_style)
# Colours are ASS &HAABBGGRR (alpha inverted: 00=opaque)
# ---------------------------------------------------------------------------
STYLES = {
    "Classic White": (
        "FontName=Arial,FontSize=24,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,Outline=2,Shadow=1,Bold=0,Alignment=2,MarginV=30"
    ),
    "Yellow Outline": (
        "FontName=Arial,FontSize=26,PrimaryColour=&H0000FFFF,"
        "OutlineColour=&H00000000,Outline=3,Shadow=0,Bold=1,Alignment=2,MarginV=35"
    ),
    "Minimal Clean": (
        "FontName=Arial,FontSize=22,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H80000000,Outline=1,Shadow=0,Bold=0,Alignment=2,MarginV=25"
    ),
}

QUALITY_PRESETS = {
    "High (slower, smaller)": {"crf": 18, "cq": 18, "amf_q": 15},
    "Balanced (recommended)": {"crf": 23, "cq": 23, "amf_q": 22},
    "Fast (lower quality)": {"crf": 28, "cq": 28, "amf_q": 30},
}

# Used only as last-resort fallback if detection fails
_FALLBACK_HW = [
    ("CPU only (libx264)", "cpu"),
]


def which_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _ffmpeg_claims_encoder(name: str) -> bool:
    """Check if FFmpeg binary lists the encoder (compile-time support)."""
    ffmpeg = which_ffmpeg()
    if not ffmpeg:
        return False
    try:
        out = subprocess.check_output(
            [ffmpeg, "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=8,
        )
        return name in out
    except Exception:
        return False


def _test_encoder_works(encoder: str) -> bool:
    """
    Runtime test: can we actually open this encoder?
    Uses a tiny nullsrc so it fails fast when the driver is missing.
    """
    ffmpeg = which_ffmpeg()
    if not ffmpeg:
        return False
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.1",
        "-c:v", encoder, "-f", "null", "-"
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def detect_working_encoders() -> list[tuple[str, str]]:
    """
    Returns a list of (label, internal_id) for encoders that actually work
    on this machine. CPU is always included.
    Order: fastest / preferred first.
    """
    candidates = [
        ("NVIDIA NVENC (h264)", "nvenc", "h264_nvenc"),
        ("Intel QSV (h264)", "qsv", "h264_qsv"),
        ("AMD AMF (h264)", "amf", "h264_amf"),
        ("Apple VideoToolbox (h264)", "videotoolbox", "h264_videotoolbox"),
    ]

    working: list[tuple[str, str]] = []

    for label, key, encoder_name in candidates:
        # First check if FFmpeg even knows about it
        if not _ffmpeg_claims_encoder(encoder_name):
            continue
        # Then do a real open test
        if _test_encoder_works(encoder_name):
            working.append((label, key))

    # CPU is always available
    working.append(("CPU only (libx264)", "cpu"))
    return working


def needs_safe_copy(path: str) -> bool:
    """True if the path is likely to cause problems inside an FFmpeg filter."""
    # Spaces, non-ASCII, or characters that are special in filtergraphs
    if any(ord(c) > 127 for c in path):
        return True
    if " " in path or "'" in path or "[" in path or "]" in path:
        return True
    return False


def make_safe_srt_copy(src: str) -> tuple[str, str | None]:
    """
    If the SRT path is problematic, copy it to a temporary ASCII-only name.
    Returns (path_to_use, temp_dir_or_None).
    Caller must clean up the temp dir when finished.
    """
    if not needs_safe_copy(src):
        return src, None

    tmpdir = tempfile.mkdtemp(prefix="burn_subs_")
    # Keep the .srt extension, use a simple name
    safe = os.path.join(tmpdir, "subs.srt")
    shutil.copy2(src, safe)
    return safe, tmpdir


def escape_for_filter(path: str) -> str:
    """
    Prepare a path for use inside an FFmpeg filter argument.
    - Absolute path
    - Forward slashes
    - Escape characters that are special to the filter parser
    """
    p = os.path.abspath(path).replace("\\", "/")
    # Drive letter colon must be escaped on Windows: C\:/Users/...
    if len(p) >= 2 and p[1] == ":":
        p = p[0] + "\\:" + p[2:]
    # Escape other special characters
    for ch in ("'", "[", "]", ",", ";"):
        p = p.replace(ch, "\\" + ch)
    return p


def build_ffmpeg_cmd(
    video: str,
    srt: str,
    output: str,
    style_key: str,
    quality_key: str,
    hw: str,
) -> list[str]:
    ffmpeg = which_ffmpeg()
    style = STYLES[style_key]
    q = QUALITY_PRESETS[quality_key]
    srt_esc = escape_for_filter(srt)

    # The whole filter value is one argument. Single-quotes protect spaces.
    vf = f"subtitles='{srt_esc}':force_style='{style}'"

    cmd: list[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "info"]

    cmd += ["-i", video]
    cmd += ["-vf", vf]

    if hw == "nvenc":
        cmd += [
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", str(q["cq"]),
            "-b:v", "0",
            "-spatial-aq", "1",
        ]
    elif hw == "qsv":
        cmd += [
            "-c:v", "h264_qsv",
            "-global_quality", str(q["cq"]),
            "-look_ahead", "1",
        ]
    elif hw == "amf":
        cmd += [
            "-c:v", "h264_amf",
            "-quality", "balanced",
            "-rc", "cqp",
            "-qp_i", str(q["amf_q"]),
            "-qp_p", str(q["amf_q"]),
        ]
    elif hw == "videotoolbox":
        bitrate = {18: "12M", 23: "8M", 28: "5M"}.get(q["crf"], "8M")
        cmd += [
            "-c:v", "h264_videotoolbox",
            "-b:v", bitrate,
            "-allow_sw", "1",
        ]
    else:  # cpu
        cmd += [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", str(q["crf"]),
            "-pix_fmt", "yuv420p",
        ]

    # Fast path: copy audio, drop any existing subtitle streams
    cmd += ["-c:a", "copy", "-map", "0", "-sn", output]
    return cmd


# ---------------------------------------------------------------------------
# Worker – uses subprocess + reader thread (reliable on all platforms)
# ---------------------------------------------------------------------------
class FFmpegWorker(threading.Thread):

    def __init__(self, cmd: list[str], events: queue.Queue, temp_dir: str | None = None):
        super().__init__(daemon=True)
        self.cmd = cmd
        self.events = events
        self.temp_dir = temp_dir
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self._duration_s = 0.0

    def cancel(self):
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    def _emit(self, kind: str, value):
        self.events.put((kind, value))

    def run(self):
        duration_re = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
        time_re = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")

        try:
            self._proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # line buffered
            )
        except Exception as e:
            self._cleanup_temp()
            self._emit("error", f"Failed to start FFmpeg:\n{e}")
            return

        # Reader thread so we don't block
        def reader():
            assert self._proc and self._proc.stdout
            for line in self._proc.stdout:
                if self._cancelled:
                    break
                line = line.rstrip()
                if not line:
                    continue
                self._emit("log", line)

                m = duration_re.search(line)
                if m and self._duration_s <= 0:
                    h, mi, s = map(float, m.groups())
                    self._duration_s = h * 3600 + mi * 60 + s

                m = time_re.search(line)
                if m and self._duration_s > 0:
                    h, mi, s = map(float, m.groups())
                    t = h * 3600 + mi * 60 + s
                    pct = min(99.5, (t / self._duration_s) * 100)
                    self._emit("progress", pct)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        # Wait for process
        while self._proc.poll() is None:
            if self._cancelled:
                break
            self.msleep(150)

        t.join(timeout=2)

        if self._cancelled:
            self._cleanup_temp()
            self._emit("error", "Cancelled by user")
            return

        code = self._proc.returncode
        self._cleanup_temp()

        if code == 0:
            self._emit("progress", 100.0)
            self._emit("success", self.cmd[-1])
        else:
            self._emit("error",
                f"FFmpeg exited with code {code}.\n"
                "Check the log above for the exact error."
            )

    def _cleanup_temp(self):
        if self.temp_dir and os.path.isdir(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("HK's SRT Burner")
        self.root.geometry("620x500")
        self.root.minsize(620, 500)

        self.worker: FFmpegWorker | None = None
        self.events = queue.Queue()

        # Detect only encoders that actually work on this machine
        self.working_hw = detect_working_encoders()  # list of (label, key)

        layout = ttk.Frame(self.root, padding=10)
        layout.pack(fill="both", expand=True)

        # --- Files ---
        files = ttk.LabelFrame(layout, text="Files", padding=8)
        files.pack(fill="x", pady=(0, 8))

        self.video_edit = self._file_row(files, "Video:", self.pick_video)
        self.srt_edit = self._file_row(files, "Subtitles:", self.pick_srt)
        self.out_edit = self._file_row(files, "Output:", self.pick_output)

        # --- Options ---
        opts = ttk.LabelFrame(layout, text="Options", padding=8)
        opts.pack(fill="x", pady=(0, 8))

        self.style_combo = ttk.Combobox(opts, values=list(STYLES), state="readonly")
        self.style_combo.current(0)
        self._option_row(opts, "Subtitle style:", self.style_combo)

        self.quality_combo = ttk.Combobox(opts, values=list(QUALITY_PRESETS), state="readonly")
        self.quality_combo.set("Balanced (recommended)")
        self._option_row(opts, "Video quality:", self.quality_combo)

        self.hw_combo = ttk.Combobox(opts, values=[label for label, _ in self.working_hw], state="readonly")
        self.hw_combo.current(0)
        self._option_row(opts, "Hardware encoder:", self.hw_combo)

        # Friendly summary
        names = [label for label, _ in self.working_hw]
        if len(names) == 1:
            hint_txt = "Only software (CPU) encoding is available on this machine."
        else:
            hint_txt = "Only working encoders are shown."
        ttk.Label(opts, text=hint_txt, foreground="#666").grid(row=3, column=1, sticky="w", pady=(4, 0))

        # --- Progress / log ---
        self.progress = ttk.Progressbar(layout, maximum=100)
        self.progress.pack(fill="x", pady=(0, 8))

        self.log = tk.Text(layout, height=10, state="disabled", font=("Consolas", 9), wrap="none")
        self.log.pack(fill="both", expand=True, pady=(0, 8))

        # --- Buttons ---
        btn_row = ttk.Frame(layout)
        btn_row.pack(fill="x")
        self.start_btn = ttk.Button(btn_row, text="Start Burn-in", command=self.start)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.cancel_btn = ttk.Button(btn_row, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", expand=True, fill="x")

        if not which_ffmpeg():
            messagebox.showerror(
                "FFmpeg missing",
                "FFmpeg was not found in PATH.\n"
                "Install it and make sure it is available from the command line.",
            )
            self.start_btn.configure(state="disabled")

        self.root.after(100, self.process_events)

    def _file_row(self, parent, label, command):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=12).pack(side="left")

        entry = ttk.Entry(row)
        entry.pack(side="left", fill="x", expand=True)

        ttk.Button(row, text="Browse…", command=command).pack(side="left", padx=(6, 0))
        return entry

    def _option_row(self, parent, label, widget):
        row = parent.grid_size()[1]
        ttk.Label(parent, text=label, width=18).grid(row=row, column=0, sticky="w", pady=2)
        widget.grid(row=row, column=1, sticky="ew", pady=2)
        parent.columnconfigure(1, weight=1)

    def pick_video(self):
        path = filedialog.askopenfilename(title="Select video", filetypes=[("Video files", "*.mp4 *.mkv *.avi *.mov *.webm *.m4v *.ts *.flv"), ("All files", "*.*")])
        if path:
            self.video_edit.insert(0, path)
            self.video_edit.delete(0, "end")
            self.video_edit.insert(0, path)
            self._suggest_output()

    def pick_srt(self):
        path = filedialog.askopenfilename(title="Select subtitle file", filetypes=[("Subtitle files", "*.srt"), ("All files", "*.*")])
        if path:
            self.srt_edit.delete(0, "end")
            self.srt_edit.insert(0, path)
            self._suggest_output()

    def pick_output(self):
        path = filedialog.asksaveasfilename(title="Save output as", initialfile=self.out_edit.get(), filetypes=[("MP4", "*.mp4"), ("MKV", "*.mkv"), ("All files", "*.*")])
        if path:
            self.out_edit.delete(0, "end")
            self.out_edit.insert(0, path)

    def _suggest_output(self):
        video = self.video_edit.get().strip()
        if not video:
            return
        p = Path(video)
        suggested = str(p.with_name(p.stem + "_subs.mp4"))
        if not self.out_edit.get().strip():
            self.out_edit.insert(0, suggested)

    def log_msg(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self):
        video = self.video_edit.get().strip()
        srt = self.srt_edit.get().strip()
        out = self.out_edit.get().strip()

        if not video or not os.path.isfile(video):
            messagebox.showwarning("Missing video", "Please select a valid video file.")
            return
        if not srt or not os.path.isfile(srt):
            messagebox.showwarning("Missing SRT", "Please select a valid .srt file.")
            return
        if not out:
            messagebox.showwarning("Missing output", "Please choose an output path.")
            return

        Path(out).parent.mkdir(parents=True, exist_ok=True)

        # Make a safe copy of the SRT when the path contains spaces / non-ASCII
        safe_srt, temp_dir = make_safe_srt_copy(srt)
        if temp_dir:
            self.log_msg("Note: SRT path has spaces/special chars -> using temporary copy")

        style = self.style_combo.get()
        quality = self.quality_combo.get()
        hw = dict(self.working_hw).get(self.hw_combo.get(), "cpu")

        cmd = build_ffmpeg_cmd(video, safe_srt, out, style, quality, hw)

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        # Pretty-print the command for the log
        pretty = []
        for c in cmd:
            if " " in c or any(ch in c for ch in "&=;'"):
                pretty.append(f'"{c}"')
            else:
                pretty.append(c)
        self.log_msg("Command:\n" + " ".join(pretty))
        self.log_msg("-" * 60)

        self.progress["value"] = 0
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

        self.worker = FFmpegWorker(cmd, self.events, temp_dir=temp_dir)
        self.worker.start()

    def cancel(self):
        if self.worker:
            self.worker.cancel()
            self.log_msg("Cancelling…")

    def process_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    self.progress["value"] = value
                elif kind == "log":
                    self.log_msg(value)
                elif kind == "success":
                    self.on_ok(value)
                elif kind == "error":
                    self.on_err(value)
        except queue.Empty:
            pass
        self.root.after(100, self.process_events)

    def on_ok(self, path: str):
        self.progress["value"] = 100
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.log_msg(f"\nDone → {path}")
        messagebox.showinfo("Finished", f"Subtitles burned successfully:\n\n{path}")

    def on_err(self, msg: str):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.log_msg(f"\nERROR: {msg}")
        messagebox.showerror("Error", msg)


def main():
    win = MainWindow()
    win.root.mainloop()


if __name__ == "__main__":
    main()
