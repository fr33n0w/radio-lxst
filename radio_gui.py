#!/usr/bin/env python3
# radio-lxst GUI: a small broadcast console in the spirit of the old Winamp source plugins.

import os
import sys
import json
import time
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog

import RNS
import radio_lxst as rl
import bpm
import radio_client as rc
import station_lxmf as sl
from ui_common import (LCD_BOLD, BG, EDGE, LCD_BG, DIM, LCD, LCD_DIM, AMBER, UI_FONT, TITLE_FONT, LCD_FONT, LCD_BIG, hms,
                       apply_style, volume_scale, draw_vu, draw_badge, draw_progress, LogBox, copy_text, context_menu, hash_of, update_banner, about_tab, rns_watch, rns_lost)

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "settings.json")
DEFAULTS = {
    "name": "radio-lxst", "mode": "url", "url": "", "folder": "", "shuffle": False, "input_device": "",
    "frame_ms": 120, "announce_on_start": True, "announce_every_min": 60,
    "identity": os.path.join(HERE, "storage", "identity"), "rnsconfig": "", "auto_start": False,
    "gain_db": 0.0, "limiter_on": True, "limiter_ceiling_db": -2.0, "monitor_device": "",
    "lxmf_on": True, "lxmf_every_h": 3, "slots": 0,
    "fade_on": False, "fade_s": 3, "mix_on": False, "mix_s": 5, "trim_silence": False, "beat_on": False, "beat_n": 32,
    "loop_out": False, "loop_in": False, "loop_n": 8, "loop_times": 4,
    "confirm_play": False, "norm_on": False, "norm_lufs": -14,
}
BEAT_CHOICES = (8, 16, 32, 64, 128)  # beatmix length in beats
# Playlist Sort menu: (label, key, reverse), None = separator
PL_SORTS = (
    ("Title A-Z", "name", False), ("Title Z-A", "name", True), None,
    ("BPM low to high", "bpm", False), ("BPM high to low", "bpm", True), None,
    ("Shortest first", "len", False), ("Longest first", "len", True), None,
    ("Most played", "plays", True), ("Least played", "plays", False), None,
    ("Not played yet first", "fresh", False),
    ("Played longest ago first", "last", False), ("Played most recently first", "last", True), None,
    ("Randomize list", "random", False),
)

def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    for k in ("identity", "folder", "rnsconfig"):  # same rule as radio_lxst --settings
        s[k] = rl.settings_path(s[k], os.path.dirname(SETTINGS))
    return s


