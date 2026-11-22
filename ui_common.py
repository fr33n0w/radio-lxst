# Shared look for the radio-lxst server and client windows.

import tkinter as tk
from tkinter import ttk

BG = "#202028"
PANEL = "#2b2b35"
EDGE = "#4b4b5c"
TEXT = "#d6d6e0"
DIM = "#8a8a9a"
LCD_BG = "#050805"
LCD = "#2cff4a"
LCD_DIM = "#145a1e"
RED = "#ff3b30"
AMBER = "#ffb020"
# named Tk fonts, created by apply_style() from what the system has (Windows, Linux, macOS)
UI_FONT = "RadioUi"
TITLE_FONT = "RadioTitle"
LCD_FONT = "RadioLcd"
LCD_BIG = "RadioLcdBig"
LCD_BOLD = "RadioLcdBold"
LCD_SMALL = "RadioLcdSmall"
LOG_FONT = "RadioLog"
MONO_FAMILIES = ("Consolas", "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono", "Menlo", "Courier New")
UI_FAMILIES = ("Tahoma", "Segoe UI", "DejaVu Sans", "Noto Sans", "Liberation Sans", "Helvetica")
VU_SEGS = 24
_FONTS = []


def _make_fonts(root):
    from tkinter import font as tkfont
    have = set(tkfont.families(root))
    mono = next((f for f in MONO_FAMILIES if f in have), tkfont.nametofont("TkFixedFont").actual("family"))
    ui = next((f for f in UI_FAMILIES if f in have), tkfont.nametofont("TkDefaultFont").actual("family"))
    for name, fam, size, weight in ((UI_FONT, ui, 9, "normal"), (TITLE_FONT, ui, 11, "bold"),
                                    (LCD_FONT, mono, 10, "normal"), (LCD_BIG, mono, 15, "bold"),
                                    (LCD_BOLD, mono, 10, "bold"), (LCD_SMALL, mono, 8, "normal"),
                                    (LOG_FONT, mono, 9, "normal")):
        try:
            # keep a reference: Tk deletes a named font when its Python object is collected
            _FONTS.append(tkfont.Font(root, name=name, family=fam, size=size, weight=weight, exists=False))
        except tk.TclError:
            tkfont.nametofont(name).configure(family=fam, size=size, weight=weight)


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def apply_style(root):
    _make_fonts(root)
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL, font=UI_FONT,
                 bordercolor=EDGE, lightcolor=PANEL, darkcolor=BG, troughcolor=BG)
    st.configure("TNotebook", background=BG, borderwidth=0)
    st.configure("TNotebook.Tab", background=PANEL, foreground=DIM, padding=(12, 4))
    st.map("TNotebook.Tab", background=[("selected", EDGE)], foreground=[("selected", LCD)])
    st.configure("TFrame", background=PANEL)
    st.configure("TLabel", background=PANEL, foreground=TEXT)
    st.configure("Dim.TLabel", background=PANEL, foreground=DIM)
    st.configure("TCheckbutton", background=PANEL, foreground=TEXT)
    st.configure("TRadiobutton", background=PANEL, foreground=TEXT)
    st.map("TCheckbutton", background=[("active", PANEL)])
    st.configure("Bar.TCheckbutton", background=BG, foreground=TEXT)
    st.map("Bar.TCheckbutton", background=[("active", BG)])
    st.map("TRadiobutton", background=[("active", PANEL)])
    st.configure("TEntry", fieldbackground=LCD_BG, foreground=LCD, insertcolor=LCD)
    st.configure("TSpinbox", fieldbackground=LCD_BG, foreground=LCD, arrowcolor=TEXT)
    st.configure("TCombobox", fieldbackground=LCD_BG, foreground=LCD, arrowcolor=TEXT,
                 selectbackground=LCD_BG, selectforeground=LCD)
    st.map("TCombobox", fieldbackground=[("readonly", LCD_BG)], foreground=[("readonly", LCD)])
    root.option_add("*TCombobox*Listbox.background", LCD_BG)
    root.option_add("*TCombobox*Listbox.foreground", LCD)
    st.configure("TButton", background=EDGE, foreground=TEXT, padding=(10, 3))
    st.map("TButton", background=[("active", "#5c5c70"), ("disabled", PANEL)],
           foreground=[("disabled", DIM)])
    st.configure("TMenubutton", background=EDGE, foreground=TEXT, arrowcolor=TEXT, padding=(10, 3),
                 anchor="center")
    st.map("TMenubutton", background=[("active", "#5c5c70")])
    st.configure("Air.TButton", foreground=LCD)
    st.configure("Stop.TButton", foreground=RED)
    st.configure("Treeview", background=LCD_BG, fieldbackground=LCD_BG, foreground=LCD,
                 rowheight=20, font=LCD_FONT, borderwidth=0)
    st.configure("Treeview.Heading", background=EDGE, foreground=TEXT, font=UI_FONT)
    st.map("Treeview", background=[("selected", LCD_DIM)])


