# radio-lxst client statistics: today and all time, kept in <storage>/my_stats.json (no track titles).

import os
import json
import time

from station_stats import dur, when

STATS_FILE = "my_stats.json"
SAVE_EVERY = 60  # s
MAX_STEP = 5     # s: a longer gap between two ticks (PC asleep) is not counted as listening
COUNTS = ("listen_s", "sessions", "tracks", "dropouts", "longest_s")
TOP = 10
ENDED = ("idle", "failed", "ended")


def _today():
    return time.strftime("%Y-%m-%d")


def _midnight():
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _blank():
    d = {k: 0 for k in COUNTS}
    d.update(listened=[], seen=[])
    return d


class MyStats:
    """Fed by tick() with the client snapshot about once a second; saved every minute and by save()."""

    def __init__(self, storage):
        self.path = os.path.join(storage, STATS_FILE)
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            d = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            d = {}
        self.all = {k: d.get(k, 0) for k in COUNTS}
        self.since = d.get("since") or time.time()
        self.stations = d.get("stations") or {}  # call_id -> {"name", "times", "listen_s"}
        self.seen = set(d.get("seen") or [])
        self.day = d.get("day") or _today()
        self.today = _blank()
        self.today.update({k: v for k, v in (d.get("today") or {}).items() if k in self.today})
        self.today["listened"], self.today["seen"] = set(self.today["listened"]), set(self.today["seen"])
        self._rollover()
        self.session = None  # call_id listened now
        self.session_s = 0.0
        self.track = ""
        self.drops = None
        self.last = None
        self.saved = time.monotonic()

    def _rollover(self):
        if self.day != _today():
            self.day, self.today = _today(), _blank()
            self.today["listened"], self.today["seen"] = set(), set()

    def _add(self, key, n=1):
        self.all[key] += n
        self.today[key] += n

    def tick(self, snap, stations=()):
        now = time.monotonic()
        dt = min(MAX_STEP, now - self.last) if self.last else 0.0
        self.last = now
        self._rollover()
        cid, state = snap.get("call_id") or "", snap.get("state")
        if self.session and (state in ENDED or cid != self.session):
            self.session = None
        if state == "playing" and cid and self.session is None:
            self.session, self.session_s, self.track = cid, 0.0, ""
            self._add("sessions")
            self.today["listened"].add(cid)
            st = self.stations.setdefault(cid, {"name": "", "times": 0, "listen_s": 0})
            st["times"] += 1
        if self.session:
            st = self.stations[self.session]
            st["name"] = snap.get("name") or st["name"]
            if state == "playing" and snap.get("player") == "playing":
                self._add("listen_s", dt)
                st["listen_s"] += dt
                self.session_s += dt
                self.all["longest_s"] = max(self.all["longest_s"], self.session_s)
                self.today["longest_s"] = max(self.today["longest_s"], self.session_s)
                track = snap.get("track") or ""
                if track and track != self.track:
                    self.track = track
                    self._add("tracks")
        drops = (snap.get("underruns") or 0) + (snap.get("card_gaps") or 0) + (snap.get("overflows") or 0)
        if self.drops is not None and drops > self.drops:
            self._add("dropouts", drops - self.drops)
        self.drops = drops
        day0 = _midnight()
        for s in stations:
            self.seen.add(s.call_id)
            if s.heard >= day0:
                self.today["seen"].add(s.call_id)
        if now - self.saved >= SAVE_EVERY:
            self.save()

    def rows(self):
        """(label, today, all time)."""
        t, a = self.today, self.all
        return [
            ("Listening time", dur(t["listen_s"]), dur(a["listen_s"])),
            ("Listens", str(t["sessions"]), str(a["sessions"])),
            ("Longest listen", dur(t["longest_s"]), dur(a["longest_s"])),
            ("Tracks heard", str(t["tracks"]), str(a["tracks"])),
            ("Stations listened", str(len(t["listened"])), str(len(self.stations))),
            ("Stations seen", str(len(t["seen"])), str(len(self.seen))),
            ("Audio dropouts", str(t["dropouts"]), str(a["dropouts"])),
            ("Counting since", "", when(self.since, True)),
        ]

    def top(self, n=TOP):
        """[(times, listening time, name)] most listened first."""
        out = [(v["times"], v["listen_s"], v["name"] or cid) for cid, v in self.stations.items()]
        out.sort(key=lambda x: (-x[1], -x[0]))
        return [(t, dur(s), name) for t, s, name in out[:n]]

    def save(self):
        self.saved = time.monotonic()
        today = dict(self.today, listened=sorted(self.today["listened"]), seen=sorted(self.today["seen"]))
        d = dict(self.all, since=self.since, stations=self.stations, seen=sorted(self.seen), day=self.day,
                 today=today, saved=time.time())
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.path)
        except OSError:
            pass
