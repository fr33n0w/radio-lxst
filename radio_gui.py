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
from ui_common import (BG, EDGE, LCD_BG, DIM, LCD, LCD_DIM, AMBER, UI_FONT, TITLE_FONT, LCD_FONT, LCD_BIG, hms,
                       apply_style, volume_scale, draw_vu, draw_badge, LogBox, copy_text, context_menu, hash_of)

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "settings.json")
DEFAULTS = {
    "name": "radio-lxst", "mode": "url", "url": "", "folder": "", "shuffle": False,
    "frame_ms": 120, "announce_on_start": True, "announce_every_min": 15,
    "identity": os.path.join(HERE, "storage", "identity"), "rnsconfig": "", "auto_start": False,
    "gain_db": 0.0, "limiter_on": True, "limiter_ceiling_db": -2.0,
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

        root.title("radio-lxst")
        root.configure(bg=BG)
        root.minsize(660, 590)
        apply_style(root)

        cfg = self.s["rnsconfig"] or None
        RNS.Reticulum(configdir=cfg, loglevel=RNS.LOG_NOTICE)
        RNS.logtimefmt = "%H:%M:%S"
        RNS.logdest = RNS.LOG_CALLBACK
        RNS.logcall = lambda line: self.logq.put(line)
        rl.set_frame_ms(int(self.s["frame_ms"]))
        self.radio = rl.Radio(rl.load_identity(self.s["identity"]), self.s["name"])
        self._apply_announce()

        self._build()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.log(f"radio-lxst {rl.__version__} ready, identity {self.radio.identity_hash}")
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
        nb.add(self._tab_log(nb), text="Log")
        nb.enable_traversal()

        self.status = tk.Label(r, text="", bg=BG, fg=DIM, font=UI_FONT, anchor="w")
        self.status.pack(fill="x", padx=10, pady=(0, 6))
        # click on the CALL ID line of the display copies it
        self.lcd.tag_bind("cid", "<Button-1>", lambda ev: self.copy_id())

    def _tab_listeners(self, nb):
        f = ttk.Frame(nb, padding=6)
        cols = ("id", "profile", "time", "kbps", "lost")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for c, t, w in (("id", "Listener", 255), ("profile", "Quality", 190), ("time", "Time", 70),
                        ("kbps", "kbit/s", 55), ("lost", "Lost", 40)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c in ("id", "profile") else "e")
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
        self.in_info = ttk.Label(f, text="", style="Dim.TLabel")
        self.in_info.grid(row=5, column=0, columnspan=3, sticky="w", pady=(14, 0))
        row = ttk.Frame(f)
        row.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.b_switch = ttk.Button(row, text="Switch now", style="Air.TButton", command=self.switch_source)
        self.b_switch.pack(side="left")
        ttk.Label(row, text="  changes the source on air, listeners stay connected", style="Dim.TLabel").pack(
            side="left")
        f.columnconfigure(1, weight=1)
        for v in (self.v_mode, self.v_url, self.v_folder, self.v_shuffle):
            v.trace_add("write", lambda *a: self.save())
        return f

    def _tab_output(self, nb):
        f = ttk.Frame(nb, padding=10)
        self.v_name = tk.StringVar(value=self.s["name"])
        self.v_frame = tk.IntVar(value=int(self.s["frame_ms"]))
        self.v_ann_start = tk.BooleanVar(value=self.s["announce_on_start"])
        self.v_ann_every = tk.StringVar(value=str(self.s["announce_every_min"]))

        ttk.Label(f, text="Station name").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v_name, width=36).grid(row=0, column=1, columnspan=2, sticky="w", pady=2)

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
        ttk.Spinbox(row, from_=0, to=720, width=5, textvariable=self.v_ann_every).pack(side="left", padx=4)
        ttk.Label(row, text="minutes (0 = off)").pack(side="left")
        self.ann_info = ttk.Label(f, text="", style="Dim.TLabel")
        self.ann_info.grid(row=5, column=1, columnspan=2, sticky="w")

        ttk.Label(f, text="Addresses").grid(row=6, column=0, sticky="nw", pady=(14, 0))
        for i, (lbl, val) in enumerate((("Call ID (dial this)", self.radio.identity_hash),
                                         ("LXST destination", self.radio.dest.hash.hex()),
                                         ("LXMF address", self.radio.lxmf.hash.hex()))):
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
        for v in (self.v_name, self.v_frame, self.v_ann_start, self.v_ann_every):
            v.trace_add("write", lambda *a: self._output_changed())
        return f

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
        self.radio.announce()

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

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.v_folder.get() or HERE)
        if d:
            self.v_folder.set(os.path.normpath(d))
            self.v_mode.set("folder")

    def _apply_announce(self):
        self.radio.announce_on_start = bool(self.s["announce_on_start"])
        try:
            self.radio.announce_every_min = max(0, int(self.s["announce_every_min"]))
        except (TypeError, ValueError):
            pass

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
        try:
            self.s.update({
                "name": self.v_name.get(), "mode": self.v_mode.get(), "url": self.v_url.get(),
                "folder": self.v_folder.get(), "shuffle": self.v_shuffle.get(),
                "frame_ms": int(self.v_frame.get()), "announce_on_start": self.v_ann_start.get(),
                "announce_every_min": every, "auto_start": self.v_auto.get(),
                "gain_db": self.radio.gain_db, "limiter_on": self.radio.limiter_on,
                "limiter_ceiling_db": self.radio.limiter.ceiling_db,
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
            self.radio.stop() if self.radio.on_air else None
        except Exception:
            pass
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
                if row["state"] == "listening":
                    vals = (row["id"], row["profile"], hms(row["time"]), f"{row['kbps']:.1f}", row["lost"])
                else:
                    vals = (row["id"], row["state"], "", "", "")
                self.tree.insert("", "end", values=vals)
            self.lst_info.configure(text=f"Peak {snap['peak']}   Calls {snap['calls']}   "
                                         f"Sent {RNS.prettysize(snap['bytes'])}")
            self.in_info.configure(text=f"Source: {snap['source_state']}   {snap['now_playing']}")
            na = snap["next_announce"]
            self.ann_info.configure(text=f"next announce in {hms(max(0, na))}" if na is not None else "")
            if time.time() > self.flash[1]:
                self.status.configure(text=f"Reticulum ok   frames {rl.FRAME_MS} ms   "
                                           f"buffer {snap['buffer_ms'] / 1000:.1f} s   "
                                           f"send late {snap['late_ms']:.0f} ms", fg=DIM)
            self.radio.max_late = 0.0
            on = snap["on_air"]
            self.b_stop.state(["!disabled"] if on else ["disabled"])
            src = self.radio.source
            self.b_skip.state(["!disabled"] if on and src and src.folder else ["disabled"])
            self.b_switch.state(["!disabled"] if on else ["disabled"])

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
        draw_badge(c, w, "ON AIR" if on else "OFF AIR", on, (self.tick // 5) % 2 == 0)

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
            f"LISTEN   {snap['listeners']:<3} PEAK {snap['peak']:<3} SENT {RNS.prettysize(snap['bytes'])}",
            f"UPTIME   {hms(snap['uptime'])}   SRC {snap['source_state'].upper()}",
            f"PLAYING  {snap['now_playing'][:46]}",
        ]
        for i, t in enumerate(lines):
            c.create_text(12, 44 + i * 18, text=t, anchor="w", fill=LCD if on else LCD_DIM, font=LCD_FONT,
                          tags=("cid",) if i == 0 else ())

        draw_vu(c, w, self.vu, on)


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