def volume_scale(parent, var, length=220):
    return tk.Scale(parent, from_=-24, to=12, resolution=0.5, orient="horizontal", variable=var,
                    showvalue=0, length=length, bg=BG, troughcolor=LCD_BG, activebackground=LCD,
                    highlightthickness=0, bd=0, sliderlength=14, width=12)


def _vu_y(db):
    # same curve as the lit segments: level ** 0.6
    return 100 - (10 ** (db / 20)) ** 0.6 * VU_SEGS * 3 + 1


def draw_vu(c, w, levels, on, ceiling_db=None):
    """Two segmented L/R meters at the right edge of an LCD canvas, dB scale in between."""
    x0 = w - 96
    for ch, lbl in enumerate(("L", "R")):
        x = x0 + ch * 44
        c.create_text(x + 10, 110, text=lbl, fill=LCD_DIM, font=LCD_SMALL)
        lit = int(min(1.0, levels[ch] ** 0.6) * VU_SEGS) if on else 0
        for sgi in range(VU_SEGS):
            y = 100 - sgi * 3
            col = RED if sgi >= VU_SEGS - 3 else AMBER if sgi >= VU_SEGS - 8 else LCD
            c.create_rectangle(x, y - 2, x + 20, y, outline="", fill=col if sgi < lit else "#0d1a0d")
    for db in (0, -6, -12, -24):
        c.create_text(x0 + 32, _vu_y(db), text=str(db), fill=LCD_DIM, font=LCD_SMALL)
    if ceiling_db is not None:
        y = _vu_y(ceiling_db)
        c.create_line(x0 - 3, y, x0 + 67, y, fill=AMBER)


