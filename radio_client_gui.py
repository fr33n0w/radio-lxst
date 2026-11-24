#!/usr/bin/env python3
# radio-lxst client GUI: station list + player, same look as the server console.

import os
import sys
import json
import time
import queue
import random
import threading
import faulthandler
import tkinter as tk
from tkinter import ttk

import RNS
import radio_client as rc
import client_lxmf
import listener_stats as lstats
from ui_common import (app_id, stop_children, set_icon, BG, LCD, LCD_DIM, LCD_BG, AMBER, RED, DIM, UI_FONT, TITLE_FONT, LCD_FONT, LCD_BIG, LOG_FONT, LCD_BOLD, hms,
                       apply_style, volume_scale, draw_vu, draw_badge, draw_progress, LogBox, copy_text, context_menu, hash_of, update_banner, about_tab, rns_watch, rns_lost, usage_watch)

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "client_settings.json")
# emoji on Windows; Linux often has no emoji font (empty boxes), so plain symbols there
ICONS = ({"next": "⏭", "random": "🔀", "mute": "🔊", "unmute": "🔇", "radio": "📻"} if sys.platform == "win32" else
         {"next": "»", "random": "⤮", "mute": "♪", "unmute": "✕", "radio": "♫"})
DEFAULTS = {"call_id": "", "quality": "SHQ", "volume_db": 0.0, "buffer_s": 3.0, "device": "",
            "identity": os.path.join(HERE, "storage", "client_identity"), "rnsconfig": "", "autoplay": False,
            "nick": "", "reconnect": True}
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
        self._marked_off = None  # call ID already marked off air for the current hang up
        self.flash = ("", 0)

        root.title(f"RADIO-LXST CLIENT v{rc.__version__}")
        root.configure(bg=BG)
        root.minsize(800, 600)
        apply_style(root)

        RNS.Reticulum(configdir=self.s["rnsconfig"] or None, loglevel=RNS.LOG_NOTICE)
        RNS.logtimefmt = "%H:%M:%S"
        RNS.logdest = RNS.LOG_CALLBACK
        RNS.logcall = self._logline
        self.dir = rc.Directory(os.path.join(HERE, "storage", "stations.json"))
        self.dir.on_change = lambda: setattr(self, "dir_dirty", True)
        self.client = rc.Client(rc.load_identity(self.s["identity"]), float(self.s["buffer_s"]),
                                self.s["device"] or None, nick=self.s.get("nick", ""))
        self.dir.on_heard = self.client.station_back
        self.cmds = None
        self.cmds_wait = "preparing the LXMF address..."
        self.stamping = False
        self.cmds_go = False
        self.notices = queue.Queue()
        threading.Thread(target=self._start_cmds, daemon=True).start()

        self._build()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.log(f"radio-lxst client {rc.__version__} ready")
        update_banner(root, self.status_row, rc.__version__, self.log)
        rns_watch(root, self.status_row)
        if self.s.get("autoplay") and self.s["call_id"]:
            root.after(800, lambda: self.play("autoplay"))
        self._update()

    def _build(self):
        r = self.root
        top = tk.Frame(r, bg=BG)
        top.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(top, text="radio-lxst", bg=BG, fg=LCD, font=TITLE_FONT).pack(side="left")
        tk.Label(top, text="  client", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        # our name for the stations (sent in the call); empty = Radio-LXST Client #xxxx
        self.v_nick = tk.StringVar(value=self.s.get("nick", ""))
        ttk.Entry(top, textvariable=self.v_nick, width=22).pack(side="right")
        tk.Label(top, text="Your name", bg=BG, fg=DIM, font=UI_FONT).pack(side="right", padx=(0, 4))

        lcd_wrap = tk.Frame(r, bg="#4b4b5c", padx=2, pady=2)
        lcd_wrap.pack(fill="x", padx=8, pady=6)
        self.lcd = tk.Canvas(lcd_wrap, height=130, bg="#050805", highlightthickness=0)
        self.lcd.pack(fill="x")

        # transport row, volume on the right like a player
        bar = tk.Frame(r, bg=BG)
        bar.pack(fill="x", padx=8)
        for text, style, cmd in (("▶  Play", "Air", lambda: self.play("Play button")), ("■  Stop", "Stop", self.stop),
                                 (ICONS["next"] + "  Next", "Next", lambda: self.next_station(False)),
                                 (ICONS["random"] + "  Random", "Next", lambda: self.next_station(True)),
                                 (ICONS["mute"] + "  Mute", "Mute", self.toggle_mute), ("⟳  Restart", "Restart", self.restart)):
            b = ttk.Button(bar, text=text, style=f"{style}.TButton", width=10, padding=(4, 3), command=cmd)
            b.pack(side="left", padx=(0, 4))
            if style == "Mute":
                self.b_mute = b
        self.v_vol = tk.DoubleVar(value=float(self.s["volume_db"]))
        self.vol_lbl = tk.Label(bar, text="", bg=BG, fg=LCD, font=LCD_FONT, width=8, anchor="w", cursor="hand2")
        self.vol_lbl.pack(side="right")
        self.vol_lbl.bind("<Double-Button-1>", lambda ev: self.v_vol.set(0.0))
        # slider placed (not packed) so following the out combobox width cannot grow the window
        vbox = tk.Frame(bar, bg=BG, width=110)
        vbox.pack_propagate(False)
        vbox.pack(side="right", padx=4, fill="both", expand=True)
        vs = volume_scale(vbox, self.v_vol, length=60)
        vbox.configure(height=vs.winfo_reqheight())
        tk.Label(bar, text="VOLUME", bg=BG, fg=DIM, font=UI_FONT).pack(side="right", padx=(4, 0))

        mix = tk.Frame(r, bg=BG)
        mix.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(mix, text="Call ID", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        self.v_call = tk.StringVar(value=self.s["call_id"])
        e = ttk.Entry(mix, textvariable=self.v_call, width=34)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self.play("Enter"))
        self.v_quality = tk.StringVar(value=self.s["quality"])
        ttk.Combobox(mix, textvariable=self.v_quality, values=list(rc.PROFILES), width=5,
                     state="readonly").pack(side="left", padx=4)
        ttk.Button(mix, text="Save station", width=12, command=self._save_call).pack(side="left", padx=4)
        tk.Label(mix, text="buffer", bg=BG, fg=DIM, font=UI_FONT).pack(side="left", padx=(8, 0))
        self.v_buf = tk.StringVar(value=str(self.s["buffer_s"]))
        ttk.Spinbox(mix, from_=0.5, to=10, increment=0.5, width=4, textvariable=self.v_buf).pack(side="left", padx=4)
        tk.Label(mix, text="s", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        tk.Label(mix, text="AUDIO OUT:", bg=BG, fg=DIM, font=UI_FONT).pack(side="left", padx=(12, 0))
        self.v_dev = tk.StringVar(value=self.s["device"] or "default")
        out = ttk.Combobox(mix, textvariable=self.v_dev, values=self._devices(), width=26, state="readonly")
        out.pack(side="left", padx=4, fill="x", expand=True)

        def fit_vol(_ev=None):
            vs.place(relx=1, rely=0.5, anchor="e", width=max(60, min(out.winfo_width(), vbox.winfo_width())))
        out.bind("<Configure>", fit_vol, add="+")
        vbox.bind("<Configure>", fit_vol, add="+")
        for v in (self.v_vol, self.v_buf, self.v_dev, self.v_quality, self.v_call, self.v_nick):
            v.trace_add("write", lambda *a: self._changed())
        self.v_quality.trace_add("write", lambda *a: self.client.set_profile(rc.PROFILES.get(self.v_quality.get(), 0x60)))
        self._changed()

        nb = ttk.Notebook(r)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        nb.add(self._tab_stations(nb), text="Stations")
        nb.add(self._tab_commands(nb), text="Commands")
        nb.add(self._tab_mystats(nb), text="My stats")
        nb.bind("<<NotebookTabChanged>>", lambda ev: nb.index("current") == 1 and self._cmd_pick_current())
        f = ttk.Frame(nb, padding=4)
        self.logbox = LogBox(f)
        nb.add(f, text="Log")
        nb.add(about_tab(nb, "radio-lxst client", rc.__version__, "client"), text="About")

        # usage in its own label beside status, as in the station
        self.status_row = tk.Frame(r, bg=BG)
        self.status_row.pack(fill="x", padx=10, pady=(0, 6))
        self.usage = tk.Label(self.status_row, text="", bg=BG, fg=DIM, font=UI_FONT)
        self.usage.pack(side="right")
        self.status = tk.Label(self.status_row, text="", bg=BG, fg=DIM, font=UI_FONT, anchor="w", cursor="hand2")
        self.status.pack(side="left", fill="x", expand=True)
        usage_watch(r.winfo_toplevel(), self.usage)
        self.status.bind("<Button-1>", lambda ev: self.copied("client ID", self.client.identity.hash.hex()))
        # click on the CALL ID line of the display copies it
        self.lcd.tag_bind("cid", "<Button-1>", lambda ev: self.snap["call_id"] and
                          self.copied("Call ID", self.snap["call_id"]))

    def _tab_stations(self, nb):
        f = ttk.Frame(nb, padding=6)
        cols = ("fav", "name", "air", "sub", "lst", "q", "rx", "kbps", "np", "cid", "hops", "ver", "heard")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for c, t, w in (("fav", "★", 26), ("name", "Station", 160), ("air", "", 34), ("sub", "Sub", 36), ("lst", "Listeners", 75),
                        ("q", "Quality", 55), ("rx", "Receiving", 120), ("kbps", "kbit/s", 50),
                        ("np", "Now playing", 95), ("cid", "Call ID", 255), ("hops", "Hops", 40), ("ver", "Version", 55),
                        ("heard", "Heard", 54)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c == "fav" else "e" if c in ("lst", "hops", "kbps")
                             else "w", stretch=c != "fav")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-Button-1>", self._tune_clicked)
        self.tree.bind("<Button-1>", self._star_clicked, add="+")
        self.tree.bind("<Button-3>", self._station_menu)
        self.tree.bind("<Control-c>", lambda ev: self._copy_selected())
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Listen", command=lambda: self._tune_selected("Listen button")).pack(side="left")
        ttk.Button(row, text="Copy Call ID", command=self._copy_selected).pack(side="left", padx=6)
        ttk.Button(row, text="Forget", command=self._forget_selected).pack(side="left")
        ttk.Button(row, text="Refresh", command=lambda: self.dir.refresh(self._playing_cid())).pack(side="left", padx=6)
        self.v_auto = tk.BooleanVar(value=bool(self.s.get("autoplay")))
        ttk.Checkbutton(row, text="Play last station at start", variable=self.v_auto).pack(side="right")
        self.v_auto.trace_add("write", lambda *a: self._changed())
        self.v_reconnect = tk.BooleanVar(value=bool(self.s.get("reconnect", True)))
        ttk.Checkbutton(row, text="Retry on hang up", variable=self.v_reconnect).pack(
            side="right", padx=(0, 10))
        self.v_reconnect.trace_add("write", lambda *a: self._changed())
        self.client.reconnect = self.v_reconnect.get()
        return f

    def _tab_commands(self, nb):
        # station autoresponder commands over LXMF, no free text
        f = ttk.Frame(nb, padding=6)
        row = ttk.Frame(f)
        row.pack(fill="x")
        tk.Label(row, text="Station", bg=BG, fg=DIM, font=UI_FONT).pack(side="left")
        self.v_cmd_st = tk.StringVar()
        self.cmd_box = ttk.Combobox(row, textvariable=self.v_cmd_st, width=70, state="readonly")
        self.cmd_box.pack(side="left", padx=6, fill="x", expand=True)
        self.v_cmd_st.trace_add("write", lambda *a: setattr(self, "cmd_shown", None))
        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(6, 4))
        self.cmd_btns = []
        for c in client_lxmf.COMMANDS:
            b = ttk.Button(btns, text=c.capitalize(), command=lambda c=c: self._send_cmd(c), state="disabled")
            b.pack(side="left", padx=(0, 4))
            self.cmd_btns.append(b)
        self.cmd_btns_on = False
        ttk.Button(btns, text="Clear", command=self._clear_cmds).pack(side="right")
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        self.cmd_text = tk.Text(wrap, bg=LCD_BG, fg=LCD, font=LOG_FONT, relief="flat", wrap="word", height=8)
        sb = ttk.Scrollbar(wrap, command=self.cmd_text.yview)
        self.cmd_text.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.cmd_text.pack(fill="both", expand=True)
        self.cmd_text.tag_configure("out", foreground=AMBER)
        self.cmd_text.tag_configure("dim", foreground=DIM)
        self.cmd_text.tag_configure("bad", foreground=RED)
        arow = ttk.Frame(f)
        arow.pack(fill="x", pady=(4, 0))
        self.b_ann = ttk.Button(arow, text="Announce", command=self._announce, state="disabled")
        self.b_ann.pack(side="right")
        self.cmd_addr = tk.Label(arow, text="", bg=BG, fg=DIM, font=UI_FONT, anchor="w", cursor="hand2")
        self.cmd_addr.pack(side="left", fill="x", expand=True)
        self.cmd_sub = tk.Label(btns, text="", bg=BG, fg=DIM, font=UI_FONT)
        self.cmd_sub.pack(side="right", padx=8)
        self.cmd_addr.bind("<Button-1>", lambda ev: self.cmds and self.copied("LXMF address", self.cmds.address))
        self.cmd_shown = None
        self.cmd_ids = {}
        return f

    def _start_cmds(self):
        # our LXMF address goes up only with its announce stamp made (in a child process, first start only)
        try:
            if not client_lxmf.load_ready(self.client.identity):
                self.stamping = True
                self.cmds_wait = ("making the announce stamp of your LXMF address: first start only, "
                                  "a few minutes in the background")
            ok = client_lxmf.ensure_stamp(self.client.identity)
            self.stamping = False
            if not ok:
                self.cmds_wait = "could not make the announce stamp, commands are off (see Log)"
                return
            self.cmds_go = True  # the router is made by _update: LXMRouter sets signal handlers, main thread only
        except Exception as e:
            self.cmds_wait = f"commands are off: {e}"
            RNS.log(f"LXMF commands off: {e}", RNS.LOG_ERROR)

    def _make_cmds(self):
        self.cmds_go = False
        try:
            self.cmds = client_lxmf.Commands(self.client.identity, os.path.join(HERE, "storage"),
                                             self.client.listener_name, self.dir)
            self.cmds.on_notice = lambda cid, text: self.notices.put((cid, text))
        except Exception as e:
            self.cmds_wait = f"commands are off: {e}"
            RNS.log(f"LXMF commands off: {e}", RNS.LOG_ERROR)

    def _cmd_station(self):
        """Call ID of the station picked (the list shows its LXMF address, the one that gets the messages)."""
        v = self.v_cmd_st.get()
        return self.cmd_ids.get(v.rsplit(" ", 1)[-1], "") if v else ""

    def _cmd_choices(self):
        rows = [(st.name or "?", st.call_id) for st in
                sorted(list(self.dir.stations.values()), key=lambda s: (not s.on_air, -s.heard))]
        cid = self.v_call.get().strip().lower()
        if len(cid) == 32 and cid not in self.dir.stations and all(ch in "0123456789abcdef" for ch in cid):
            rows.append(("?", cid))
        out = []
        for name, c in rows:
            lx = hash_of("lxmf.delivery", c)
            self.cmd_ids[lx] = c
            out.append(f"{name}  LXMF {lx}")
        return out

    def _cmd_pick_current(self):
        """Opening the tab: the station of the Call ID field (the one playing or picked last)."""
        self.cmd_box.configure(values=self._cmd_choices())
        cid = self.v_call.get().strip().lower()
        lx = next((k for k, v in self.cmd_ids.items() if v == cid), "")
        for v in self.cmd_box.cget("values"):
            if lx and v.endswith(" " + lx):
                self.v_cmd_st.set(v)
                return
        if not self.v_cmd_st.get() and self.cmd_box.cget("values"):
            self.v_cmd_st.set(self.cmd_box.cget("values")[0])

    def _send_cmd(self, cmd):
        cid = self._cmd_station()
        if not self.cmds:
            return self.log(f"Commands not ready: {self.cmds_wait}")
        if len(cid) != 32:
            return self.log("Pick a station first")
        self.cmds.set_name(self.client.listener_name)
        self.cmds.send(cid, cmd)

    def _clear_cmds(self):
        cid = self._cmd_station()
        if self.cmds and cid:
            self.cmds.clear(cid)

    def _draw_cmds(self):
        cid = self._cmd_station()
        key = (cid, self.cmds.revision) if self.cmds else ("wait", self.cmds_wait)
        if key == self.cmd_shown:
            return
        self.cmd_shown = key
        t = self.cmd_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not self.cmds:
            t.insert("end", f"Commands: {self.cmds_wait}\n", "dim")
            t.configure(state="disabled")
            return
        rows = self.cmds.transcript(cid) if cid else []
        if not rows:
            t.insert("end", "Send a command to the station: the reply shows here.\n"
                            "Subscribe = a message when it goes on air (while this client is open).\n", "dim")
        for r in rows:
            when = time.strftime("%d/%m %H:%M", time.localtime(r["t"]))
            if r["dir"] == "out":
                st = r.get("state", "")
                t.insert("end", f"{when}  > {r['text']}", "out")
                t.insert("end", f"   {st}\n", "bad" if st.startswith("failed") else "dim")
            else:
                t.insert("end", f"{when}  ", "dim")
                t.insert("end", r["text"] + "\n\n")
        t.see("end")
        t.configure(state="disabled")
        sub = self.cmds.subscribed(cid) if cid else None
        # what the station said to our last subscribe/unsubscribe
        self.cmd_sub.configure(text={True: "✓ Subscribed", False: "Not subscribed"}.get(sub, ""),
                               fg=LCD if sub else DIM)

    def _playing_cid(self):
        # the station we listen to tells its state inside the call: no status request for it
        return {self.client.call_id} if self.client.call_id and self.client.state == "playing" else set()

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
            ("Remove from favourites" if self._is_fav(cid) else "Add to favourites", lambda: self._toggle_fav(cid)),
            ("Forget", self._forget_selected),
        ])

    def _is_fav(self, cid):
        st = self.dir.stations.get(cid)
        return bool(st and st.fav)

    def _toggle_fav(self, cid):
        st = self.dir.stations.get(cid)
        if st:
            st.fav = not st.fav
            self.dir.save()
            self.dir_dirty = True
            self.log(f"{st.name or cid} {'added to' if st.fav else 'removed from'} favourites")

    def _star_clicked(self, ev):
        # one click on the star column: favourite on/off
        if self.tree.identify_region(ev.x, ev.y) == "cell" and self.tree.identify_column(ev.x) == "#1":
            cid = self.tree.identify_row(ev.y)
            if cid:
                self._toggle_fav(cid)

    def _copy_selected(self):
        sel = self.tree.selection()
        if sel:
            self.copied("Call ID", sel[0])

    def _announce(self):
        # our LXMF address, so stations know our key before we message them
        if self.cmds and self.cmds.announce_now():
            self.log(f"Announcing LXMF address {self.cmds.address}")
            self.dir_dirty = True

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

    def next_station(self, pick_random):
        # only stations on air, never the one playing; by name, the list itself reorders as stations are heard
        cur = self.client.call_id or self.v_call.get().strip().lower()
        on = sorted((s for s in list(self.dir.stations.values()) if s.on_air),
                    key=lambda s: ((s.name or "").lower(), s.call_id))
        others = [s for s in on if s.call_id != cur]
        if not others:
            self.flash = ("No other station on air", time.time() + 3)
            self.status.configure(text=self.flash[0], fg=AMBER)
            return
        if pick_random:
            pick = random.choice(others)
        else:
            ids = [s.call_id for s in on]
            pick = on[(ids.index(cur) + 1) % len(on)] if cur in ids else others[0]
        self.v_call.set(pick.call_id)
        if self.tree.exists(pick.call_id):
            self.tree.selection_set(pick.call_id)
            self.tree.see(pick.call_id)
        self.play("Random button" if pick_random else "Next button")

    def toggle_mute(self):
        # quick pause: the call stays up, only the audio out goes silent
        p = self.client.player
        p.muted = not p.muted
        self.b_mute.configure(text=ICONS["unmute"] + "  Unmute" if p.muted else ICONS["mute"] + "  Mute")
        self.log("Muted" if p.muted else "Unmuted")

    def _tune_selected(self, why=""):
        sel = self.tree.selection()
        if sel:
            self.v_call.set(sel[0])
            self.play(why)

    def _tune_clicked(self, ev):
        # the row under the mouse, not the selection: the list may have been redrawn between the clicks
        cid = self.tree.identify_row(ev.y)
        if cid and self.tree.identify_column(ev.x) != "#1":  # the star column only toggles the favourite
            self.tree.selection_set(cid)
            self.v_call.set(cid)
            self.play("double click")

    def _tab_mystats(self, nb):
        self.mystats = lstats.MyStats(os.path.join(HERE, "storage"))
        f = ttk.Frame(nb, padding=6)
        self.ms_tree = ttk.Treeview(f, columns=("what", "today", "all"), show="headings", height=8)
        for c, t, w, a in (("what", "", 150, "w"), ("today", "Today", 100, "e"), ("all", "All time", 150, "e")):
            self.ms_tree.heading(c, text=t)
            self.ms_tree.column(c, width=w, anchor=a, stretch=c == "what")
        self.ms_tree.pack(side="left", fill="both", expand=True)
        self.ms_top = ttk.Treeview(f, columns=("n", "time", "name"), show="headings", height=8)
        for c, t, w, a in (("n", "Listens", 55, "e"), ("time", "Time", 75, "e"), ("name", "Most listened", 200, "w")):
            self.ms_top.heading(c, text=t)
            self.ms_top.column(c, width=w, anchor=a, stretch=c == "name")
        self.ms_top.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.ms_shown = {}
        return f

    def _mystats_refresh(self):
        rows = {f"r{i}": v for i, v in enumerate(self.mystats.rows())}
        rows.update({f"t{i}": v for i, v in enumerate(self.mystats.top())})
        for iid in [k for k in self.ms_shown if k not in rows]:
            (self.ms_tree if iid[0] == "r" else self.ms_top).delete(iid)
            del self.ms_shown[iid]
        for iid, vals in rows.items():
            if self.ms_shown.get(iid) == vals:
                continue  # only the rows that changed
            tree = self.ms_tree if iid[0] == "r" else self.ms_top
            if iid in self.ms_shown:
                tree.item(iid, values=vals)
            else:
                tree.insert("", "end", iid=iid, values=vals)
            self.ms_shown[iid] = vals

    def _save_call(self):
        cid = self.v_call.get().strip().lower()
        added = self.dir.add(cid)
        if added is False:
            self.log("Save: not a Call ID (32 hex characters)")
        elif added is None:
            self.log(f"Save: {self.dir.name_of(cid) or cid} is already in the list")
        else:
            self.log(f"Saved {cid} in the stations list, asking its name and state")
            self.dir.poll(self._playing_cid())

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
                       "autoplay": self.v_auto.get() if hasattr(self, "v_auto") else bool(self.s.get("autoplay")),
                       "nick": self.v_nick.get().strip()[:32]})
        if hasattr(self, "v_reconnect"):
            self.s["reconnect"] = self.client.reconnect = self.v_reconnect.get()
        self.client.set_nick(self.s["nick"])
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
        self.mystats.save()
        try:
            self.client.hangup()
        except Exception:
            pass
        self.root.destroy()
        stop_children()
        os._exit(0)

    def restart(self):
        """Same interpreter and arguments as now (pythonw from the .bat, python from a terminal): the new one waits for the lock."""
        import subprocess
        env = dict(os.environ, RADIO_LXST_RESTART="1")
        try:
            subprocess.Popen([sys.executable] + sys.argv, cwd=os.getcwd(), env=env)
        except Exception as e:
            self.log(f"Restart failed: {e}")
            return
        self.quit()

    # refresh
    def _update(self):
        self.tick += 1
        snap = self.client.snapshot()
        self.snap = snap
        for i in (0, 1):
            lv = snap["levels"][i]
            self.vu[i] = lv if lv > self.vu[i] else self.vu[i] * 0.8
        self._draw_lcd()

        if self.tick % 8 == 0:
            self.dir.poll(self._playing_cid())
            self.mystats.tick(snap, list(self.dir.stations.values()))
            self._mystats_refresh()
        if self.tick % 8 == 0 or self.dir_dirty:
            self.dir_dirty = False
            st = self.dir.stations.get(snap["call_id"])
            if st is None and snap["state"] == "playing" and snap["listeners"] is not None:
                # tuned in by Call ID before hearing its announce: it goes in the list too
                st = self.dir.stations[snap["call_id"]] = rc.Station(snap["call_id"], snap["name"])
                self.dir.save()
            if st and snap["state"] == "playing" and snap["listeners"] is not None:
                # keep the list in step with what the station tells us in the call
                st.name = snap["name"] or st.name
                st.listeners, st.now_playing, st.heard = snap["listeners"], snap["track"], time.time()
                st.on_air, st.slots = True, snap["slots"]
                st.kbps = snap["kb"] or st.kbps
                st.version = snap["v"] or st.version
            gone = snap["state"] == "ended" or (snap["state"] == "failed" and
                                                 snap["message"] in ("station off air", "station offline"))
            if st and gone and self._marked_off != st.call_id:
                # once per hang up: later status answers (station back on air) must not be undone
                self._marked_off = st.call_id
                self.dir.mark_off(st.call_id)
            if not gone:
                self._marked_off = None
            sel = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            playing = snap["call_id"] if snap["state"] == "playing" else None
            for st in sorted(list(self.dir.stations.values()), key=lambda s: (not s.fav, not s.on_air, -s.heard)):
                hops = st.hops
                me = st.call_id == playing  # what we get only on the station we listen to
                rx = ", ".join(x for x in (snap["q"].replace("Audio, ", ""),
                                            f"{snap['ms']} ms" if snap["ms"] else "") if x) if me else ""
                self.tree.insert("", "end", iid=st.call_id, values=(
                    "★" if st.fav else "☆", st.name or "?", "ON" if st.on_air else "off", "✓" if self.cmds and self.cmds.subscribed(st.call_id) else "",
                    st.listeners_text, st.quality, rx, f"{snap['kbps']:.1f}" if me else "", st.now_playing, st.call_id,
                    hops if hops is not None else "", st.version, ago(st.heard)))
            for cid in sel:
                if self.tree.exists(cid):
                    self.tree.selection_add(cid)
            self.cmd_box.configure(values=self._cmd_choices())
            self.cmd_addr.configure(text=f"Commands go from your LXMF address {self.cmds.address}  (click to copy)"
                                    if self.cmds else f"Commands: {self.cmds_wait}",
                                    fg=DIM if self.cmds else AMBER)
            aw = self.cmds.announce_wait() if self.cmds else 0
            self.b_ann.configure(text=f"Announce in {hms(aw)}" if aw else "Announce",
                                 state="normal" if self.cmds and not aw else "disabled")
            if time.time() > self.flash[1]:
                self.status.configure(text=f"Reticulum {'LOST' if rns_lost() else 'ok'}   stations {len(self.dir.stations)}   "
                                           f"client {self.client.identity.hash.hex()}", fg=DIM)

        if self.cmds_go:
            self._make_cmds()
        try:
            while True:
                # a station we subscribed to tells it is on air: shown for a while in the status bar and the log
                cid, text = self.notices.get_nowait()
                self.client.station_back(cid)
                name = self.dir.name_of(cid) or cid
                line = f"{ICONS['radio']} {name}: {' '.join(text.split())}"
                self.log(line)
                self.flash = (line[:120], time.time() + 30)
                self.status.configure(text=self.flash[0], fg=AMBER)
                self.dir_dirty = True
        except queue.Empty:
            pass
        self._draw_cmds()
        if bool(self.cmds) != self.cmd_btns_on:
            # grey until our LXMF address is up (its announce stamp made)
            self.cmd_btns_on = bool(self.cmds)
            for b in self.cmd_btns:
                b.configure(state="normal" if self.cmd_btns_on else "disabled")
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
        if self.client.player.muted:
            c.create_text(x0, 74, text="MUTE", anchor="w", fill=AMBER, font=LCD_FONT)
        else:
            c.create_text(x0, 74, text=f"VOL {self.client.player.volume_db:+.1f}", anchor="w", fill=col, font=LCD_FONT)
        od = self.client.player.out_db
        c.create_text(x0, 89, text=f"OUT {od:5.1f}" if od is not None else "OUT   --", anchor="w", fill=col, font=LCD_FONT)

        msg = s["message"] or ""
        lines = [
            f"CALL ID  {s['call_id'] or '-'}",
            f"STATE    {state.upper()}  {msg[:30]}" + (f"   LISTENERS {s['listeners']}" + (f"/{s['slots']}" if s['slots'] else "")
                                             if s['listeners'] is not None else ""),
            f"TIME     {hms(s['time'])}   {s['kbps']:.1f} kbit/s   HOPS {s['hops'] if s['hops'] is not None else '-'}"
            f"   DROPS {s['underruns']}" + (f" CARD {s['card_gaps']}" if s['card_gaps'] else "")
            + (f" FULL {s['overflows']}" if s.get('overflows') else ""),
            f"TRACK    {s['track'][:46] or '-'}",
        ]
        for i, t in enumerate(lines):
            c.create_text(12, 44 + i * 18, text=t, anchor="w", fill=col, font=LCD_FONT, tags=("cid",) if i == 0 else ())
        draw_progress(c, 12, w - 192, 117, s["progress"], active, s["live"], self.tick // 2)
        draw_vu(c, w, self.vu, active)
        if self.stamping:
            on = (self.tick // 5) % 2 == 0
            c.create_rectangle(w - 180, 88, w - 108, 106, outline=AMBER, fill=AMBER if on else LCD_BG)
            c.create_text(w - 144, 97, text="STAMP", fill=LCD_BG if on else AMBER, font=LCD_BOLD)


def main():
    app_id("client")  # before any window, also the "already running" box
    lock = rc.lock_storage(os.path.join(HERE, "storage"))
    if lock is None and os.environ.pop("RADIO_LXST_RESTART", None):
        for _ in range(100):  # the old client is still closing
            time.sleep(0.1)
            lock = rc.lock_storage(os.path.join(HERE, "storage"))
            if lock:
                break
    if lock is None:
        print(rc.ALREADY_OPEN)
        r = tk.Tk()
        r.withdraw()
        from tkinter import messagebox
        messagebox.showinfo("radio-lxst client", rc.ALREADY_OPEN)
        r.destroy()
        sys.exit(1)
    root = tk.Tk()
    set_icon(root, "client")
    try:
        App(root)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(f"radio-lxst client could not start: {e}")
    root.mainloop()


if __name__ == "__main__":
    main()
