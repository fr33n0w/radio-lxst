#!/usr/bin/env python3
# radio-lxst client GUI: station list + player, same look as the server console.

import os
import sys
import json
import time
import queue
import faulthandler
import tkinter as tk
from tkinter import ttk

import RNS
import radio_client as rc
from ui_common import (BG, LCD, LCD_DIM, AMBER, RED, DIM, UI_FONT, TITLE_FONT, LCD_FONT, LCD_BIG, hms,
                       apply_style, volume_scale, draw_vu, draw_badge, LogBox, copy_text, context_menu, hash_of)

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "client_settings.json")
DEFAULTS = {"call_id": "", "quality": "SHQ", "volume_db": 0.0, "buffer_s": 3.0, "device": "",
            "identity": os.path.join(HERE, "storage", "client_identity"), "rnsconfig": "", "autoplay": False}
BADGES = {"playing": ("PLAYING", LCD), "buffering": ("BUFFER", AMBER), "searching": ("SEARCH", AMBER),
          "retrying": ("RETRY", AMBER),
          "connecting": ("CONNECT", AMBER), "ringing": ("RINGING", AMBER), "failed": ("FAILED", RED),
          "ended": ("ENDED", RED)}


def open_log(name, keep=1_000_000):
    """storage/<name>, emptied when it grows past keep bytes."""
    try:
        path = os.path.join(HERE, "storage", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        big = os.path.isfile(path) and os.path.getsize(path) > keep
        return open(path, "w" if big else "a", encoding="utf-8")
    except OSError:
        return None


LOGFILE = open_log("client.log")
_crash = open_log("client_crash.log")
if _crash:
    faulthandler.enable(file=_crash, all_threads=True)  # native crashes leave a trace even under pythonw


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def ago(t):
    d = time.time() - t
    if d < 90:
        return "now"
    if d < 5400:
        return f"{int(d // 60)} min"
    if d < 172800:
        return f"{int(d // 3600)} h"
    return f"{int(d // 86400)} d"


class App:
    def __init__(self, root):
        self.root = root
        self.s = load_settings()
        self.logq = queue.Queue()
        self.tick = 0
        self.vu = [0.0, 0.0]
        self.dir_dirty = True
        self.flash = ("", 0)

        root.title("radio-lxst client")
        root.configure(bg=BG)
        root.minsize(760, 600)
        apply_style(root)

        RNS.Reticulum(configdir=self.s["rnsconfig"] or None, loglevel=RNS.LOG_NOTICE)
        RNS.logtimefmt = "%H:%M:%S"
        RNS.logdest = RNS.LOG_CALLBACK
        RNS.logcall = self._logline
        self.dir = rc.Directory(os.path.join(HERE, "storage", "stations.json"))
        self.dir.on_change = lambda: setattr(self, "dir_dirty", True)
        self.client = rc.Client(rc.load_identity(self.s["identity"]), float(self.s["buffer_s"]),
                                self.s["device"] or None)

        self._build()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.log(f"radio-lxst client {rc.__version__} ready")
        if self.s.get("autoplay") and self.s["call_id"]:
            root.after(800, lambda: self.play("autoplay"))
        self._update()

    def _build(self):
        r = self.root
        top = tk.Frame(r, bg=BG)
        top.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(top, text="radio-lxst", bg=BG, fg=LCD, font=TITLE_FONT).pack(side="left")
        tk.Label(top, text="  client", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")

        lcd_wrap = tk.Frame(r, bg="#4b4b5c", padx=2, pady=2)
        lcd_wrap.pack(fill="x", padx=8, pady=6)
        self.lcd = tk.Canvas(lcd_wrap, height=118, bg="#050805", highlightthickness=0)
        self.lcd.pack(fill="x")

        bar = tk.Frame(r, bg=BG)
        bar.pack(fill="x", padx=8)
        ttk.Button(bar, text="▶  Play", style="Air.TButton", command=lambda: self.play("Play button")).pack(side="left", padx=(0, 6))
        self.b_stop = ttk.Button(bar, text="■  Stop", style="Stop.TButton", command=self.stop)
        self.b_stop.pack(side="left", padx=(0, 10))
        tk.Label(bar, text="Call ID", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        self.v_call = tk.StringVar(value=self.s["call_id"])
        e = ttk.Entry(bar, textvariable=self.v_call, width=34)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self.play("Enter"))
        self.v_quality = tk.StringVar(value=self.s["quality"])
        ttk.Combobox(bar, textvariable=self.v_quality, values=list(rc.PROFILES), width=5,
                     state="readonly").pack(side="left", padx=4)

        mix = tk.Frame(r, bg=BG)
        mix.pack(fill="x", padx=8, pady=(8, 0))
        self.v_vol = tk.DoubleVar(value=float(self.s["volume_db"]))
        tk.Label(mix, text="VOLUME", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        volume_scale(mix, self.v_vol, length=160).pack(side="left", padx=6)
        self.vol_lbl = tk.Label(mix, text="", bg=BG, fg=LCD, font=LCD_FONT, width=8, anchor="w", cursor="hand2")
        self.vol_lbl.pack(side="left")
        self.vol_lbl.bind("<Double-Button-1>", lambda ev: self.v_vol.set(0.0))
        tk.Label(mix, text="buffer", bg=BG, fg=DIM, font=UI_FONT).pack(side="left", padx=(8, 0))
        self.v_buf = tk.StringVar(value=str(self.s["buffer_s"]))
        ttk.Spinbox(mix, from_=0.5, to=10, increment=0.5, width=4, textvariable=self.v_buf).pack(side="left", padx=4)
        tk.Label(mix, text="s   out", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        self.v_dev = tk.StringVar(value=self.s["device"] or "default")
        ttk.Combobox(mix, textvariable=self.v_dev, values=self._devices(), width=18,
                     state="readonly").pack(side="left", padx=4)
        for v in (self.v_vol, self.v_buf, self.v_dev, self.v_quality, self.v_call):
            v.trace_add("write", lambda *a: self._changed())
        self._changed()

        nb = ttk.Notebook(r)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        nb.add(self._tab_stations(nb), text="Stations")
        f = ttk.Frame(nb, padding=4)
        self.logbox = LogBox(f)
        nb.add(f, text="Log")

        self.status = tk.Label(r, text="", bg=BG, fg=DIM, font=UI_FONT, anchor="w", cursor="hand2")
        self.status.pack(fill="x", padx=10, pady=(0, 6))
        self.status.bind("<Button-1>", lambda ev: self.copied("client ID", self.client.identity.hash.hex()))
        # click on the CALL ID line of the display copies it
        self.lcd.tag_bind("cid", "<Button-1>", lambda ev: self.snap["call_id"] and
                          self.copied("Call ID", self.snap["call_id"]))

    def _tab_stations(self, nb):
        f = ttk.Frame(nb, padding=6)
        cols = ("name", "air", "lst", "np", "cid", "hops", "heard")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for c, t, w in (("name", "Station", 160), ("air", "", 34), ("lst", "Listeners", 75),
                        ("np", "Now playing", 95), ("cid", "Call ID", 255), ("hops", "Hops", 40), ("heard", "Heard", 54)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="e" if c in ("lst", "hops") else "w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-Button-1>", self._tune_clicked)
        self.tree.bind("<Button-3>", self._station_menu)
        self.tree.bind("<Control-c>", lambda ev: self._copy_selected())
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Listen", command=lambda: self._tune_selected("Listen button")).pack(side="left")
        ttk.Button(row, text="Copy Call ID", command=self._copy_selected).pack(side="left", padx=6)
        ttk.Button(row, text="Forget", command=self._forget_selected).pack(side="left")
        self.v_auto = tk.BooleanVar(value=bool(self.s.get("autoplay")))
        ttk.Checkbutton(row, text="Play last station at start", variable=self.v_auto).pack(side="right")
        self.v_auto.trace_add("write", lambda *a: self._changed())
        return f

    def _station_menu(self, ev):
        cid = self.tree.identify_row(ev.y)
        if not cid:
            return
        self.tree.selection_set(cid)
        name = self.dir.name_of(cid)
        context_menu(self.root, ev, [
            ("Listen", lambda: self._tune_selected("menu")),
            None,
            ("Copy Call ID", lambda: self.copied("Call ID", cid)),
            ("Copy LXMF address", lambda: self.copied("LXMF address", hash_of("lxmf.delivery", cid))),
            ("Copy LXST destination", lambda: self.copied("LXST destination", hash_of("lxst.telephony", cid))),
            ("Copy name", lambda: self.copied("name", name)) if name else None,
            None,
            ("Forget", self._forget_selected),
        ])

    def _copy_selected(self):
        sel = self.tree.selection()
        if sel:
            self.copied("Call ID", sel[0])

    def copied(self, what, text):
        copy_text(self.root, text)
        self.flash = (f"Copied {what}: {text}", time.time() + 3)
        self.status.configure(text=self.flash[0], fg=LCD)

    def _devices(self):
        return ["default"] + rc.list_devices()

    # actions
    def play(self, why=""):
        cid = self.v_call.get().strip().lower()
        if not cid:
            return self.log("Pick a station or paste a Call ID")
        self._changed()
        p = self.client.player
        p.prefill = self._buf()
        p.device = None if self.v_dev.get() == "default" else self.v_dev.get()
        self.client.tune(cid, rc.PROFILES.get(self.v_quality.get(), 0x60), self.dir.name_of(cid), why)

    def stop(self):
        self.client.hangup()

    def _tune_selected(self, why=""):
        sel = self.tree.selection()
        if sel:
            self.v_call.set(sel[0])
            self.play(why)

    def _tune_clicked(self, ev):
        # the row under the mouse, not the selection: the list may have been redrawn between the clicks
        cid = self.tree.identify_row(ev.y)
        if cid:
            self.tree.selection_set(cid)
            self.v_call.set(cid)
            self.play("double click")

    def _forget_selected(self):
        for cid in self.tree.selection():
            self.dir.stations.pop(cid, None)
        self.dir.save()
        self.dir_dirty = True

    def _buf(self):
        try:
            return max(0.5, min(10.0, float(self.v_buf.get())))
        except ValueError:
            return 3.0

    def _changed(self):
        g = round(float(self.v_vol.get()) * 2) / 2
        self.client.player.volume_db = g
        self.vol_lbl.configure(text=f"{g:+.1f} dB")
        self.s.update({"call_id": self.v_call.get().strip(), "quality": self.v_quality.get(), "volume_db": g,
                       "buffer_s": self._buf(), "device": "" if self.v_dev.get() == "default" else self.v_dev.get(),
                       "autoplay": self.v_auto.get() if hasattr(self, "v_auto") else bool(self.s.get("autoplay"))})
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(self.s, f, indent=2)
        except OSError:
            pass

    def _logline(self, line):
        self.logq.put(line)
        if LOGFILE:
            try:
                LOGFILE.write(time.strftime("%Y-%m-%d ") + line + "\n")
                LOGFILE.flush()
            except Exception:
                pass

    def log(self, msg):
        self.logq.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def quit(self):
        self._changed()
        try:
            self.client.hangup()
        except Exception:
            pass
        self.root.destroy()
        os._exit(0)

    # refresh
    def _update(self):
        self.tick += 1
        snap = self.client.snapshot()
        self.snap = snap
        for i in (0, 1):
            lv = snap["levels"][i]
            self.vu[i] = lv if lv > self.vu[i] else self.vu[i] * 0.8
        self._draw_lcd()

        if self.tick % 8 == 0 or self.dir_dirty:
            self.dir_dirty = False
            st = self.dir.stations.get(snap["call_id"])
            if st and snap["state"] == "playing" and snap["listeners"] is not None:
                # keep the list in step with what the station tells us in the call
                st.name = snap["name"] or st.name
                st.listeners, st.now_playing, st.heard = snap["listeners"], snap["track"], time.time()
            sel = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            for st in sorted(list(self.dir.stations.values()), key=lambda s: (not s.on_air, -s.heard)):
                hops = st.hops
                self.tree.insert("", "end", iid=st.call_id, values=(
                    st.name or "?", "ON" if st.on_air else "off",
                    st.listeners if st.listeners is not None else "", st.now_playing, st.call_id,
                    hops if hops is not None else "", ago(st.heard)))
            for cid in sel:
                if self.tree.exists(cid):
                    self.tree.selection_add(cid)
            if time.time() > self.flash[1]:
                self.status.configure(text=f"Reticulum ok   stations {len(self.dir.stations)}   "
                                           f"client {self.client.identity.hash.hex()}", fg=DIM)

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
        s = self.snap
        state = s["player"] if s["state"] == "playing" else s["state"]
        active = state not in ("idle",)
        col = LCD if active else LCD_DIM

        name = s["name"] or ("unknown station" if s["call_id"] else "radio-lxst")
        c.create_text(12, 18, text=name[:30], anchor="w", fill=col, font=LCD_BIG)
        text, bcol = BADGES.get(state, ("STOPPED", LCD_DIM))
        draw_badge(c, w, text, state != "idle", state == "playing" or (self.tick // 5) % 2 == 0, bcol)

        # buffer bar
        target = max(0.5, self.client.player.prefill)
        frac = min(1.0, s["buffer_s"] / (target + 4))
        x0, x1 = w - 180, w - 108
        c.create_text(x0, 44, text=f"BUF {s['buffer_s']:.1f}s", anchor="w", fill=col, font=LCD_FONT)
        c.create_rectangle(x0, 54, x1, 60, outline=LCD_DIM)
        if active:
            c.create_rectangle(x0, 54, x0 + (x1 - x0) * frac, 60, outline="",
                               fill=AMBER if s["buffer_s"] < target / 2 else LCD)
        c.create_text(x0, 74, text=f"VOL {self.client.player.volume_db:+.1f}", anchor="w", fill=col, font=LCD_FONT)

        msg = s["message"] or ""
        lines = [
            f"CALL ID  {s['call_id'] or '-'}",
            f"STATE    {state.upper()}  {msg[:30]}" + (f"   LISTEN {s['listeners']}" if s['listeners'] is not None else ""),
            f"TIME     {hms(s['time'])}   {s['kbps']:.1f} kbit/s   HOPS {s['hops'] if s['hops'] is not None else '-'}"
            f"   DROPS {s['underruns']}",
            f"TRACK    {s['track'][:46] or '-'}",
        ]
        for i, t in enumerate(lines):
            c.create_text(12, 44 + i * 18, text=t, anchor="w", fill=col, font=LCD_FONT, tags=("cid",) if i == 0 else ())
        draw_vu(c, w, self.vu, active)


def main():
    root = tk.Tk()
    try:
        App(root)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(f"radio-lxst client could not start: {e}")
    root.mainloop()


if __name__ == "__main__":
    main()
