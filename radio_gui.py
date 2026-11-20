#!/usr/bin/env python3
# radio-lxst GUI: a small broadcast console in the spirit of the old Winamp source plugins.

import os
import sys
import json
import time
import queue
import tkinter as tk
from tkinter import ttk, filedialog

import RNS
import radio_lxst as rl
import radio_client as rc
import station_lxmf as sl
from ui_common import (LCD_BOLD, BG, EDGE, LCD_BG, DIM, LCD, LCD_DIM, AMBER, UI_FONT, TITLE_FONT, LCD_FONT, LCD_BIG, hms,
                       apply_style, volume_scale, draw_vu, draw_badge, LogBox, copy_text, context_menu, hash_of, update_banner, about_tab, rns_watch, rns_lost)

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "settings.json")
DEFAULTS = {
    "name": "radio-lxst", "mode": "url", "url": "", "folder": "", "shuffle": False, "input_device": "",
    "frame_ms": 120, "announce_on_start": True, "announce_every_min": 60,
    "identity": os.path.join(HERE, "storage", "identity"), "rnsconfig": "", "auto_start": False,
    "gain_db": 0.0, "limiter_on": True, "limiter_ceiling_db": -2.0, "monitor_device": "",
    "lxmf_on": True, "lxmf_every_h": 3, "slots": 0,
}

def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
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
        update_banner(root, self.status, rl.__version__, self.log)
        rns_watch(root, self.status)
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
        self.lcd = tk.Canvas(lcd_wrap, height=118, bg=LCD_BG, highlightthickness=0)
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

        # output volume + limiter, live
        mix = tk.Frame(r, bg=BG)
        mix.pack(fill="x", padx=8, pady=(8, 0))
        self.v_gain = tk.DoubleVar(value=float(self.s["gain_db"]))
        self.v_lim = tk.BooleanVar(value=bool(self.s["limiter_on"]))
        self.v_ceil = tk.StringVar(value=str(self.s["limiter_ceiling_db"]))
        tk.Label(mix, text="VOLUME", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        volume_scale(mix, self.v_gain).pack(side="left", padx=6)
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
        for v in (self.v_gain, self.v_lim, self.v_ceil):
            v.trace_add("write", lambda *a: self._mix_changed())
        self._mix_changed()

        nb = ttk.Notebook(r)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        nb.add(self._tab_listeners(nb), text="Listeners")
        nb.add(self._tab_input(nb), text="Input")
        nb.add(self._tab_output(nb), text="Output")
        nb.add(self._tab_replies(nb), text="Replies")
        nb.add(self._tab_log(nb), text="Log")
        nb.add(about_tab(nb, "radio-lxst", rl.__version__), text="About")
        nb.enable_traversal()

        self.status = tk.Label(r, text="", bg=BG, fg=DIM, font=UI_FONT, anchor="w")
        self.status.pack(fill="x", padx=10, pady=(0, 6))
        # click on the CALL ID line of the display copies it
        self.lcd.tag_bind("cid", "<Button-1>", lambda ev: self.copy_id())
        self.lcd.tag_bind("mon", "<Button-1>", lambda ev: self.toggle_monitor())

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
        ttk.Radiobutton(f, text="Folder playlist (mp3, ogg, opus, flac, wav, m4a)",
                        variable=self.v_mode, value="folder").grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Entry(f, textvariable=self.v_folder, width=50).grid(row=3, column=0, columnspan=2, sticky="we",
                                                                  padx=(20, 6), pady=2)
        ttk.Button(f, text="Browse...", command=self.browse).grid(row=3, column=2, sticky="w")
        ttk.Checkbutton(f, text="Shuffle", variable=self.v_shuffle).grid(row=4, column=0, sticky="w",
                                                                           padx=(20, 0), pady=4)
        self.b_skip = ttk.Button(f, text="Next track", command=self.skip)
        self.b_skip.grid(row=4, column=1, sticky="w", pady=4)
        ttk.Radiobutton(f, text="Line input (mic, sound card, VB-Cable, SDR app, Loopback = what the PC plays)",
                        variable=self.v_mode, value="line").grid(row=5, column=0, columnspan=3, sticky="w",
                                                                   pady=(12, 0))
        self.cb_input = ttk.Combobox(f, textvariable=self.v_input, width=50, state="readonly")
        self.cb_input.grid(row=6, column=0, columnspan=2, sticky="we", padx=(20, 6), pady=2)
        self.cb_input.bind("<<ComboboxSelected>>", lambda e: self.v_mode.set("line"))
        ttk.Button(f, text="Refresh", command=self.refresh_inputs).grid(row=6, column=2, sticky="w")
        self.in_info = ttk.Label(f, text="", style="Dim.TLabel")
        self.in_info.grid(row=7, column=0, columnspan=3, sticky="w", pady=(14, 0))
        row = ttk.Frame(f)
        row.grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.b_switch = ttk.Button(row, text="Switch now", style="Air.TButton", command=self.switch_source)
        self.b_switch.pack(side="left")
        ttk.Label(row, text="  changes the source on air, listeners stay connected", style="Dim.TLabel").pack(
            side="left")
        f.columnconfigure(1, weight=1)
        self.refresh_inputs()
        for v in (self.v_mode, self.v_url, self.v_folder, self.v_shuffle, self.v_input):
            v.trace_add("write", lambda *a: self.save())
        return f

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
        return rl.Source(folder=folder, shuffle=self.v_shuffle.get())

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
            self.radio.switch_source(src)

    def stop(self):
        if self.radio.on_air:
            self.radio.stop()

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
        try:
            self.s.update({
                "name": self.v_name.get(), "mode": self.v_mode.get(), "url": self.v_url.get(),
                "folder": self.v_folder.get(), "shuffle": self.v_shuffle.get(),
                "input_device": self.v_input.get(), "monitor_device": self._monitor_device(),
                "frame_ms": int(self.v_frame.get()), "announce_on_start": self.v_ann_start.get(),
                "announce_every_min": every, "auto_start": self.v_auto.get(),
                "gain_db": self.radio.gain_db, "limiter_on": self.radio.limiter_on,
                "limiter_ceiling_db": self.radio.limiter.ceiling_db,
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
            f"CALL ID  {self.radio.identity_hash}",
            f"LISTEN   {(str(snap['listeners']) + (f'/{self.radio.slots}' if self.radio.slots else '')):<5} PEAK {snap['peak']:<3} SENT {RNS.prettysize(snap['bytes'])}",
            f"UPTIME   {hms(snap['uptime'])}   SRC {snap['source_state'].upper()}",
            f"PLAYING  {snap['now_playing'][:46]}" if snap["stamps_ready"] else
            f"STAMPS   preparing {snap['stamps_done']}/{snap['stamps_total']}" + (", on air when ready" if snap["waiting"] else
                                                          ", first start only"),
        ]
        for i, t in enumerate(lines):
            c.create_text(12, 44 + i * 18, text=t, anchor="w", fill=LCD if on else LCD_DIM, font=LCD_FONT,
                          tags=("cid",) if i == 0 else ())

        mon = self.radio.monitor is not None
        # same size as the ON AIR badge
        c.create_rectangle(w - 180, 84, w - 108, 104, outline=LCD if mon else LCD_DIM,
                           fill=LCD if mon else LCD_BG, tags=("mon",))
        c.create_text(w - 144, 94, text="MON", fill=LCD_BG if mon else LCD_DIM, font=LCD_BOLD, tags=("mon",))
        draw_vu(c, w, self.vu, on, snap["ceiling_db"] if snap["limiter_on"] else None)


def main():
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