class App:
    def __init__(self, root):
        self.root = root
        self.s = load_settings()
        self.logq = queue.Queue()
        self.tick = 0
        self.vu = [0.0, 0.0]
        self.flash = ("", 0)

        root.title(f"RADIO-LXST STATION v{rl.__version__}")
        root.configure(bg=BG)
        root.minsize(720, 590)
        apply_style(root)

        cfg = self.s["rnsconfig"] or None
        RNS.Reticulum(configdir=cfg, loglevel=RNS.LOG_NOTICE)
        RNS.logtimefmt = "%H:%M:%S"
        RNS.logdest = RNS.LOG_CALLBACK
        RNS.logcall = lambda line: self.logq.put(line)
        rl.set_frame_ms(int(self.s["frame_ms"]))
        self.radio = rl.Radio(rl.load_identity(self.s["identity"]), self.s["name"],
                              storage=os.path.dirname(os.path.abspath(self.s["identity"])),
                              lxmf_on=bool(self.s["lxmf_on"]))
        self._apply_announce()

        self._build()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.log(f"radio-lxst {rl.__version__} ready, identity {self.radio.identity_hash}")
        update_banner(root, self.status_row, rl.__version__, self.log)
        rns_watch(root, self.status_row)
        if self.s.get("auto_start"):
            root.after(500, self.go_on_air)
        self._update()

    def _build(self):
        r = self.root
        top = tk.Frame(r, bg=BG)
        top.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(top, text="radio-lxst", bg=BG, fg=LCD, font=TITLE_FONT).pack(side="left")
        tk.Label(top, text="  internet radio over Reticulum", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")

        # LCD display + VU meters
        lcd_wrap = tk.Frame(r, bg=EDGE, padx=2, pady=2)
        lcd_wrap.pack(fill="x", padx=8, pady=6)
        self.lcd = tk.Canvas(lcd_wrap, height=130, bg=LCD_BG, highlightthickness=0)
        self.lcd.pack(fill="x")
        self.lcd.bind("<Configure>", lambda e: self._draw_lcd())

        bar = tk.Frame(r, bg=BG)
        bar.pack(fill="x", padx=8)
        self.b_air = ttk.Button(bar, text="▶  On air", style="Air.TButton", command=self.go_on_air)
        self.b_stop = ttk.Button(bar, text="■  Stop", style="Stop.TButton", command=self.stop)
        self.b_ann = ttk.Button(bar, text="Announce now", command=self.announce_now)
        self.b_copy = ttk.Button(bar, text="Copy call ID", command=self.copy_id)
        for b in (self.b_air, self.b_stop, self.b_ann, self.b_copy):
            b.pack(side="left", padx=(0, 6))
        # inline confirm for Stop, row under the buttons (no popup)
        self.bar = bar
        self.stop_ask = tk.Frame(r, bg=BG)
        tk.Label(self.stop_ask, text="Stop streaming? Listeners will be cut off.", bg=BG, fg=AMBER,
                 font=UI_FONT).pack(side="left")
        ttk.Button(self.stop_ask, text="Stop", command=self._stop_go).pack(side="left", padx=(8, 4))
        ttk.Button(self.stop_ask, text="Cancel", command=self._stop_ask_hide).pack(side="left")
        self._stop_ask_job = None

        # output volume + limiter, live
        mix = tk.Frame(r, bg=BG)
        mix.pack(fill="x", padx=8, pady=(8, 0))
        self.v_gain = tk.DoubleVar(value=float(self.s["gain_db"]))
        self.v_lim = tk.BooleanVar(value=bool(self.s["limiter_on"]))
        self.v_ceil = tk.StringVar(value=str(self.s["limiter_ceiling_db"]))
        tk.Label(mix, text="VOLUME", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        volume_scale(mix, self.v_gain, length=170).pack(side="left", padx=6)
        self.gain_lbl = tk.Label(mix, text="", bg=BG, fg=LCD, font=LCD_FONT, width=8, anchor="w",
                                 cursor="hand2")
        self.gain_lbl.pack(side="left")
        self.gain_lbl.bind("<Double-Button-1>", lambda e: self.v_gain.set(0.0))
        ttk.Checkbutton(mix, text="Limiter", variable=self.v_lim, style="Bar.TCheckbutton").pack(
            side="left", padx=(12, 4))
        tk.Label(mix, text="ceiling", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        ttk.Spinbox(mix, from_=-12, to=0, increment=0.5, width=5, textvariable=self.v_ceil).pack(
            side="left", padx=4)
        tk.Label(mix, text="dB", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        lo, hi = rl.NORM_LUFS
        self.v_norm = tk.BooleanVar(value=bool(self.s["norm_on"]))
        self.v_norm_lufs = tk.StringVar(value=str(max(lo, min(hi, int(self.s["norm_lufs"])))))
        ttk.Checkbutton(mix, text="Normalize", variable=self.v_norm, style="Bar.TCheckbutton").pack(
            side="left", padx=(12, 4))
        ttk.Spinbox(mix, values=list(range(lo, hi + 1)), width=4, textvariable=self.v_norm_lufs,
                    state="readonly").pack(side="left", padx=4)
        tk.Label(mix, text="LUFS", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        for v in (self.v_gain, self.v_lim, self.v_ceil, self.v_norm, self.v_norm_lufs):
            v.trace_add("write", lambda *a: self._mix_changed())
        self._mix_changed()

        nb = ttk.Notebook(r)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        nb.add(self._tab_listeners(nb), text="Listeners")
        nb.add(self._tab_input(nb), text="Input")
        nb.add(self._tab_playlist(nb), text="Playlist")
        nb.add(self._tab_output(nb), text="Output")
        nb.add(self._tab_replies(nb), text="Replies")
        nb.add(self._tab_log(nb), text="Log")
        nb.add(about_tab(nb, "radio-lxst", rl.__version__), text="About")
        nb.enable_traversal()

        # usage in its own label beside status: placed inside it, every status redraw hid it on Windows
        self.status_row = tk.Frame(r, bg=BG)
        self.status_row.pack(fill="x", padx=10, pady=(0, 6))
        self.usage = tk.Label(self.status_row, text="", bg=BG, fg=DIM, font=UI_FONT)
        self.usage.pack(side="right")
        self.status = tk.Label(self.status_row, text="", bg=BG, fg=DIM, font=UI_FONT, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self._procs = {}
        self._usage_tick()
        # click on the CALL ID line of the display copies it
        self.lcd.tag_bind("cid", "<Button-1>", lambda ev: self.copy_id())
        self.lcd.tag_bind("mon", "<Button-1>", lambda ev: self.toggle_monitor())
        self._seek_bar = None
        self.lcd.bind("<Button-1>", self._seek_click, add="+")
        self.lcd.bind("<Motion>", lambda ev: self.lcd.configure(cursor="hand2" if self._on_bar(ev) else ""))

    def _on_bar(self, ev):
        sb = self._seek_bar
        return sb and abs(ev.y - 117) <= 8 and sb[0][0] <= ev.x <= sb[0][1]

    def _seek_click(self, ev):
        if not self._on_bar(ev):
            return
        (bx0, bx1), dur = self._seek_bar
        src = self.radio.source
        if src and getattr(src, "folder", None) and hasattr(src, "seek"):
            src.seek((ev.x - bx0) / max(1, bx1 - bx0) * dur)

    def _usage_tick(self):
        """CPU and RAM of the station and its children (ffmpeg, BPM analysis), every 2 s; needs psutil."""
        try:
            import psutil
            me = self._procs.get("me") or self._procs.setdefault("me", psutil.Process())
            cpu, ram, seen = 0.0, 0, {"me"}
            for p in [me] + me.children(recursive=True):
                q = self._procs.setdefault(p.pid, p)  # same object: cpu_percent counts since the last call
                seen.add(p.pid)
                try:
                    cpu += q.cpu_percent(None)
                    ram += q.memory_info().rss
                except psutil.Error:
                    pass
            for k in set(self._procs) - seen:
                del self._procs[k]
            self.usage.configure(text=f"CPU {cpu / (psutil.cpu_count() or 1):.0f}%   RAM {ram / 2**20:.0f} MB")
        except Exception:
            return  # no psutil: nothing shown
        self.root.after(2000, self._usage_tick)

    def _tab_listeners(self, nb):
        f = ttk.Frame(nb, padding=6)
        cols = ("id", "user", "sub", "profile", "time", "kbps", "lost")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for c, t, w in (("id", "Listener", 255), ("user", "Name", 110), ("sub", "Sub", 36),
                        ("profile", "Quality", 140), ("time", "Time", 70), ("kbps", "kbit/s", 55),
                        ("lost", "Lost", 40)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c == "sub" else
                             ("w" if c in ("id", "user", "profile") else "e"))
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-3>", self._listener_menu)
        self.tree.bind("<Control-c>", lambda ev: self._copy_listener())
        self.lst_info = ttk.Label(f, text="", style="Dim.TLabel")
        self.lst_info.pack(anchor="w", pady=(6, 0))
        return f

    def _tab_input(self, nb):
        f = ttk.Frame(nb, padding=10)
        self.v_mode = tk.StringVar(value=self.s["mode"])
        self.v_url = tk.StringVar(value=self.s["url"])
        self.v_folder = tk.StringVar(value=self.s["folder"])
        self.v_shuffle = tk.BooleanVar(value=self.s["shuffle"])
        self.v_input = tk.StringVar(value=self.s["input_device"])

        ttk.Radiobutton(f, text="Existing stream (Icecast, Shoutcast, Zeno, any http audio)",
                        variable=self.v_mode, value="url").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Entry(f, textvariable=self.v_url, width=60).grid(row=1, column=0, columnspan=3, sticky="we",
                                                               padx=(20, 0), pady=(2, 12))
        ttk.Radiobutton(f, text="Folder playlist, subfolders included (mp3, ogg, opus, flac, wav, m4a)",
                        variable=self.v_mode, value="folder").grid(row=2, column=0, columnspan=3, sticky="w")
        e = ttk.Entry(f, textvariable=self.v_folder, width=50)
        e.grid(row=3, column=0, columnspan=2, sticky="we", padx=(20, 6), pady=2)
        # the playlist reads the folder once it is set, not while typing it
        e.bind("<Return>", lambda ev: self._set_pl_folder())
        e.bind("<FocusOut>", lambda ev: self._set_pl_folder())
        self.pl_folder = self.s["folder"]
        ttk.Button(f, text="Browse...", command=self.browse).grid(row=3, column=2, sticky="w")
        ttk.Checkbutton(f, text="Shuffle every round", variable=self.v_shuffle).grid(row=4, column=0, sticky="w",
                                                                                       padx=(20, 0), pady=4)
        self.b_skip = ttk.Button(f, text="Next track", command=self.skip)
        self.b_skip.grid(row=4, column=1, sticky="w", pady=4)
        opts = ttk.Frame(f)
        opts.grid(row=5, column=0, columnspan=3, sticky="w", padx=(20, 0), pady=(0, 4))
        self.v_fade_on = tk.BooleanVar(value=self.s["fade_on"])
        self.v_fade = tk.StringVar(value=str(self.s["fade_s"]))
        self.v_mix_on = tk.BooleanVar(value=self.s["mix_on"])
        self.v_mix = tk.StringVar(value=str(self.s["mix_s"]))
        self.v_trim = tk.BooleanVar(value=self.s["trim_silence"])
        for text, on, val in (("Fade in/out", self.v_fade_on, self.v_fade), ("Mix tracks", self.v_mix_on, self.v_mix)):
            ttk.Checkbutton(opts, text=text, variable=on).pack(side="left")
            sb = ttk.Spinbox(opts, from_=1, to=30, increment=1, width=3, textvariable=val)
            sb.pack(side="left", padx=(4, 2))
            sb.bind("<FocusOut>", lambda e, v=val, k=("fade_s" if val is self.v_fade else "mix_s"): v.set(str(self.s[k])))
            ttk.Label(opts, text="s", style="Dim.TLabel").pack(side="left", padx=(0, 14))
        ttk.Checkbutton(opts, text="Remove gaps (silence between tracks)", variable=self.v_trim).pack(side="left")
        bm = ttk.Frame(f)
        bm.grid(row=6, column=0, columnspan=3, sticky="w", padx=(20, 0), pady=(0, 4))
        self.v_beat_on = tk.BooleanVar(value=self.s["beat_on"])
        self.v_beat_n = tk.StringVar(value=str(self.s["beat_n"]))
        ttk.Checkbutton(bm, text="Beatmix (experimental)", variable=self.v_beat_on).pack(side="left")
        sb = ttk.Spinbox(bm, values=BEAT_CHOICES, width=4, textvariable=self.v_beat_n, state="readonly")
        sb.pack(side="left", padx=(4, 2))
        ttk.Label(bm, text="beats", style="Dim.TLabel").pack(side="left", padx=(0, 16))
        self.v_loop_out = tk.BooleanVar(value=self.s["loop_out"])
        self.v_loop_in = tk.BooleanVar(value=self.s["loop_in"])
        self.v_loop_n = tk.StringVar(value=str(self.s["loop_n"]))
        self.v_loop_times = tk.StringVar(value=str(self.s["loop_times"]))
        ttk.Checkbutton(bm, text="Loop out", variable=self.v_loop_out).pack(side="left")
        ttk.Checkbutton(bm, text="Loop in", variable=self.v_loop_in).pack(side="left", padx=(8, 0))
        ttk.Spinbox(bm, values=rl.LOOP_CHOICES, width=3, textvariable=self.v_loop_n, state="readonly").pack(
            side="left", padx=(8, 2))
        ttk.Label(bm, text="beats  x", style="Dim.TLabel").pack(side="left")
        ttk.Spinbox(bm, values=rl.LOOP_TIMES, width=3, textvariable=self.v_loop_times, state="readonly").pack(
            side="left", padx=(4, 0))
        ttk.Label(f, text="Beatmix needs a BPM on both tracks (else Mix/Fade); Mix wins over Fade; "
                          "applies from the next track",
                  style="Dim.TLabel").grid(row=7, column=0, columnspan=3, sticky="w", padx=(20, 0))
        ttk.Radiobutton(f, text="Line input (mic, sound card, VB-Cable, SDR app, Loopback = what the PC plays)",
                        variable=self.v_mode, value="line").grid(row=8, column=0, columnspan=3, sticky="w",
                                                                   pady=(12, 0))
        self.cb_input = ttk.Combobox(f, textvariable=self.v_input, width=50, state="readonly")
        self.cb_input.grid(row=9, column=0, columnspan=2, sticky="we", padx=(20, 6), pady=2)
        self.cb_input.bind("<<ComboboxSelected>>", lambda e: self.v_mode.set("line"))
        ttk.Button(f, text="Refresh", command=self.refresh_inputs).grid(row=9, column=2, sticky="w")
        self.in_info = ttk.Label(f, text="", style="Dim.TLabel")
        self.in_info.grid(row=10, column=0, columnspan=3, sticky="w", pady=(14, 0))
        row = ttk.Frame(f)
        row.grid(row=11, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.b_switch = ttk.Button(row, text="Switch now", style="Air.TButton", command=self.switch_source)
        self.b_switch.pack(side="left")
        ttk.Label(row, text="  changes the source on air, listeners stay connected", style="Dim.TLabel").pack(
            side="left")
        f.columnconfigure(1, weight=1)
        self.refresh_inputs()
        for v in (self.v_mode, self.v_url, self.v_folder, self.v_shuffle, self.v_input):
            v.trace_add("write", lambda *a: self.save())
        for v in (self.v_shuffle, self.v_fade_on, self.v_fade, self.v_mix_on, self.v_mix, self.v_trim,
                  self.v_beat_on, self.v_beat_n, self.v_loop_out, self.v_loop_in, self.v_loop_n, self.v_loop_times):
            v.trace_add("write", lambda *a: self._apply_folder_opts())
        return f

    def _tab_playlist(self, nb):
        f = ttk.Frame(nb, padding=6)
        self.pl_cache = {}       # folder -> Playlist
        self.pl_drawn = None     # (playlist, version) on screen
        self.pl_air = None       # (path on air, paths mixing) as drawn
        self.pl_iid = {}         # tree row -> path
        self._drag = None
        head = ttk.Frame(f)
        head.pack(fill="x", pady=(0, 4))
        self.v_confirm = tk.BooleanVar(value=self.s["confirm_play"])
        self.v_confirm.trace_add("write", lambda *a: self.save())
        ttk.Checkbutton(head, text="Ask before changing track on air", variable=self.v_confirm).pack(side="right")
        self.pl_info = ttk.Label(head, text="", style="Dim.TLabel")
        self.pl_info.pack(side="left")
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True)
        self.pl_tree = ttk.Treeview(box, columns=("n", "track", "len", "bpm", "ok", "plays", "last"), show="headings",
                                    height=8, selectmode="extended")
        for c, text, w, anchor in (("n", "#", 50, "e"), ("ok", "Played", 50, "center"), ("plays", "Plays", 45, "e"),
                                   ("last", "Last played", 110, "w"), ("bpm", "BPM", 55, "e"),
                                   ("len", "Length", 60, "e")):
            self.pl_tree.heading(c, text=text)
            self.pl_tree.column(c, width=w, anchor=anchor, stretch=False)
        self.pl_tree.heading("track", text="Track (drag to move)")
        self.pl_tree.column("track", width=420, anchor="w")
        self.pl_tree.tag_configure("air", foreground=LCD, font=LCD_BOLD)
        self.pl_tree.tag_configure("mix", foreground=AMBER, font=LCD_BOLD)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.pl_tree.yview)
        self.pl_tree.configure(yscrollcommand=sb.set)
        arrows = ttk.Frame(box)
        arrows.pack(side="right", fill="y", padx=(4, 0))
        for text, step in (("▲", -1), ("▼", 1)):
            ttk.Button(arrows, text=text, width=3, command=lambda s=step: self._pl_move(s)).pack(fill="x", pady=(0, 4))
        # tracks from anywhere: they stay in their folders, only the list keeps them
        for text, cmd in (("+ Files", self._pl_add_files), ("+ Folder", self._pl_add_folder),
                          ("Remove", self._pl_remove)):
            ttk.Button(arrows, text=text, width=8, command=cmd).pack(fill="x", pady=(8 if text == "+ Files" else 0, 4))
        sb.pack(side="right", fill="y")
        self.pl_tree.pack(side="left", fill="both", expand=True)  # last: a narrow window squeezes the list, not the buttons
        self.pl_tree.bind("<ButtonPress-1>", self._pl_press)
        self.pl_tree.bind("<B1-Motion>", self._pl_motion)
        self.pl_tree.bind("<ButtonRelease-1>", self._pl_release)
        self.pl_tree.bind("<Double-Button-1>", lambda e: self._pl_play_now())
        play = (("Play now", self._pl_play_now), ("Queue next", self._pl_play_next),
                ("Next track now", self._pl_skip), ("Set unplayed", self._pl_unplayed))
        sort = tuple(s and (s[0], lambda k=s[1], r=s[2]: self._pl_sort(k, r)) for s in PL_SORTS)
        tools = (("Shuffle", lambda: self._pl_do("shuffle_now")), ("Sort", sort),
                 ("Detect BPM", self._pl_detect_bpm), ("Rescan", lambda: self._pl_do("rescan")))
        for buttons in (play, tools):
            row = ttk.Frame(f)
            row.pack(fill="x", pady=(6, 0))
            for i, (text, cmd) in enumerate(buttons):  # same widths in both rows: columns line up
                row.columnconfigure(i, weight=1, uniform="pl")
                if callable(cmd):
                    w = ttk.Button(row, text=text, command=cmd)
                else:  # a tuple of choices = drop-down
                    w = ttk.Menubutton(row, text=text)
                    menu = tk.Menu(w, tearoff=0)
                    for item in cmd:
                        if item is None:
                            menu.add_separator()
                        else:
                            menu.add_command(label=item[0], command=item[1])
                    w["menu"] = menu
                w.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 4, 0))
        self.pl_hint = ttk.Label(f, text="Double click = play now, Queue next = after the track on air. The order is saved.\n"
                                         "Played = since the station started, Last played is kept.",
                                 style="Dim.TLabel")
        self.pl_hint.pack(anchor="w", pady=(6, 0))
        # inline question / notice for Play now (no pop-up windows)
        self.pl_ask = ttk.Frame(f)
        btns = ttk.Frame(self.pl_ask)
        btns.pack(side="right")  # first: a long text cannot squeeze the buttons
        self.pl_ask_no = ttk.Button(btns, text="Cancel", command=self._pl_ask_hide)
        self.pl_ask_no.pack(side="right")
        self.pl_ask_yes = ttk.Button(btns, text="Play")
        self.pl_ask_yes.pack(side="right", padx=(0, 4))
        # width=1: the text never asks for room, it wraps in what the bar has (else wrap and width chase each other)
        self.pl_ask_text = ttk.Label(self.pl_ask, text="", width=1)
        self.pl_ask_text.pack(side="left", fill="x", expand=True)

        def wrap(e):
            w = max(100, e.width - btns.winfo_reqwidth() - 8)
            if str(self.pl_ask_text.cget("wraplength")) != str(w):
                self.pl_ask_text.configure(wraplength=w)
        self.pl_ask.bind("<Configure>", wrap)
        self._pl_ask_job = None
        return f

    # playlist
    def _playlist_for(self, folder):
        folder = os.path.abspath(folder)
        pl = self.pl_cache.get(folder)
        if pl is None:
            pl = self.pl_cache[folder] = rl.Playlist(folder, os.path.join(self.radio.storage, rl.PLAYLIST_FILE),
                                                     self.v_shuffle.get(), scan=False)
            threading.Thread(target=pl.rescan, daemon=True).start()  # a big folder must not freeze the GUI
        pl.shuffle = self.v_shuffle.get()
        return pl

    def _playlist(self):
        """The playlist on air, else the one of the folder set in Input."""
        src = self.radio.source
        if self.radio.on_air and src is not None and getattr(src, "playlist", None):
            return src.playlist
        folder = (self.pl_folder or "").strip()
        return self._playlist_for(folder) if folder and os.path.isdir(folder) else None

    def _set_pl_folder(self):
        self.pl_folder = self.v_folder.get().strip()

    def _pl_paths(self):
        return [self.pl_iid[i] for i in self.pl_tree.selection() if i in self.pl_iid]

    def _pl_refresh(self):
        if self._drag is not None:
            return
        pl = self._playlist()
        src = self.radio.shown_source
        air = getattr(src, "now_path", None) if self.radio.on_air else None
        t = self.pl_tree
        if pl is None:
            if self.pl_drawn is not None:
                t.delete(*t.get_children())
                self.pl_drawn, self.pl_iid = None, {}
            self.pl_info.configure(text="Choose a folder in Input to see its tracks here")
            return
        bv = self._bpm().version
        if self.pl_drawn != (pl, pl.version, bv):
            keep = set(self._pl_paths())
            t.delete(*t.get_children())
            self.pl_iid = {}
            sel = []
            for i, p in enumerate(pl.items()):
                iid = f"t{i}"
                self.pl_iid[iid] = p
                t.insert("", "end", iid=iid, values=self._pl_row(pl, p, i + 1))
                if p in keep:
                    sel.append(iid)
            if sel:
                t.selection_set(sel)
            self.pl_drawn, self.pl_air = (pl, pl.version, bv), None
            if pl.scanned:
                self._bpm().want(pl.items())
            on = "on air" if src is not None and getattr(src, "playlist", None) is pl else "not on air"
            n = f"{len(self.pl_iid)} tracks" if pl.scanned else "scanning..."
            left = self._bpm().pending()
            self.pl_info.configure(text=f"{n} in {pl.folder}  ({on})" + (f"   BPM: {left} to analyse" if left else ""))
        mixed = set(src.mixing_paths()) if air and hasattr(src, "mixing_paths") else set()
        state = (air, frozenset(mixed))
        if state != self.pl_air:
            old_air, old_mixed = self.pl_air or (None, frozenset())
            touch = {old_air, air} | old_mixed | mixed
            for i, (iid, p) in enumerate(self.pl_iid.items()):
                if p in touch:
                    tag = ("mix",) if p in mixed else ("air",) if p == air else ()
                    t.item(iid, values=self._pl_row(pl, p, "▶" if p == air else i + 1), tags=tag)
                    if p == air and p != old_air:
                        t.see(iid)
            self.pl_air = state

    def _pl_row(self, pl, path, n):
        now, last = pl.played(path)
        if last:
            fmt = "%H:%M" if time.strftime("%Y%m%d", time.localtime(last)) == time.strftime("%Y%m%d") else "%d/%m/%y %H:%M"
            last = time.strftime(fmt, time.localtime(last))
        dur = (self._bpm().get(path) or {}).get("dur")
        length = (f"{int(dur // 60)}:{int(dur % 60):02d}" if dur < 3600 else hms(dur)) if dur else ""
        return (n, pl.rel(path), length, self._pl_bpm(path), "✓" if now else "", pl.plays.get(path) or "", last or "")

    def _pl_sort(self, key, reverse):
        pl = self._playlist()
        if not pl:
            return
        if key == "random":
            return pl.shuffle_now()
        cache = self._bpm()
        keys = {
            "name": None,
            "bpm": lambda p: (cache.get(p) or {}).get("bpm"),
            "len": lambda p: (cache.get(p) or {}).get("dur") or None,
            "plays": lambda p: pl.plays.get(p, 0),
            "fresh": lambda p: int(pl.played(p)[0]),
            "last": lambda p: pl.last_played.get(p, 0),
        }
        pl.sort_now(key=keys[key], reverse=reverse)

    def _bpm(self):
        if getattr(self, "bpm", None) is None:
            self.bpm = bpm.BpmCache(os.path.join(self.radio.storage, bpm.BPM_FILE))
        return self.bpm

    def _pl_bpm(self, path):
        r = self._bpm().get(path)
        if r is None or (self._bpm().waiting(path) and "dur" in r):  # no dur yet = only the length is missing
            return "..."
        return f"{r['bpm']:.1f}" if r.get("bpm") else "?"

    def _pl_detect_bpm(self):
        paths = self._pl_paths()
        if paths:
            self._bpm().want(paths, force=True)
            self.log(f"BPM: analysing {len(paths)} track(s) again")

    def _pl_unplayed(self):
        pl, paths = self._playlist(), self._pl_paths()
        if pl and paths:
            pl.set_unplayed(paths)

    def _pl_press(self, ev):
        self._drag = self.pl_tree.identify_row(ev.y) or None
        self._dragged = False

    def _pl_motion(self, ev):
        if not self._drag:
            return
        target = self.pl_tree.identify_row(ev.y)
        if target and target != self._drag:
            self.pl_tree.move(self._drag, "", self.pl_tree.index(target))
            self._dragged = True

    def _pl_release(self, ev):
        drag, self._drag = self._drag, None
        pl = self.pl_drawn[0] if self.pl_drawn else None
        if drag and self._dragged and pl:
            pl.set_order([self.pl_iid[i] for i in self.pl_tree.get_children() if i in self.pl_iid])
            self.pl_tree.selection_set(drag)

    def _pl_do(self, what):
        pl = self._playlist()
        if pl and what == "rescan":
            threading.Thread(target=pl.rescan, daemon=True).start()
        elif pl:
            getattr(pl, what)()

    def _pl_add(self, paths):
        pl = self._playlist()
        if not pl:
            self._pl_ask_show("Choose a folder in Input first: added tracks go into its playlist.")
            return
        if paths:
            self.log(f"Playlist: added {', '.join(paths)}")
            threading.Thread(target=pl.add, args=(list(paths),), daemon=True).start()  # a big folder is scanned

    def _pl_add_files(self):
        exts = " ".join("*" + e for e in rl.AUDIO_EXTS)
        self._pl_add(filedialog.askopenfilenames(title="Add tracks to the playlist",
                                                 filetypes=[("Audio", exts), ("All files", "*")]))

    def _pl_add_folder(self):
        d = filedialog.askdirectory(title="Add a folder to the playlist")
        self._pl_add([d] if d else [])

    def _pl_remove(self):
        pl, paths = self._playlist(), self._pl_paths()
        if pl and paths:
            pl.remove(paths)  # only from the list, the files stay on disk
            self.log(f"Playlist: removed {pl.rel(paths[0])}" + (f" (+{len(paths) - 1})" if len(paths) > 1 else ""))

    def _pl_move(self, delta):
        pl, paths = self._playlist(), self._pl_paths()
        if pl and paths:
            pl.move(paths, delta)

    def _pl_play_next(self):
        pl, paths = self._playlist(), self._pl_paths()
        if pl and paths:
            pl.play_next(paths)
            self.log(f"Next: {pl.rel(paths[0])}" + (f" (+{len(paths) - 1})" if len(paths) > 1 else ""))

    def _pl_play_now(self):
        pl, paths = self._playlist(), self._pl_paths()
        if not (pl and paths):
            return
        if self._pl_other_on_air():
            return
        go = lambda: self._pl_start(pl, paths[0])
        if not self.radio.on_air:  # always asked: going on air by mistake is worse
            self._pl_ask_show(f"The station is off air. Start streaming with {pl.rel(paths[0])} ?", go, "Go on air")
        elif self.v_confirm.get():
            self._pl_ask_show(f"Play now on air: {pl.rel(paths[0])} ?", go)
        else:
            self._pl_start(pl, paths[0])

    def _pl_skip(self):
        if not (self.radio.on_air and getattr(self.radio.source, "folder", None)):
            return
        if self.v_confirm.get():
            self._pl_ask_show("Skip to the next track on air ?", self._pl_skip_go)
        else:
            self.skip()

    def _pl_skip_go(self):
        self._pl_ask_hide()
        self.skip()

    def _pl_other_on_air(self):
        """On air with a stream or line input: Play now does nothing, just says so."""
        if self.radio.on_air and not getattr(self.radio.source, "folder", None):
            self._pl_ask_show("On air with a stream or line input: use On air in Input to switch to the folder")
            return True
        return False

    def _pl_start(self, pl, path):
        self._pl_ask_hide()
        if self._pl_other_on_air():
            return
        pl.play_now(path)
        self.log(f"Playing now: {pl.rel(path)}")
        src = self.radio.source
        if self.radio.on_air and getattr(src, "playlist", None) is pl:
            src.skip()
        elif not self.radio.on_air:
            self.v_folder.set(pl.folder)
            self.v_mode.set("folder")
            self.go_on_air()

    def _pl_ask_show(self, text, on_yes=None, yes="Play"):
        self.pl_ask_text.configure(text=text)
        if on_yes:
            self.pl_ask_yes.configure(command=on_yes, text=yes)
            self.pl_ask_yes.pack(side="right", padx=(0, 4))
            self.pl_ask_no.configure(text="Cancel")
        else:
            self.pl_ask_yes.pack_forget()
            self.pl_ask_no.configure(text="OK")
        self.pl_ask.pack(fill="x", pady=(6, 0), before=self.pl_hint)
        if self._pl_ask_job:
            self.root.after_cancel(self._pl_ask_job)
        self._pl_ask_job = self.root.after(15000, self._pl_ask_hide)

    def _pl_ask_hide(self):
        if self._pl_ask_job:
            self.root.after_cancel(self._pl_ask_job)
            self._pl_ask_job = None
        self.pl_ask.pack_forget()

    def _tab_output(self, nb):
        f = ttk.Frame(nb, padding=10)
        self.v_name = tk.StringVar(value=self.s["name"])
        self.v_frame = tk.IntVar(value=int(self.s["frame_ms"]))
        self.v_ann_start = tk.BooleanVar(value=self.s["announce_on_start"])
        self.v_ann_every = tk.StringVar(value=str(self.s["announce_every_min"]))

        ttk.Label(f, text="Station name").grid(row=0, column=0, sticky="w")
        row = ttk.Frame(f)
        row.grid(row=0, column=1, columnspan=2, sticky="w", pady=2)
        ttk.Entry(row, textvariable=self.v_name, width=36).pack(side="left")
        self.v_slots = tk.StringVar(value=str(self.s["slots"]))
        ttk.Label(row, text="Listener slots").pack(side="left", padx=(16, 4))
        sb = ttk.Spinbox(row, from_=0, to=99, increment=1, width=4, textvariable=self.v_slots)
        sb.pack(side="left")
        sb.bind("<FocusOut>", lambda e: self.v_slots.set(str(self.radio.slots)))
        ttk.Label(row, text="0 = no limit; when full new callers get BUSY", style="Dim.TLabel").pack(
            side="left", padx=4)

        ttk.Label(f, text="Quality").grid(row=1, column=0, sticky="nw", pady=(10, 0))
        ttk.Radiobutton(f, text="Steady: 120 ms packets, up to 28 kbit/s (best on slow paths)",
                        variable=self.v_frame, value=120).grid(row=1, column=1, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Radiobutton(f, text="High: 60 ms packets, up to 56 kbit/s (needs a smooth path)",
                        variable=self.v_frame, value=60).grid(row=2, column=1, columnspan=2, sticky="w")

        ttk.Label(f, text="Announce").grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Checkbutton(f, text="when going on air", variable=self.v_ann_start).grid(
            row=3, column=1, sticky="w", pady=(10, 0))
        self.v_auto = tk.BooleanVar(value=self.s.get("auto_start", False))
        ttk.Checkbutton(f, text="Go on air when the program starts", variable=self.v_auto).grid(
            row=3, column=2, sticky="w", pady=(10, 0))
        self.v_auto.trace_add("write", lambda *a: self._output_changed())
        row = ttk.Frame(f)
        row.grid(row=4, column=1, columnspan=2, sticky="w")
        ttk.Label(row, text="every").pack(side="left")
        sb = ttk.Spinbox(row, values=(0, 20, 30, 45, 60, 90, 120, 180, 240, 360, 720), width=5,
                         textvariable=self.v_ann_every)
        sb.pack(side="left", padx=4)
        sb.bind("<FocusOut>", lambda e: self.v_ann_every.set(str(self.radio.announce_every_min)))
        ttk.Label(row, text=f"minutes (0 = off, min {rl.ANNOUNCE_MIN_EVERY}; "
                            f"at most one announce every {rl.ANNOUNCE_MIN_GAP // 60} min)").pack(side="left")
        self.ann_info = ttk.Label(f, text="", style="Dim.TLabel")
        self.ann_info.grid(row=5, column=1, columnspan=2, sticky="w")

        ttk.Label(f, text="Addresses").grid(row=6, column=0, sticky="nw", pady=(14, 0))
        for i, (lbl, val) in enumerate((("Call ID (dial this)", self.radio.identity_hash),
                                         ("LXST destination", self.radio.dest.hash.hex()),
                                         ("LXMF address", self.radio.lxmf_hash))):
            ttk.Label(f, text=lbl, style="Dim.TLabel").grid(row=6 + i, column=1, sticky="w",
                                                            pady=(14 if i == 0 else 2, 0))
            e = ttk.Entry(f, width=36)
            e.insert(0, val)
            e.state(["readonly"])
            e.configure(cursor="hand2")
            e.bind("<Button-1>", lambda ev, l=lbl, v=val: self.copied(l.split(" (")[0], v))
            e.grid(row=6 + i, column=2, sticky="w", pady=(14 if i == 0 else 2, 0))
        ttk.Label(f, text="Quality changes apply after Stop and On air.", style="Dim.TLabel").grid(
            row=9, column=1, columnspan=2, sticky="w", pady=(12, 0))

        ttk.Label(f, text="Monitor").grid(row=10, column=0, sticky="w", pady=(14, 0))
        self.v_mon_dev = tk.StringVar(value=self.s["monitor_device"] or "Default")
        self.cb_mon = ttk.Combobox(f, textvariable=self.v_mon_dev, width=34, state="readonly",
                                   values=["Default"] + rc.list_devices())
        self.cb_mon.grid(row=10, column=1, columnspan=2, sticky="w", pady=(14, 0))
        self.cb_mon.bind("<<ComboboxSelected>>", lambda e: self._monitor_device_changed())
        ttk.Label(f, text="Headphones to hear what goes on air: MON on the display.", style="Dim.TLabel").grid(
            row=11, column=1, columnspan=2, sticky="w")

        ttk.Label(f, text="LXMF").grid(row=12, column=0, sticky="w", pady=(14, 0))
        self.v_lxmf = tk.BooleanVar(value=bool(self.s["lxmf_on"]))
        self.v_lxmf_every = tk.StringVar(value=str(self.s["lxmf_every_h"]))
        ttk.Checkbutton(f, text="Autoresponder + on-air message to subscribers", variable=self.v_lxmf).grid(
            row=12, column=1, columnspan=2, sticky="w", pady=(14, 0))
        row = ttk.Frame(f)
        row.grid(row=13, column=1, columnspan=2, sticky="w")
        ttk.Label(row, text="announce the LXMF address every").pack(side="left")
        sb = ttk.Spinbox(row, values=(1, 2, 3, 4, 6, 8, 12, 24), width=4, textvariable=self.v_lxmf_every)
        sb.pack(side="left", padx=4)
        sb.bind("<FocusOut>", lambda e: self.v_lxmf_every.set(str(self.radio.lxmf_every_h)))
        ttk.Label(row, text=f"hours (min {rl.LXMF_MIN_EVERY_H}); off = no LXMF address, one announce less").pack(
            side="left")
        for v in (self.v_name, self.v_frame, self.v_ann_start, self.v_ann_every, self.v_lxmf, self.v_lxmf_every,
                  self.v_slots):
            v.trace_add("write", lambda *a: self._output_changed())
        return f

    def _tab_replies(self, nb):
        # LXMF autoresponder texts: the commands are fixed, what they answer is ours (link stays fixed)
        f = ttk.Frame(nb, padding=6)
        self.rep_text = dict(sl.DEFAULT_REPLIES, **sl.load_replies(self.radio.storage))
        self.rep_saved = dict(self.rep_text)
        self.rep_cur = None
        left = ttk.Frame(f)
        left.pack(side="left", fill="y")
        self.rep_list = tk.Listbox(left, width=33, height=11, bg=LCD_BG, fg=LCD, selectbackground=EDGE,
                                   font=LCD_FONT, relief="flat", activestyle="none", exportselection=False)
        self.rep_list.pack(fill="y", expand=True)
        self.rep_list.bind("<<ListboxSelect>>", lambda ev: self._rep_pick())
        right = ttk.Frame(f)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.rep_edit = tk.Text(right, bg=LCD_BG, fg=LCD, insertbackground=LCD, font=LCD_FONT, relief="flat",
                                wrap="word", height=7, undo=True)
        self.rep_edit.pack(fill="both", expand=True)
        self.rep_edit.bind("<<Modified>>", self._rep_typed)
        tk.Label(right, text="Placeholders: " + " ".join("{" + p + "}" for p in sl.PLACEHOLDERS),
                 bg=BG, fg=DIM, font=UI_FONT, anchor="w", justify="left", wraplength=520).pack(fill="x", pady=(4, 0))
        row = ttk.Frame(right)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Save", command=self._rep_save).pack(side="left")
        ttk.Button(row, text="Default", command=self._rep_default).pack(side="left", padx=6)
        self.rep_state = tk.Label(row, text="", bg=BG, fg=DIM, font=UI_FONT, anchor="w")
        self.rep_state.pack(side="left", padx=6)
        self._rep_fill()
        self.rep_list.selection_set(0)
        self._rep_pick()
        return f

    def _rep_fill(self):
        sel = self.rep_list.curselection()
        self.rep_list.delete(0, "end")
        for key, label in sl.REPLY_LABELS:
            mark = "*" if self.rep_text[key] != self.rep_saved[key] else (
                "+" if self.rep_saved[key] != sl.DEFAULT_REPLIES[key] else " ")
            self.rep_list.insert("end", f"{mark} {label}")
        for i in sel:
            self.rep_list.selection_set(i)

    def _rep_pick(self):
        sel = self.rep_list.curselection()
        if not sel:
            return
        self.rep_cur = sl.REPLY_LABELS[sel[0]][0]
        self.rep_edit.delete("1.0", "end")
        self.rep_edit.insert("1.0", self.rep_text[self.rep_cur])
        self.rep_edit.edit_modified(False)
        self._rep_status()

    def _rep_typed(self, ev=None):
        if not self.rep_edit.edit_modified() or not self.rep_cur:
            return
        self.rep_edit.edit_modified(False)
        self.rep_text[self.rep_cur] = self.rep_edit.get("1.0", "end-1c")
        self._rep_fill()
        self._rep_status()

    def _rep_status(self):
        unsaved = sum(self.rep_text[k] != self.rep_saved[k] for k, _ in sl.REPLY_LABELS)
        custom = sum(self.rep_saved[k] != sl.DEFAULT_REPLIES[k] for k, _ in sl.REPLY_LABELS)
        self.rep_state.configure(text=(f"{unsaved} not saved   " if unsaved else "") +
                                 f"{custom} custom   (+ custom, * changed; link is fixed)",
                                 fg=AMBER if unsaved else DIM)

    def _rep_default(self):
        if self.rep_cur:
            self.rep_edit.delete("1.0", "end")
            self.rep_edit.insert("1.0", sl.DEFAULT_REPLIES[self.rep_cur])

    def _rep_save(self):
        keys = [k for k, _ in sl.REPLY_LABELS]
        try:
            kept = sl.save_replies(self.radio.storage, {k: self.rep_text[k] for k in keys})
        except OSError as e:
            return self.log(f"Replies not saved: {e}")
        for k in keys:
            if not self.rep_text[k].strip():
                self.rep_text[k] = sl.DEFAULT_REPLIES[k]  # empty = default
        self.rep_saved = dict(self.rep_text)
        if self.radio.lx:
            self.radio.lx.reload_replies()
        self._rep_pick()
        self._rep_fill()
        self.log(f"Replies saved ({len(kept)} custom)")

    def _tab_log(self, nb):
        f = ttk.Frame(nb, padding=4)
        self.logbox = LogBox(f)
        return f

    # actions
    def _make_source(self):
        if self.v_mode.get() == "url":
            url = self.v_url.get().strip()
            if not url:
                self.log("Set a stream URL in Input first")
                return None
            return rl.Source(url=url)
        if self.v_mode.get() == "line":
            dev = self.v_input.get().strip()
            if not dev:
                self.log("Choose an input device in Input first")
                return None
            return rl.LineSource(dev)
        folder = self.v_folder.get().strip()
        if not os.path.isdir(folder):
            self.log("Choose an existing folder in Input first")
            return None
        self._set_pl_folder()
        return rl.Source(folder=folder, playlist=self._playlist_for(folder), bpm_of=self._bpm().get,
                         mix_log=os.path.join(self.radio.storage, rl.MIX_LOG_FILE),
                         norm=lambda: self.radio.norm_lufs, **self._folder_opts())

    def _folder_opts(self):
        return {"shuffle": self.v_shuffle.get(), "fade": self.s["fade_s"] if self.v_fade_on.get() else 0,
                "mix": self.s["mix_s"] if self.v_mix_on.get() else 0, "trim": self.v_trim.get(),
                "beats": self.s["beat_n"] if self.v_beat_on.get() else 0,
                "loop_out": self.s["loop_out"], "loop_in": self.s["loop_in"], "loop_n": self.s["loop_n"],
                "loop_times": self.s["loop_times"]}

    def _apply_folder_opts(self):
        self.save()
        src = self.radio.source
        if src is not None and getattr(src, "folder", None):
            for k, v in self._folder_opts().items():
                setattr(src, k, v)

    def go_on_air(self):
        self.save()
        if self.radio.on_air:
            # already on air: change the source live, listeners stay connected
            return self.switch_source()
        rl.set_frame_ms(int(self.v_frame.get()))
        src = self._make_source()
        if src:
            self.radio.name = self.v_name.get().strip() or "radio-lxst"
            self.radio.start(src)

    def switch_source(self):
        self.save()
        src = self._make_source()
        if src:
            o = self._folder_opts()
            self.radio.switch_source(src, o["mix"] or o["fade"])  # crossfade like between tracks

    def stop(self):
        if not self.radio.on_air:
            return
        self.stop_ask.pack(fill="x", padx=8, pady=(4, 0), after=self.bar)
        if self._stop_ask_job:
            self.root.after_cancel(self._stop_ask_job)
        self._stop_ask_job = self.root.after(15000, self._stop_ask_hide)

    def _stop_go(self):
        self._stop_ask_hide()
        if self.radio.on_air:
            self.radio.stop()

    def _stop_ask_hide(self):
        if self._stop_ask_job:
            self.root.after_cancel(self._stop_ask_job)
            self._stop_ask_job = None
        self.stop_ask.pack_forget()

    def announce_now(self):
        self.radio.name = self.v_name.get().strip() or "radio-lxst"
        if not self.radio.announce():
            self.log(f"Announce not sent: one every {rl.ANNOUNCE_MIN_GAP // 60} min at most, "
                     f"next in {hms(self.radio.announce_wait())}")

    def copy_id(self):
        self.copied("Call ID", self.radio.identity_hash)

    def copied(self, what, text):
        copy_text(self.root, text)
        self.flash = (f"Copied {what}: {text}", time.time() + 3)
        self.status.configure(text=self.flash[0], fg=LCD)

    def _listener_menu(self, ev):
        row = self.tree.identify_row(ev.y)
        if not row:
            return
        self.tree.selection_set(row)
        lid = self.tree.item(row, "values")[0]
        if len(lid) != 32:
            return
        context_menu(self.root, ev, [
            ("Copy listener ID", lambda: self.copied("listener ID", lid)),
            ("Copy listener LXMF address", lambda: self.copied("listener LXMF address", hash_of("lxmf.delivery", lid))),
        ])

    def _copy_listener(self):
        sel = self.tree.selection()
        if sel:
            lid = self.tree.item(sel[0], "values")[0]
            if len(lid) == 32:
                self.copied("listener ID", lid)

    def skip(self):
        if self.radio.source and self.radio.source.folder:
            self.radio.source.skip()

    def refresh_inputs(self):
        devs = rl.list_inputs()
        cur = self.v_input.get()
        if cur and cur not in devs:
            devs.append(cur)  # keep the saved one even if unplugged now
        self.cb_input["values"] = devs

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.v_folder.get() or HERE)
        if d:
            self.v_folder.set(os.path.normpath(d))
            self.v_mode.set("folder")
            self._set_pl_folder()

    # local monitor
    def _monitor_device(self):
        d = self.v_mon_dev.get()
        return "" if d == "Default" else d

    def toggle_monitor(self):
        if self.radio.monitor:
            self.radio.monitor.stop()
            self.radio.monitor = None
            self.log("Monitor off")
            return
        dev = self._monitor_device()
        src = self.radio.source
        if isinstance(src, rl.LineSource) and src.device.startswith("Loopback: "):
            if src.device[len("Loopback: "):] == (dev or rl.default_output_name()):
                self.log("Monitor not started: it would play into the same device the input records (feedback)")
                return
        mon = rc.Player(prefill=0.3, device=dev or None, max_extra=0.5)
        mon.start()
        self.radio.monitor = mon
        self.log(f"Monitor on: {dev or 'default output'}")

    def _monitor_device_changed(self):
        self.save()
        if self.radio.monitor:  # restart on the new device
            self.toggle_monitor()
            self.toggle_monitor()

    def _apply_announce(self):
        self.radio.announce_on_start = bool(self.s["announce_on_start"])
        try:
            self.radio.announce_every_min = int(self.s["announce_every_min"])
        except (TypeError, ValueError):
            pass
        self.s["announce_every_min"] = self.radio.announce_every_min
        try:
            self.radio.lxmf_every_h = int(self.s["lxmf_every_h"])
        except (TypeError, ValueError):
            pass
        self.s["lxmf_every_h"] = self.radio.lxmf_every_h
        try:
            self.radio.slots = max(0, min(99, int(self.s["slots"])))
        except (TypeError, ValueError):
            pass
        self.s["slots"] = self.radio.slots
        if bool(self.s["lxmf_on"]) != self.radio.lxmf_on:
            self.radio.lxmf_on = bool(self.s["lxmf_on"])

    def _mix_changed(self):
        g = round(float(self.v_gain.get()) * 2) / 2
        self.radio.gain_db = g
        self.gain_lbl.configure(text=f"{g:+.1f} dB")
        self.radio.limiter_on = bool(self.v_lim.get())
        try:
            self.radio.limiter.ceiling_db = max(-12.0, min(0.0, float(self.v_ceil.get())))
        except ValueError:
            pass
        self.radio.norm_lufs = int(self.v_norm_lufs.get()) if self.v_norm.get() else None
        self.save()

    def _output_changed(self):
        self.save()
        self._apply_announce()
        if not self.radio.on_air:
            self.radio.name = self.s["name"]

    def save(self):
        try:
            every = int(self.v_ann_every.get())
        except (ValueError, AttributeError):
            every = self.s["announce_every_min"]
        every = max(rl.ANNOUNCE_MIN_EVERY, every) if every > 0 else 0
        try:
            lx_every = max(rl.LXMF_MIN_EVERY_H, int(self.v_lxmf_every.get()))
        except (ValueError, AttributeError):
            lx_every = self.s["lxmf_every_h"]
        try:
            slots = max(0, min(99, int(self.v_slots.get())))
        except (ValueError, AttributeError):
            slots = self.s["slots"]
        secs = {}
        for k, var in (("fade_s", "v_fade"), ("mix_s", "v_mix")):
            try:
                secs[k] = max(1, min(30, int(float(getattr(self, var).get()))))
            except (ValueError, AttributeError):
                secs[k] = self.s[k]
        try:
            self.s.update(secs)
            self.s.update({"fade_on": self.v_fade_on.get(), "mix_on": self.v_mix_on.get(),
                           "trim_silence": self.v_trim.get(), "beat_on": self.v_beat_on.get()})
            n = self.v_beat_n.get()
            self.s["beat_n"] = int(n) if n.isdigit() and int(n) in BEAT_CHOICES else self.s["beat_n"]
            self.s.update(loop_out=self.v_loop_out.get(), loop_in=self.v_loop_in.get())
            for k, var, ok in (("loop_n", self.v_loop_n, rl.LOOP_CHOICES), ("loop_times", self.v_loop_times, rl.LOOP_TIMES)):
                v = var.get()
                self.s[k] = int(v) if v.isdigit() and int(v) in ok else self.s[k]
            self.s["confirm_play"] = self.v_confirm.get()
        except AttributeError:
            pass
        try:
            self.s.update({
                "name": self.v_name.get(), "mode": self.v_mode.get(), "url": self.v_url.get(),
                "folder": self.v_folder.get(), "shuffle": self.v_shuffle.get(),
                "input_device": self.v_input.get(), "monitor_device": self._monitor_device(),
                "frame_ms": int(self.v_frame.get()), "announce_on_start": self.v_ann_start.get(),
                "announce_every_min": every, "auto_start": self.v_auto.get(),
                "gain_db": self.radio.gain_db, "limiter_on": self.radio.limiter_on,
                "limiter_ceiling_db": self.radio.limiter.ceiling_db,
                "norm_on": self.v_norm.get(), "norm_lufs": int(self.v_norm_lufs.get()),
                "lxmf_on": self.v_lxmf.get(), "lxmf_every_h": lx_every, "slots": slots,
            })
        except AttributeError:
            return  # widgets not built yet
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(self.s, f, indent=2)
        except OSError as e:
            self.log(f"Could not save settings: {e}")

    def log(self, msg):
        self.logq.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def quit(self):
        self.save()
        try:
            if self.radio.monitor:
                self.radio.monitor.stop()
            self.radio.stop() if self.radio.on_air else None
        except Exception:
            pass
        time.sleep(0.3)  # let audio threads leave WASAPI before exit
        self.root.destroy()
        os._exit(0)

    # refresh
    def _update(self):
        self.tick += 1
        snap = self.radio.snapshot()
        self.snap = snap
        for i in (0, 1):
            lv = snap["levels"][i]
            self.vu[i] = lv if lv > self.vu[i] else self.vu[i] * 0.8
        self._draw_lcd()

        if self.tick % 4 == 0:
            self.tree.delete(*self.tree.get_children())
            for row in snap["rows"]:
                sub = "✓" if row["sub"] else ""
                if row["state"] == "listening":
                    vals = (row["id"], row["user"], sub, row["profile"], hms(row["time"]),
                            f"{row['kbps']:.1f}", row["lost"])
                else:
                    vals = (row["id"], row["user"], sub, row["state"], "", "", "")
                self.tree.insert("", "end", values=vals)
            self.lst_info.configure(text=f"Peak {snap['peak']}   Calls {snap['calls']}   "
                                         f"Sent {RNS.prettysize(snap['bytes'])}")
            self.in_info.configure(text=f"Source: {snap['source_state']}   {snap['now_playing']}")
            na = snap["next_announce"]
            self.ann_info.configure(text=f"next announce in {hms(max(0, na))}" if na is not None else "")
            if time.time() > self.flash[1]:
                self.status.configure(text=f"Reticulum {'LOST' if rns_lost() else 'ok'}   frames {rl.FRAME_MS} ms   "
                                           f"buffer {snap['buffer_ms'] / 1000:.1f} s   "
                                           f"send late {snap['late_ms']:.0f} ms", fg=DIM)
            self.radio.max_late = 0.0
            on = snap["on_air"]
            self.b_stop.state(["!disabled"] if on or snap["waiting"] else ["disabled"])
            src = self.radio.source
            self.b_skip.state(["!disabled"] if on and src and src.folder else ["disabled"])
            self.b_switch.state(["!disabled"] if on else ["disabled"])
            aw = snap["announce_wait"]
            self.b_ann.configure(text=f"Announce in {hms(aw)}" if aw else "Announce now")
            self.b_ann.state(["disabled"] if aw else ["!disabled"])
            self._pl_refresh()

        try:
            while True:
                self.logbox.add(self.logq.get_nowait())
        except queue.Empty:
            pass
        self.root.after(120, self._update)

    def _draw_lcd(self):
        c = self.lcd
        c.delete("all")
        w = c.winfo_width() or 600
        snap = getattr(self, "snap", None) or self.radio.snapshot()
        on = snap["on_air"]

        name = (self.radio.name or "radio-lxst")[:34]
        c.create_text(12, 18, text=name, anchor="w", fill=LCD if on else LCD_DIM, font=LCD_BIG)
        blink = (self.tick // 5) % 2 == 0
        if on and rns_lost():
            draw_badge(c, w, "WAITING", True, blink, AMBER)
        elif snap["waiting"]:
            draw_badge(c, w, "STAMPS", True, blink, AMBER)
        else:
            draw_badge(c, w, "ON AIR" if on else "OFF AIR", on, blink)

        c.create_text(w - 180, 44, text=f"VOL {snap['gain_db']:+.1f}", anchor="w",
                      fill=LCD if on else LCD_DIM, font=LCD_FONT)
        if snap["limiter_on"]:
            gr = snap["reduction_db"]
            col = AMBER if gr >= 0.5 else (LCD if on else LCD_DIM)
            c.create_text(w - 180, 62, text=f"LIM -{gr:.1f}" if gr >= 0.05 else "LIM  ok",
                          anchor="w", fill=col, font=LCD_FONT)
        else:
            c.create_text(w - 180, 62, text="LIM  off", anchor="w", fill=LCD_DIM, font=LCD_FONT)

        lines = [
            f"CALL ID   {self.radio.identity_hash}",
            f"LISTENERS {(str(snap['listeners']) + (f'/{self.radio.slots}' if self.radio.slots else '')):<5} PEAK {snap['peak']:<3} SENT {RNS.prettysize(snap['bytes'])}",
            f"UPTIME    {hms(snap['uptime'])}   SRC {snap['source_state'].upper()}",
            f"PLAYING   {snap['now_playing'][:45]}" if snap["stamps_ready"] else
            f"STAMPS    preparing {snap['stamps_done']}/{snap['stamps_total']}" + (", on air when ready" if snap["waiting"] else
                                                          ", first start only"),
        ]
        for i, t in enumerate(lines):
            c.create_text(12, 44 + i * 18, text=t, anchor="w", fill=LCD if on else LCD_DIM, font=LCD_FONT,
                          tags=("cid",) if i == 0 else ())

        bar = draw_progress(c, 12, w - 192, 117, snap["progress"], on, snap["live_text"], self.tick // 2,
                            mixing=snap.get("mixing") and blink)
        # click on the bar = seek (folder track on air)
        self._seek_bar = (bar, snap["progress"][1]) if bar and on and snap["progress"] else None
        mon = self.radio.monitor is not None
        # same size as the ON AIR badge
        c.create_rectangle(w - 180, 84, w - 108, 104, outline=LCD if mon else LCD_DIM,
                           fill=LCD if mon else LCD_BG, tags=("mon",))
        c.create_text(w - 144, 94, text="MON", fill=LCD_BG if mon else LCD_DIM, font=LCD_BOLD, tags=("mon",))
        draw_vu(c, w, self.vu, on, snap["ceiling_db"] if snap["limiter_on"] else None)


def main():
    storage = os.path.dirname(os.path.abspath(load_settings()["identity"]))
    lock = rl.lock_station(storage)
    if lock is None:
        print(rl.already_on(storage), flush=True)
        r = tk.Tk()
        r.withdraw()
        from tkinter import messagebox
        messagebox.showinfo("radio-lxst station", rl.already_on(storage))
        r.destroy()
        sys.exit(1)
    root = tk.Tk()
    try:
        App(root)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(f"radio-lxst could not start: {e}")
    root.mainloop()


if __name__ == "__main__":
    main()
