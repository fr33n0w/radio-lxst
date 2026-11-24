# radio-lxst station statistics: this run and all time, kept in <storage>/stats.json (counts only, no names).

import os
import json
import time
import hashlib
import secrets
import threading

STATS_FILE = "stats.json"
SAVE_EVERY = 60  # s
COUNTS = ("listens", "listen_s", "client_listens", "phone_listens", "refused", "tracks", "air_s", "run_s",
          "lost", "status_requests", "lxmf_msgs", "subscribed", "unsubscribed", "starts")
TOP_TRACKS = 10


def dur(s):
    """Short duration: 45s, 12m 05s, 3h 07m, 2d 04h."""
    s = int(max(0, s))
    d, h, m = s // 86400, s // 3600 % 24, s // 60 % 60
    if d:
        return f"{d}d {h:02d}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s % 60:02d}s"
    return f"{s}s"


def size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.2f} TB"


def when(t, day=False):
    if not t:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M" if day else "%H:%M", time.localtime(t))


def load(storage):
    """The saved all-time totals (empty dict if none)."""
    try:
        with open(os.path.join(storage, STATS_FILE), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class Stats:
    def __init__(self, radio):
        self.radio = radio
        self.path = os.path.join(radio.storage, STATS_FILE)
        self.lock = threading.Lock()
        b = load(radio.storage)
        self.base = {k: b.get(k, 0) for k in COUNTS}
        self.base_since = b.get("since") or time.time()
        self.base_peak, self.base_peak_at = b.get("peak", 0), b.get("peak_at", 0)
        self.base_longest = b.get("longest_s", 0)
        self.base_bytes = b.get("bytes", 0)
        self.salt = b.get("salt") or secrets.token_hex(16)  # listeners are counted by a salted hash, never named
        self.base_unique = set(b.get("unique", []))
        self.s = {k: 0 for k in COUNTS}
        self.s["starts"] = 1
        self.unique = set()
        self.peak, self.peak_at, self.longest = 0, 0, 0
        self.started = time.time()
        threading.Thread(target=self._save_loop, daemon=True).start()

    def inc(self, key, n=1):
        with self.lock:
            self.s[key] += n

    def _key(self, identity):
        return hashlib.sha256((self.salt + identity.hash.hex()).encode()).hexdigest()[:32]

    def listen(self, lst, n_now):
        with self.lock:
            self.s["listens"] += 1
            if lst.identity:
                self.unique.add(self._key(lst.identity))
            if n_now > self.peak:
                self.peak, self.peak_at = n_now, time.time()

    def left(self, lst):
        d = time.time() - lst.since
        with self.lock:
            self.s["listen_s"] += d
            self.s["client_listens" if lst.client else "phone_listens"] += 1
            self.s["lost"] += lst.failed
            self.longest = max(self.longest, d)

    def track(self, name=None):
        self.inc("tracks")

    def off_air(self, since):
        if since:
            self.inc("air_s", time.time() - since)

    def view(self):
        """{'run': {...}, 'all': {...}}: this run and all time, listeners on now included."""
        r, now = self.radio, time.time()
        with r.lock:
            on = [l for l in r.listeners.values() if l.state == "listening"]
        with self.lock:
            run = dict(self.s)
            unique = set(self.unique)
            peak, peak_at, longest = self.peak, self.peak_at, self.longest
        run["run_s"] = now - self.started
        run["air_s"] += now - r.on_air_since if r.on_air and r.on_air_since else 0
        for l in on:
            d = now - l.since
            run["listen_s"] += d
            run["client_listens" if l.client else "phone_listens"] += 1
            run["lost"] += l.failed
            longest = max(longest, d)
        run.update(bytes=r.total_bytes, unique=len(unique), peak=peak, peak_at=peak_at, longest_s=longest,
                   since=self.started, now_air=now - r.on_air_since if r.on_air and r.on_air_since else 0,
                   listening=len(on))
        tot = {k: self.base[k] + run[k] for k in COUNTS}
        tot.update(bytes=self.base_bytes + r.total_bytes, unique=len(self.base_unique | unique),
                   longest_s=max(self.base_longest, longest), since=self.base_since)
        tot["peak"], tot["peak_at"] = ((peak, peak_at) if peak > self.base_peak else (self.base_peak, self.base_peak_at))
        subs = r.lx.subs if r.lx else None
        run["subscribers"] = tot["subscribers"] = len(subs) if subs is not None else None
        return {"run": run, "all": tot, "unique_keys": self.base_unique | unique}

    def save(self):
        v = self.view()
        a = v["all"]
        d = {k: a[k] for k in COUNTS}
        d.update(bytes=a["bytes"], since=a["since"], peak=a["peak"], peak_at=a["peak_at"], longest_s=a["longest_s"],
                 salt=self.salt, unique=sorted(v["unique_keys"]), saved=time.time())
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _save_loop(self):
        while True:
            time.sleep(SAVE_EVERY)
            self.save()


def _avg(v):
    return v["listen_s"] / v["listens"] if v["listens"] else 0


def rows(run, tot):
    """(label, this run, all time) for every line; run None = station not running."""
    def both(key, fmt=str):
        return fmt(run[key]) if run else "-", fmt(tot[key])
    peak_r = f"{run['peak']} at {when(run['peak_at'])}" if run and run["peak"] else (str(run["peak"]) if run else "-")
    peak_t = f"{tot['peak']} on {when(tot['peak_at'], True)}" if tot["peak"] else "0"
    out = [
        ("Station running", *both("run_s", dur)),
        ("On air", *both("air_s", dur)),
        ("On air now", dur(run["now_air"]) if run and run["now_air"] else "off air" if run else "-", ""),
        ("Listening now", str(run["listening"]) if run else "-", ""),
        ("Listens (calls)", *both("listens")),
        ("Different listeners", *both("unique")),
        ("Peak listeners", peak_r, peak_t),
        ("Listening time", *both("listen_s", dur)),
        ("Average listen", dur(_avg(run)) if run else "-", dur(_avg(tot))),
        ("Longest listen", *both("longest_s", dur)),
        ("radio-lxst clients", *both("client_listens")),
        ("Phones (Sideband, Columba...)", *both("phone_listens")),
        ("Refused (station full)", *both("refused")),
        ("Tracks played", *both("tracks")),
        ("Data sent", *both("bytes", size)),
        ("Packets lost", *both("lost")),
        ("Status requests", *both("status_requests")),
        ("LXMF messages", *both("lxmf_msgs")),
        ("Subscribed", *both("subscribed")),
        ("Unsubscribed", *both("unsubscribed")),
        ("Subscribers now", "", "-" if tot.get("subscribers") is None else str(tot["subscribers"])),
        ("Station starts", "", str(tot["starts"])),
        ("Counting since", when(run["since"], True) if run else "-", when(tot["since"], True)),
    ]
    return out


def text(run, tot):
    """Aligned table for the terminal."""
    rs = rows(run, tot)
    w0 = max(len(r[0]) for r in rs)
    w1 = max(len("This run"), *(len(r[1]) for r in rs))
    lines = [f"{'':{w0}}  {'This run':>{w1}}  All time"]
    lines += [f"{a:{w0}}  {b:>{w1}}  {c}" for a, b, c in rs]
    return lines


def lxmf_text(v):
    """Short plain lines for the LXMF status reply."""
    run, tot = v["run"], v["all"]
    subs = tot.get("subscribers")
    out = ["Stats (this run / all time):",
           f"Listens: {run['listens']} / {tot['listens']}",
           f"Listeners: {run['unique']} / {tot['unique']}",
           f"Peak: {run['peak']} / {tot['peak']}",
           f"Listening time: {dur(run['listen_s'])} / {dur(tot['listen_s'])}",
           f"Tracks played: {run['tracks']} / {tot['tracks']}",
           f"On air: {dur(run['air_s'])} / {dur(tot['air_s'])}",
           f"Data sent: {size(run['bytes'])} / {size(tot['bytes'])}"]
    if subs is not None:
        out.append(f"Subscribers: {subs} (+{run['subscribed']} -{run['unsubscribed']} this run)")
    out.append(f"Since {when(tot['since'], True)}")
    return "\n".join(out)


def top_tracks(storage, n=TOP_TRACKS):
    """[(times, last time, file name)] most played first, from played.json."""
    try:
        with open(os.path.join(storage, "played.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for path, v in d.items():
        last, times = (v if isinstance(v, list) else (v, 1))[:2]
        out.append((int(times), last, os.path.splitext(os.path.basename(path))[0]))
    out.sort(key=lambda x: (-x[0], -x[1]))
    return out[:n]


def offline_text(storage, running=False):
    """--stats without a control channel: the saved totals (a running GUI station saves them every minute)."""
    b = load(storage)
    if not b:
        return ["no statistics yet in " + storage]
    tot = {k: b.get(k, 0) for k in COUNTS}
    tot.update(bytes=b.get("bytes", 0), unique=len(b.get("unique", [])), peak=b.get("peak", 0),
               peak_at=b.get("peak_at", 0), longest_s=b.get("longest_s", 0), since=b.get("since", 0))
    saved = when(b.get("saved"), True)
    lines = [f"GUI station running, all-time totals as saved at {saved}" if running
             else f"station not running, totals saved {saved}"]
    rs = [r for r in rows(None, tot) if r[0] not in ("On air now", "Listening now", "Subscribers now")]
    w0 = max(len(r[0]) for r in rs)
    lines += [f"{a:{w0}}  {c}" for a, _, c in rs]
    return lines