def _mss(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def draw_progress(c, x0, x1, y, progress, on, live="", step=0, mixing=False):
    """Player style bar of the track: elapsed, bar, length. progress = (pos, length) or None.
    No length but a live text: the text scrolls inside the box (step = animation counter).
    mixing: drawn in amber (the caller blinks it while two tracks are mixed)."""
    if not progress and live and on:
        c.create_rectangle(x0, y - 7, x1, y + 7, outline=LCD_DIM)
        from tkinter import font as tkfont
        cw = max(1, tkfont.nametofont(LCD_SMALL).measure("0"))
        n = max(1, (x1 - x0 - 8) // cw)
        loop = live + "   ·   "
        loop = loop * (n // len(loop) + 2)
        off = step % len(live + "   ·   ")
        c.create_text(x0 + 4, y, text=loop[off:off + n], anchor="w", fill=LCD, font=LCD_SMALL)
        return
    col = AMBER if mixing and on else (LCD if on and progress else LCD_DIM)
    pos, dur = progress if progress else (0, 0)
    # right: time left, then length
    left, right = (_mss(pos), f"-{_mss(max(0, dur - pos))} / {_mss(dur)}") if progress else ("-:--", "-:--")
    lt = c.create_text(x0, y, text=left, anchor="w", fill=col, font=LCD_SMALL)
    rt = c.create_text(x1, y, text=right, anchor="e", fill=col, font=LCD_SMALL)
    bx0, bx1 = max(x0 + 40, c.bbox(lt)[2] + 8), min(x1 - 40, c.bbox(rt)[0] - 8)
    c.create_rectangle(bx0, y - 3, bx1, y + 3, outline=AMBER if mixing and on else LCD_DIM)
    if progress and dur > 0:
        c.create_rectangle(bx0, y - 3, bx0 + (bx1 - bx0) * min(1.0, pos / dur), y + 3, outline="", fill=col)
    return bx0, bx1


def draw_badge(c, w, text, on, blink, color=RED):
    """ON AIR / PLAYING style box next to the meters."""
    if on:
        c.create_rectangle(w - 180, 8, w - 108, 28, outline=color, fill=color if blink else LCD_BG)
        c.create_text(w - 144, 18, text=text, fill=LCD_BG if blink else color, font=LCD_BOLD)
    else:
        c.create_rectangle(w - 180, 8, w - 108, 28, outline=LCD_DIM)
        c.create_text(w - 144, 18, text=text, fill=LCD_DIM, font=LCD_BOLD)


def copy_text(root, text):
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update_idletasks()


def context_menu(root, event, items):
    """Dark right-click menu; items = [(label, callback) or None for a separator]."""
    m = tk.Menu(root, tearoff=0, bg=PANEL, fg=TEXT, activebackground=EDGE, activeforeground=LCD,
                bd=1, relief="flat", font=UI_FONT)
    last_sep = True
    for it in items:
        if it is None:
            if not last_sep:
                m.add_separator()
            last_sep = True
        else:
            m.add_command(label=it[0], command=it[1])
            last_sep = False
    global _MENU
    if _MENU is not None:  # one menu at a time
        try:
            _MENU.unpost()
            _MENU.destroy()
        except tk.TclError:
            pass
    _MENU = m
    m.bind("<Escape>", lambda e: m.unpost())
    if root.tk.call("tk", "windowingsystem") == "win32":
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()
    else:
        # X11: tk_popup returns at once, the grab must stay or a click outside never closes the menu
        m.tk_popup(event.x_root, event.y_root)
        root.after(400, _watch_menu, root, m, 0)


def _watch_menu(root, m, misses):
    # X11 menus are free windows: close it when the app loses focus (other desktop, other window)
    try:
        if not m.winfo_ismapped():
            return
        try:
            focused = root.focus_get() is not None
        except (KeyError, tk.TclError):
            focused = True
        misses = 0 if focused else misses + 1
        if misses >= 2:
            m.unpost()
            m.grab_release()
            return
        root.after(250, _watch_menu, root, m, misses)
    except tk.TclError:
        pass


_MENU = None


def update_banner(root, before, current, log=None):
    """Hidden bar shown above `before` when GitHub has a newer version; click opens the repo."""
    import queue
    import webbrowser
    import update_check
    found = queue.Queue()
    bar = tk.Label(root, text="", bg=PANEL, fg=AMBER, font=UI_FONT, anchor="w", cursor="hand2", padx=8, pady=3)
    bar.bind("<Button-1>", lambda ev: webbrowser.open(update_check.REPO_URL))

    def poll():
        try:
            v = found.get_nowait()
            bar.configure(text=f"radio-lxst {v} is out (you have {current}) - click to open {update_check.REPO_URL}")
            if not bar.winfo_ismapped():
                bar.pack(fill="x", padx=8, pady=(0, 4), before=before)
            if log:
                log(f"New version {v} on GitHub: {update_check.REPO_URL}")
        except queue.Empty:
            pass
        root.after(2000, poll)
    update_check.watch(current, found.put)
    root.after(2000, poll)
    return bar


def rns_lost():
    import rns_watch
    return rns_watch.lost()


def rns_watch(root, before):
    """Red bar above `before` while the shared RNS instance is gone, green for 5 s when it is back."""
    import time
    import rns_watch as rw
    rw.start()
    bar = tk.Label(root, text="", bg=RED, fg="#ffffff", font=UI_FONT, anchor="w", padx=8, pady=3)
    state = {"lost": False, "since": 0.0, "hide": 0.0}

    def show():
        if not bar.winfo_ismapped():
            bar.pack(fill="x", padx=8, pady=(0, 4), before=before)

    def poll():
        lost = rw.lost()
        now = time.time()
        if lost and not state["lost"]:
            state["since"] = now
        elif not lost and state["lost"]:
            bar.configure(bg="#1f8a3a", text=f"Reticulum shared instance back after {hms(now - state['since'])}"
                                               " - network connection ready!")
            state["hide"] = now + 5
            show()
        if lost:
            bar.configure(bg=RED, text=f"Reticulum shared instance lost {hms(now - state['since'])} ago"
                                       " - waiting to reconnect... (is the rnsd instance running?)")
            state["hide"] = 0.0
            show()
        elif state["hide"] and now >= state["hide"]:
            state["hide"] = 0.0
            bar.pack_forget()
        state["lost"] = lost
        root.after(1000, poll)
    root.after(1000, poll)
    return bar


def about_tab(nb, app, current):
    """About tab: version, repo link, update check each time the tab is shown."""
    import threading
    import webbrowser
    import update_check
    f = ttk.Frame(nb, padding=16)
    tk.Label(f, text=app, bg=PANEL, fg=LCD, font=TITLE_FONT).pack(pady=(8, 2))
    tk.Label(f, text=f"version {current}", bg=PANEL, fg=TEXT, font=UI_FONT).pack()
    tk.Label(f, text="internet radio over Reticulum (LXST)", bg=PANEL, fg=DIM, font=UI_FONT).pack(pady=(2, 12))
    link = tk.Label(f, text=update_check.REPO_URL, bg=PANEL, fg=AMBER, font=UI_FONT, cursor="hand2")
    link.pack()
    link.bind("<Button-1>", lambda ev: webbrowser.open(update_check.REPO_URL))
    status = tk.Label(f, text="", bg=PANEL, fg=DIM, font=UI_FONT)
    status.pack(pady=(12, 0))
    res = []

    def show(v):
        if v is None:
            status.configure(text="update check failed (no internet?)", fg=DIM)
        elif update_check.newer(v, current):
            status.configure(text=f"new version {v} available - click the link above", fg=AMBER)
        else:
            status.configure(text=f"up to date (GitHub has {v})", fg=LCD)

    def poll():
        if res:
            show(res.pop())
        else:
            f.after(300, poll)

    def check(ev=None):
        if status.cget("text").startswith("checking"):
            return
        status.configure(text="checking GitHub for updates...", fg=DIM)
        threading.Thread(target=lambda: res.append(update_check.latest_version()), daemon=True).start()
        f.after(300, poll)
    f.bind("<Map>", check)
    return f


def hash_of(name, identity_hash_hex):
    """Destination hash (hex) of an aspect like 'lxmf.delivery' for an identity hash."""
    import RNS
    return RNS.Destination.hash_from_name_and_identity(name, bytes.fromhex(identity_hash_hex)).hex()


class LogBox:
    def __init__(self, parent):
        self.text = tk.Text(parent, bg=LCD_BG, fg=LCD, font=LOG_FONT, relief="flat", wrap="word",
                            insertbackground=LCD, height=10)
        sb = ttk.Scrollbar(parent, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)

    def add(self, line):
        t = self.text
        t.configure(state="normal")
        t.insert("end", line.rstrip() + "\n")
        if int(t.index("end-1c").split(".")[0]) > 600:
            t.delete("1.0", "100.0")
        t.see("end")
        t.configure(state="disabled")
