# radio-lxst headless control: the GUI's actions as text commands, over a local channel only.
# Linux/Mac: Unix socket ctl.sock in the identity folder (owner only, nc -U works).
# Windows (or a path too long for a socket): 127.0.0.1 + a token in ctl.json, readable by the owner.

import os
import sys
import json
import time
import shlex
import socket
import select
import secrets
import threading
from collections import deque

import RNS
import radio_lxst as rl
import bpm as bpmdet
import station_lxmf as sl
import station_stats

HERE = os.path.dirname(os.path.abspath(__file__))
CTL_SOCK = "ctl.sock"
CTL_INFO = "ctl.json"
LOG_KEEP = 1000
BEAT_CHOICES = (8, 16, 32, 64, 128)
# station settings, same keys as the GUI settings.json
DEFAULTS = {
    "name": "radio-lxst", "mode": "url", "url": "", "folder": "", "shuffle": False, "input_device": "",
    "frame_ms": 60, "announce_on_start": True, "announce_every_min": 60,
    "identity": os.path.join(HERE, "storage", "identity"), "rnsconfig": "", "auto_start": False,
    "gain_db": 0.0, "limiter_on": True, "limiter_ceiling_db": -2.0, "monitor_device": "",
    "lxmf_on": True, "lxmf_every_h": 3, "slots": 0,
    "fade_on": False, "fade_s": 3, "mix_on": False, "mix_s": 5, "trim_silence": False, "beat_on": False, "beat_n": 32,
    "loop_out": False, "loop_in": False, "loop_n": 8, "loop_times": 4,
    "confirm_play": False, "phone_60": True, "norm_on": False, "norm_lufs": -14,
}
SORTS = {"name": "title", "bpm": "BPM", "len": "length", "plays": "times played", "fresh": "not played yet first",
         "last": "last played", "random": "random"}

HELP = """\
Station
  status                     what is on air, listeners, settings
  onair | start              go on air (on air: switch to the source set)
  stop                       go off air, listeners are cut off
  announce                   announce now (one every 10 min at most)
  id                         call ID and addresses
  listeners                  who is listening
  stats                      statistics, this run and all time, most played tracks
  log [N]                    last N log lines (default 30)
  follow                     new log lines as they come (Enter to stop)
  watch [S]                  status every S seconds (default 5, Enter to stop)
  save FILE                  write all settings to FILE (start later with --settings FILE)
  shutdown                   stop the station and quit the program
  quit                       close this connection, the station keeps going
Source
  source                     the source set
  source url URL | source folder PATH | source input DEVICE
  inputs                     input devices
  switch                     put the source set on air now, listeners stay
  next                       next track (folder)
  seek M:SS                  jump in the track on air (folder)
Folder options (from the next track)
  shuffle on|off   fade on|off|SEC   mix on|off|SEC   trim on|off
  beatmix on|off|BEATS (8 16 32 64 128)   loop out on|off   loop in on|off
  loop beats 4|8|16   loop times 4|8|16
Playlist (TRACKS = numbers from list, 3-7, 2,5,9 or part of a name)
  list [FROM [COUNT]]        tracks, > = on air, ~ = mixing, * = played since start
  find TEXT                  tracks with TEXT in the name
  play TRACK                 play now (off air: goes on air with the folder)
  queue TRACKS               play after the track on air
  remove TRACKS              out of the list, files stay on disk
  clear yes                  empty the list (files stay, add the folder to get it back)
  move TRACKS up|down [N] | move TRACK to POS
  add PATH                   add a file or a folder from anywhere
  added                      files and folders added
  unadd N|PATH               forget an added file or folder
  unplayed TRACKS            clear the played mark
  reshuffle                  shuffle the list now
  sort KEY [desc]            KEY: name bpm len plays fresh last random
  bpm TRACKS                 analyse the BPM again
  rescan                     read the folder again
Output
  volume DB (-24..12)   limiter on|off   ceiling DB (-12..0)   normalize on|off|LUFS (-18..-8)
  name TEXT   slots N (0 = no limit)   quality steady|high (live)   phone60 on|off
  announce start on|off   announce every MIN (0 = off, min 20)   autostart on|off (GUI)
  lxmf on|off   lxmf every HOURS
  monitor on|off   monitor device NAME|default   outputs
Replies (LXMF autoresponder)
  replies                    keys, + = custom
  reply KEY                  show it
  reply KEY TEXT             set it (\\n = new line), reply KEY default = back to default"""


READ_ONLY = {"help", "status", "stats", "id", "listeners", "log", "list", "find", "added", "inputs", "outputs", "replies"}
READ_ONLY_BARE = {"source", "volume", "name"}  # read only without arguments


class CtlError(Exception):
    pass


def split_line(line):
    """Words, "quoted words" kept together; no backslash escapes (Windows paths)."""
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split = True
    lex.escape = ""
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError as e:
        raise CtlError(f"cannot read the command: {e}")


def _onoff(v):
    v = (v or "").lower()
    if v in ("on", "yes", "1", "true"):
        return True
    if v in ("off", "no", "0", "false"):
        return False
    raise CtlError("say on or off")


def _num(v, lo, hi, kind=float):
    try:
        x = kind(v)
    except (TypeError, ValueError):
        raise CtlError(f"not a number: {v}")
    if not lo <= x <= hi:
        raise CtlError(f"{v} is out of range {lo}..{hi}")
    return x


def _secs(v):
    try:
        parts = [float(p) for p in v.split(":")]
    except ValueError:
        raise CtlError(f"time as M:SS or seconds, not {v}")
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def _mmss(s):
    s = int(s or 0)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class Station:
    """What the GUI keeps in its widgets, for a station without GUI: settings, playlists, BPM cache."""

    def __init__(self, storage, s, settings_file=None):
        self.radio = None  # set once made: its log lines are caught from the start
        self.s = s
        self.settings_file = settings_file
        self.log_lines = deque(maxlen=LOG_KEEP)
        self.log_seq = 0
        self.pl_cache = {}
        self.bpm = bpmdet.BpmCache(os.path.join(storage, bpmdet.BPM_FILE))
        self.lock = threading.RLock()
        self.stop_all = threading.Event()

    # log
    def add_log(self, line):
        self.log_lines.append(line)
        self.log_seq += 1

    # settings
    def set(self, **kv):
        """Change settings now and in the --settings file (only these keys: the rest stays as written)."""
        self.s.update(kv)
        if not self.settings_file:
            return
        try:
            with open(self.settings_file, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        d.update(kv)
        self._write(self.settings_file, d)

    @staticmethod
    def _write(path, d):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, path)

    def saved_note(self):
        return "" if self.settings_file else " (not saved: started without --settings)"

    # source
    def playlist_for(self, folder):
        folder = os.path.abspath(folder)
        pl = self.pl_cache.get(folder)
        if pl is None:
            pl = self.pl_cache[folder] = rl.Playlist(folder, os.path.join(self.radio.storage, rl.PLAYLIST_FILE),
                                                     self.s["shuffle"])
        pl.shuffle = self.s["shuffle"]
        return pl

    def playlist(self):
        """The playlist on air, else the one of the folder set."""
        src = self.radio.source
        if self.radio.on_air and src is not None and getattr(src, "playlist", None):
            return src.playlist
        folder = (self.s["folder"] or "").strip()
        return self.playlist_for(folder) if folder and os.path.isdir(folder) else None

    def folder_opts(self):
        s = self.s
        return {"shuffle": s["shuffle"], "fade": s["fade_s"] if s["fade_on"] else 0,
                "mix": s["mix_s"] if s["mix_on"] else 0, "trim": s["trim_silence"],
                "beats": s["beat_n"] if s["beat_on"] else 0, "loop_out": s["loop_out"], "loop_in": s["loop_in"],
                "loop_n": s["loop_n"], "loop_times": s["loop_times"]}

    def apply_folder_opts(self):
        src = self.radio.source
        if src is not None and getattr(src, "folder", None):
            for k, v in self.folder_opts().items():
                setattr(src, k, v)
            if src.playlist:
                src.playlist.shuffle = self.s["shuffle"]
        self.want_bpm()

    def want_bpm(self):
        # BPM and loudness only when something uses them (a Pi has better things to do)
        pl = self.playlist()
        if pl and (self.s["beat_on"] or self.s["norm_on"]):
            self.bpm.want(pl.items())

    def make_source(self):
        s = self.s
        if s["mode"] == "url":
            if not s["url"].strip():
                raise CtlError("no stream URL set: source url URL")
            return rl.Source(url=s["url"].strip())
        if s["mode"] == "line":
            if not s["input_device"].strip():
                raise CtlError("no input device set: source input DEVICE (see inputs)")
            return rl.LineSource(s["input_device"].strip())
        folder = s["folder"].strip()
        if not os.path.isdir(folder):
            raise CtlError(f"folder not found: {folder or '(none set)'}")
        pl = self.playlist_for(folder)
        src = rl.Source(folder=folder, playlist=pl, bpm_of=self.bpm.get,
                        mix_log=os.path.join(self.radio.storage, rl.MIX_LOG_FILE),
                        norm=lambda: self.radio.norm_lufs, **self.folder_opts())
        self.want_bpm()
        return src

    def apply_output(self):
        r, s = self.radio, self.s
        r.announce_on_start = bool(s["announce_on_start"])
        r.announce_every_min = int(s["announce_every_min"])
        r.lxmf_every_h = int(s["lxmf_every_h"])
        r.slots = max(0, min(99, int(s["slots"])))
        if bool(s["lxmf_on"]) != r.lxmf_on:
            r.lxmf_on = bool(s["lxmf_on"])
        r.gain_db = max(-24.0, min(12.0, float(s["gain_db"])))
        r.limiter_on = bool(s["limiter_on"])
        r.limiter.ceiling_db = max(-12.0, min(0.0, float(s["limiter_ceiling_db"])))
        lo, hi = rl.NORM_LUFS
        r.norm_lufs = max(lo, min(hi, int(s["norm_lufs"]))) if s["norm_on"] else None
        rl.set_frame_ms(int(s["frame_ms"]))  # quality, live
        rl.set_phone_60(s["phone_60"])
        if not r.on_air:
            r.name = s["name"] or "radio-lxst"

    def go_on_air(self):
        r = self.radio
        if r.on_air:
            return self.switch()
        if r.pending_source is not None:
            return "already waiting for the stamps, on air when ready"
        src = self.make_source()
        rl.set_frame_ms(int(self.s["frame_ms"]))
        r.name = self.s["name"] or "radio-lxst"
        r.start(src)
        return "on air" if r.on_air else "preparing stamps, on air when ready"

    def switch(self):
        if not self.radio.on_air:
            raise CtlError("off air: onair to start")
        src = self.make_source()
        o = self.folder_opts()
        self.radio.switch_source(src, o["mix"] or o["fade"])
        return f"switching to {rl.source_label(src)}"

    # one command line -> reply lines
    def run(self, line, who="terminal"):
        args = split_line(line)
        if not args:
            return []
        cmd, a = args[0].lower(), args[1:]
        fn = getattr(self, "c_" + cmd.replace("-", "_"), None)
        if fn is None:
            raise CtlError(f"unknown command: {cmd} (help lists them)")
        if not (cmd in READ_ONLY or (cmd in READ_ONLY_BARE and not a) or (cmd == "reply" and len(a) == 1)):
            RNS.log(f"Control ({who}): {line.strip()}", RNS.LOG_NOTICE)
        with self.lock:
            out = fn(a)
        if out is None:
            return []
        return out.split("\n") if isinstance(out, str) else list(out)

    # station
    def c_help(self, a):
        return HELP

    def c_status(self, a):
        r, s = self.radio, self.s
        snap = r.snapshot()
        if snap["waiting"]:
            state = f"PREPARING STAMPS {snap['stamps_done']}/{snap['stamps_total']}, on air when ready"
        else:
            state = "ON AIR " + _mmss(snap["uptime"]) if snap["on_air"] else "OFF AIR"
        out = [f"{r.name}  {state}"]
        src = r.shown_source
        if src is not None:
            out.append(f"source   {rl.source_label(src)} ({snap['source_state']})")
            np_ = r.now_playing_text()
            pr = snap["progress"]
            out.append(f"playing  {np_}" + (f"  {_mmss(pr[0])} / {_mmss(pr[1])}" if pr else "")
                       + ("  MIXING" if snap["mixing"] else ""))
        else:
            out.append(f"source   {self._source_set()} (set, not on air)")
        out.append(f"listeners {snap['listeners']}" + (f"/{r.slots}" if r.slots else "") +
                   f"  peak {snap['peak']}  calls {snap['calls']}  sent {RNS.prettysize(snap['bytes'])}")
        out.append(f"buffer {snap['buffer_ms'] / 1000:.1f} s  packets {rl.PACKET_MS} ms  "
                   f"send late max {snap['late_ms']:.0f} ms (since the last status)")
        r.max_late = 0.0
        out.append(f"volume {r.gain_db:+.1f} dB  limiter " +
                   (f"on {r.limiter.ceiling_db:.1f} dB (now -{snap['reduction_db']:.1f})" if r.limiter_on else "off") +
                   "  normalize " + (f"{r.norm_lufs} LUFS" if r.norm_lufs is not None else "off"))
        out.append("folder   " + self._folder_opts_text())
        na = snap["next_announce"]
        out.append(f"announce every {r.announce_every_min} min" + (f", next in {_mmss(max(0, na))}" if na is not None else "")
                   + (f", now allowed in {_mmss(snap['announce_wait'])}" if snap["announce_wait"] else "")
                   + f"  at on air {'on' if r.announce_on_start else 'off'}")
        out.append(f"lxmf {'on, every ' + str(r.lxmf_every_h) + ' h' if r.lxmf_on else 'off'}  "
                   f"monitor {'on' if r.monitor else 'off'}")
        return out

    def c_stats(self, a):
        v = self.radio.stats.view()
        out = station_stats.text(v["run"], v["all"])
        top = station_stats.top_tracks(self.radio.storage)
        if top:
            out += ["", "Most played"] + [f"{n:>5}x  {name}" for n, _, name in top]
        return out

    def _folder_opts_text(self):
        s = self.s
        bits = [f"shuffle {'on' if s['shuffle'] else 'off'}",
                f"fade {str(s['fade_s']) + ' s' if s['fade_on'] else 'off'}",
                f"mix {str(s['mix_s']) + ' s' if s['mix_on'] else 'off'}",
                f"trim {'on' if s['trim_silence'] else 'off'}",
                f"beatmix {str(s['beat_n']) + ' beats' if s['beat_on'] else 'off'}"]
        if s["beat_on"] and (s["loop_out"] or s["loop_in"]):
            which = "+".join(w for w, on in (("out", s["loop_out"]), ("in", s["loop_in"])) if on)
            bits.append(f"loop {which} {s['loop_n']} beats x {s['loop_times']}")
        return "  ".join(bits)

    def _source_set(self):
        s = self.s
        return {"url": f"stream {s['url'] or '(none)'}", "folder": f"folder {s['folder'] or '(none)'}",
                "line": f"input {s['input_device'] or '(none)'}"}.get(s["mode"], s["mode"])

    def c_onair(self, a):
        return self.go_on_air()

    c_start = c_onair

    def c_stop(self, a):
        r = self.radio
        if not r.on_air and r.pending_source is None:
            return "already off air"
        r.stop()
        return "off air"

    def c_announce(self, a):
        r = self.radio
        if a:
            return self._announce_set(a)
        if not r.on_air:
            r.name = self.s["name"] or "radio-lxst"
        if r.announce():
            return "announcing"
        return (f"held: one announce every {rl.ANNOUNCE_MIN_GAP // 60} min at most, "
                f"sent by itself in {_mmss(r.announce_wait())}")

    def _announce_set(self, a):
        if a[0] == "start" and len(a) == 2:
            self.set(announce_on_start=_onoff(a[1]))
            self.apply_output()
            return f"announce when going on air: {'on' if self.s['announce_on_start'] else 'off'}" + self.saved_note()
        if a[0] == "every" and len(a) == 2:
            m = int(_num(a[1], 0, 10080, int))
            m = max(rl.ANNOUNCE_MIN_EVERY, m) if m else 0
            self.set(announce_every_min=m)
            self.apply_output()
            return (f"announce every {m} min" if m else "periodic announce off") + self.saved_note()
        raise CtlError("announce | announce start on|off | announce every MIN")

    def c_id(self, a):
        r = self.radio
        return [f"call ID            {r.identity_hash}",
                f"LXST destination   {r.dest.hash.hex()}",
                f"station directory  {r.station.hash.hex()}",
                f"LXMF address       {r.lxmf_hash}" + ("" if r.lxmf_on else " (LXMF off)")]

    def c_listeners(self, a):
        snap = self.radio.snapshot()
        if not snap["rows"]:
            return "no listeners"
        out = []
        for row in snap["rows"]:
            name = f" ({row['user']})" if row["user"] else ""
            sub = "  subscribed" if row["sub"] else ""
            if row["state"] == "listening":
                out.append(f"{row['id']}{name}  {row['profile']}  {_mmss(row['time'])}  {row['kbps']:.1f} kbit/s"
                           f"  lost {row['lost']}{sub}")
            else:
                out.append(f"{row['id']}{name}  {row['state']}{sub}")
        return out

    def c_log(self, a):
        n = int(_num(a[0], 1, LOG_KEEP, int)) if a else 30
        return list(self.log_lines)[-n:] or "log empty"

    def c_save(self, a):
        if len(a) != 1:
            raise CtlError("save FILE")
        path = os.path.abspath(os.path.expanduser(a[0]))
        try:
            self._write(path, self.s)
        except OSError as e:
            raise CtlError(f"not saved: {e}")
        return f"saved to {path}, start with --settings {path}"

    def c_shutdown(self, a):
        self.stop_all.set()
        return "shutting down"

    # source
    def c_source(self, a):
        if not a:
            return self._source_set() + ("  (on air: " + rl.source_label(self.radio.source) + ")"
                                         if self.radio.on_air and self.radio.source else "")
        kind, rest = a[0].lower(), " ".join(a[1:]).strip()
        if not rest:
            raise CtlError("source url URL | source folder PATH | source input DEVICE")
        if kind == "url":
            self.set(mode="url", url=rest)
        elif kind == "folder":
            path = os.path.abspath(os.path.expanduser(rest))
            if not os.path.isdir(path):
                raise CtlError(f"folder not found: {path}")
            self.set(mode="folder", folder=path)
            self.playlist_for(path)
        elif kind in ("input", "line"):
            self.set(mode="line", input_device=rest)
        else:
            raise CtlError("source url URL | source folder PATH | source input DEVICE")
        what = "switch to put it on air now" if self.radio.on_air else "onair to start"
        return f"source set: {self._source_set()} ({what})" + self.saved_note()

    def c_inputs(self, a):
        return rl.list_inputs() or "no input devices found"

    def c_switch(self, a):
        return self.switch()

    def c_next(self, a):
        src = self.radio.source
        if not (self.radio.on_air and src is not None and src.folder):
            raise CtlError("next works with a folder on air")
        src.skip()
        return "next track"

    def c_seek(self, a):
        src = self.radio.source
        if not (self.radio.on_air and src is not None and src.folder and hasattr(src, "seek")):
            raise CtlError("seek works with a folder on air")
        if len(a) != 1:
            raise CtlError("seek M:SS")
        t = _secs(a[0])
        src.seek(t)
        return f"seek to {_mmss(t)} (see the log if a mix is playing)"

    # folder options
    def _folder_set(self, **kv):
        self.set(**kv)
        self.apply_folder_opts()
        return "folder  " + self._folder_opts_text() + self.saved_note()

    def c_shuffle(self, a):
        return self._folder_set(shuffle=_onoff(a[0] if a else ""))

    def _secs_opt(self, a, on_key, s_key):
        if not a:
            raise CtlError("on, off or seconds (1-30)")
        if a[0].replace(".", "").isdigit():
            return self._folder_set(**{on_key: True, s_key: int(_num(a[0], 1, 30))})
        return self._folder_set(**{on_key: _onoff(a[0])})

    def c_fade(self, a):
        return self._secs_opt(a, "fade_on", "fade_s")

    def c_mix(self, a):
        return self._secs_opt(a, "mix_on", "mix_s")

    def c_trim(self, a):
        return self._folder_set(trim_silence=_onoff(a[0] if a else ""))

    def c_beatmix(self, a):
        if not a:
            raise CtlError(f"beatmix on|off|BEATS {BEAT_CHOICES}")
        if a[0].isdigit():
            n = int(a[0])
            if n not in BEAT_CHOICES:
                raise CtlError(f"beats: one of {BEAT_CHOICES}")
            return self._folder_set(beat_on=True, beat_n=n)
        return self._folder_set(beat_on=_onoff(a[0]))

    def c_loop(self, a):
        if len(a) == 2 and a[0] in ("out", "in"):
            return self._folder_set(**{"loop_" + a[0]: _onoff(a[1])})
        if len(a) == 2 and a[0] in ("beats", "times"):
            ok = rl.LOOP_CHOICES if a[0] == "beats" else rl.LOOP_TIMES
            if not a[1].isdigit() or int(a[1]) not in ok:
                raise CtlError(f"loop {a[0]}: one of {ok}")
            return self._folder_set(**{"loop_n" if a[0] == "beats" else "loop_times": int(a[1])})
        raise CtlError("loop out on|off | loop in on|off | loop beats 4|8|16 | loop times 4|8|16")

    # playlist
    def _pl(self):
        pl = self.playlist()
        if pl is None:
            raise CtlError("no folder: source folder PATH first")
        return pl

    def _tracks(self, pl, a, one=False):
        """Numbers (1-based), ranges 3-7, lists 2,5 or part of a name -> paths."""
        items = pl.items()
        if not a:
            raise CtlError("which track? a number from list, or part of its name")
        out = []
        text = " ".join(a)
        tokens = text.replace(",", " ").split()
        ranges = []
        for t in tokens:
            lo, _, hi = t.partition("-")
            if not (lo.isdigit() and (hi.isdigit() or (not hi and "-" not in t))):
                ranges = None
                break
            ranges.append((t, int(lo), int(hi or lo)))
        if ranges:
            for t, lo, hi in ranges:
                if not (1 <= lo <= hi <= len(items)):
                    raise CtlError(f"no track {t} (1..{len(items)})")
                out += items[lo - 1:hi]
        else:
            low = text.lower()
            out = [p for p in items if low in pl.rel(p).lower()]
            if not out:
                raise CtlError(f"no track with '{text}' in the name")
            if one and len(out) > 1:
                raise CtlError(f"{len(out)} tracks match '{text}', use the number:\n" +
                               "\n".join(self._row(pl, p, items.index(p) + 1) for p in out[:20]))
        if one and len(out) != 1:
            raise CtlError("one track only")
        return out

    def _row(self, pl, p, n):
        src = self.radio.shown_source
        air = getattr(src, "now_path", None) if self.radio.on_air else None
        mixing = set(src.mixing_paths()) if air and hasattr(src, "mixing_paths") else set()
        mark = "~" if p in mixing else ">" if p == air else "*" if pl.played(p)[0] else " "
        r = self.bpm.get(p) or {}
        dur = _mmss(r["dur"]) if r.get("dur") else ""
        b = f"{r['bpm']:.1f}" if r.get("bpm") else ""
        return f"{n:>4} {mark} {pl.rel(p)}" + (f"  [{dur}" + (f" {b} BPM" if b else "") + "]" if dur or b else "")

    def c_list(self, a):
        pl = self._pl()
        items = pl.items()
        start = int(_num(a[0], 1, max(1, len(items)), int)) if a else 1
        count = int(_num(a[1], 1, 100000, int)) if len(a) > 1 else len(items)
        on = "on air" if getattr(self.radio.source, "playlist", None) is pl and self.radio.on_air else "not on air"
        out = [f"{len(items)} tracks in {pl.folder} ({on})" if pl.scanned else "scanning..."]
        out += [self._row(pl, p, i + 1) for i, p in enumerate(items) if start - 1 <= i < start - 1 + count]
        return out

    def c_find(self, a):
        pl = self._pl()
        items = pl.items()
        low = " ".join(a).lower()
        hits = [self._row(pl, p, i + 1) for i, p in enumerate(items) if low in pl.rel(p).lower()]
        return hits or f"no track with '{low}' in the name"

    def c_play(self, a):
        pl = self._pl()
        path = self._tracks(pl, a, one=True)[0]
        r = self.radio
        if r.on_air and not getattr(r.source, "folder", None):
            raise CtlError("on air with a stream or line input: source folder PATH, then switch")
        pl.play_now(path)
        if r.on_air and getattr(r.source, "playlist", None) is pl:
            r.source.skip()
            return f"playing now: {pl.rel(path)}"
        if r.on_air:
            raise CtlError("that folder is not on air: switch first")
        self.set(mode="folder", folder=pl.folder)
        return f"{self.go_on_air()}, starting with {pl.rel(path)}"

    def c_queue(self, a):
        pl = self._pl()
        paths = self._tracks(pl, a)
        pl.play_next(paths)
        return f"next: {pl.rel(paths[0])}" + (f" (+{len(paths) - 1})" if len(paths) > 1 else "")

    def c_remove(self, a):
        pl = self._pl()
        paths = self._tracks(pl, a)
        pl.remove(paths)
        return f"removed from the list: {pl.rel(paths[0])}" + (f" (+{len(paths) - 1})" if len(paths) > 1 else "") + \
            " (files stay on disk)"

    def c_clear(self, a):
        pl = self._pl()
        if a != ["yes"]:
            raise CtlError(f"empties the list of {len(pl.items())} tracks (files stay on disk): clear yes")
        n = len(pl.items())
        pl.clear()
        return f"list cleared: {n} track(s) out, add {pl.folder} brings the folder back"

    def c_move(self, a):
        pl = self._pl()
        if len(a) >= 3 and a[-2] == "to":
            path = self._tracks(pl, a[:-2], one=True)[0]
            items = pl.items()
            pos = int(_num(a[-1], 1, len(items), int))
            rest = [p for p in items if p != path]
            pl.set_order(rest[:pos - 1] + [path] + rest[pos - 1:])
            return f"{pl.rel(path)} now at {pos}"
        steps = 1
        if len(a) >= 3 and a[-1].isdigit() and a[-2] in ("up", "down"):
            steps, a = int(a[-1]), a[:-1]
        if len(a) < 2 or a[-1] not in ("up", "down"):
            raise CtlError("move TRACKS up|down [N] | move TRACK to POS")
        paths = self._tracks(pl, a[:-1])
        for _ in range(min(steps, len(pl.items()))):
            pl.move(paths, -1 if a[-1] == "up" else 1)
        items = pl.items()
        return f"{pl.rel(paths[0])} now at {items.index(paths[0]) + 1}"

    def c_add(self, a):
        pl = self._pl()
        path = os.path.abspath(os.path.expanduser(" ".join(a).strip()))
        if not a or not os.path.exists(path):
            raise CtlError(f"not found: {path}")
        n0 = len(pl.items())
        pl.add([path])
        self.want_bpm()
        return f"added {path}: {len(pl.items()) - n0} track(s) more"

    def c_added(self, a):
        pl = self._pl()
        with pl.lock:
            pl._load_lists()
            extra = list(pl.extra)
        if not extra:
            return "nothing added from outside the folder"
        return [f"{i + 1:>3}  {p}" + ("" if os.path.exists(p) else "  (missing now)") for i, p in enumerate(extra)]

    def c_unadd(self, a):
        pl = self._pl()
        with pl.lock:
            pl._load_lists()
            extra = list(pl.extra)
        arg = " ".join(a).strip()
        if arg.isdigit() and 1 <= int(arg) <= len(extra):
            path = extra[int(arg) - 1]
        else:
            path = os.path.abspath(os.path.expanduser(arg)) if arg else ""
            if path not in extra:
                raise CtlError("unadd N|PATH, N from added")
        n0 = len(pl.items())
        pl.remove_added(path)
        return f"forgot {path}: {n0 - len(pl.items())} track(s) less (files stay on disk)"

    def c_unplayed(self, a):
        pl = self._pl()
        paths = self._tracks(pl, a)
        pl.set_unplayed(paths)
        return f"{len(paths)} track(s) marked not played"

    def c_reshuffle(self, a):
        self._pl().shuffle_now()
        return "list shuffled"

    def c_sort(self, a):
        pl = self._pl()
        if not a or a[0] not in SORTS:
            raise CtlError("sort KEY [desc], KEY: " + " ".join(SORTS))
        key, rev = a[0], len(a) > 1 and a[1] in ("desc", "reverse", "z-a")
        if key == "random":
            pl.shuffle_now()
            return "list shuffled"
        cache = self.bpm
        keys = {"name": None,
                "bpm": lambda p: (cache.get(p) or {}).get("bpm"),
                "len": lambda p: (cache.get(p) or {}).get("dur") or None,
                "plays": lambda p: pl.plays.get(p, 0),
                "fresh": lambda p: int(pl.played(p)[0]),
                "last": lambda p: pl.last_played.get(p, 0)}
        pl.sort_now(key=keys[key], reverse=rev)
        return f"sorted by {SORTS[key]}" + (" (reverse)" if rev else "")

    def c_bpm(self, a):
        pl = self._pl()
        paths = self._tracks(pl, a)
        self.bpm.want(paths, force=True)
        return f"BPM: analysing {len(paths)} track(s) again"

    def c_rescan(self, a):
        pl = self._pl()
        n0 = len(pl.items())
        pl.rescan()
        self.want_bpm()
        return f"{len(pl.items())} tracks ({len(pl.items()) - n0:+d})"

    # output
    def c_volume(self, a):
        if not a:
            return f"volume {self.radio.gain_db:+.1f} dB"
        g = round(_num(a[0], -24, 12) * 2) / 2
        self.set(gain_db=g)
        self.apply_output()
        return f"volume {g:+.1f} dB" + self.saved_note()

    def c_limiter(self, a):
        self.set(limiter_on=_onoff(a[0] if a else ""))
        self.apply_output()
        return f"limiter {'on' if self.s['limiter_on'] else 'off'}" + self.saved_note()

    def c_ceiling(self, a):
        c = _num(a[0] if a else "", -12, 0)
        self.set(limiter_ceiling_db=c)
        self.apply_output()
        return f"limiter ceiling {c:.1f} dB" + self.saved_note()

    def c_normalize(self, a):
        if not a:
            raise CtlError(f"normalize on|off|LUFS ({rl.NORM_LUFS[0]}..{rl.NORM_LUFS[1]})")
        if a[0].lstrip("-").isdigit():
            self.set(norm_on=True, norm_lufs=int(_num(a[0], rl.NORM_LUFS[0], rl.NORM_LUFS[1], int)))
        else:
            self.set(norm_on=_onoff(a[0]))
        self.apply_output()
        self.want_bpm()
        return ("normalize " + (f"{self.s['norm_lufs']} LUFS" if self.s["norm_on"] else "off")
                + " (from the next track)" + self.saved_note())

    def c_name(self, a):
        name = " ".join(a).strip()
        if not name:
            return self.radio.name
        self.set(name=name)
        self.apply_output()
        return f"name {name}" + (" (on air: from the next onair)" if self.radio.on_air else "") + self.saved_note()

    def c_slots(self, a):
        n = int(_num(a[0] if a else "", 0, 99, int))
        self.set(slots=n)
        self.apply_output()
        return (f"{n} listener slots" if n else "no listener limit") + self.saved_note()

    def c_quality(self, a):
        q = (a[0] if a else "").lower()
        if q not in ("steady", "high"):
            raise CtlError("quality steady (120 ms, up to 28 kbit/s) | high (60 ms, up to 56 kbit/s)")
        self.set(frame_ms=120 if q == "steady" else 60)
        self.apply_output()
        return f"quality {q}" + self.saved_note()

    def c_phone60(self, a):
        self.set(phone_60=_onoff(a[0] if a else ""))
        self.apply_output()
        return f"phones always get 60 ms packets: {'on' if self.s['phone_60'] else 'off'}" + self.saved_note()

    def c_autostart(self, a):
        self.set(auto_start=_onoff(a[0] if a else ""))
        return f"GUI goes on air at start: {'on' if self.s['auto_start'] else 'off'}" + self.saved_note()

    def c_lxmf(self, a):
        if len(a) == 2 and a[0] == "every":
            h = int(_num(a[1], rl.LXMF_MIN_EVERY_H, 168, int))
            self.set(lxmf_every_h=h)
            self.apply_output()
            return f"LXMF address announced every {h} h" + self.saved_note()
        self.set(lxmf_on=_onoff(a[0] if a else ""))
        self.apply_output()
        return f"LXMF {'on' if self.s['lxmf_on'] else 'off'}" + self.saved_note()

    def c_monitor(self, a):
        r = self.radio
        if len(a) >= 2 and a[0] == "device":
            dev = " ".join(a[1:]).strip()
            self.set(monitor_device="" if dev.lower() == "default" else dev)
            if r.monitor:
                self._monitor(False)
                self._monitor(True)
            return f"monitor device {self.s['monitor_device'] or 'default'}" + self.saved_note()
        on = _onoff(a[0] if a else "")
        return self._monitor(on)

    def _monitor(self, on):
        r = self.radio
        if not on:
            if r.monitor:
                r.monitor.stop()
                r.monitor = None
            return "monitor off"
        if r.monitor:
            return "monitor already on"
        import radio_client as rc
        dev = self.s["monitor_device"]
        src = r.source
        if isinstance(src, rl.LineSource) and src.device.startswith("Loopback: "):
            if src.device[len("Loopback: "):] == (dev or rl.default_output_name()):
                raise CtlError("not started: it would play into the device the input records (feedback)")
        mon = rc.monitor_player(dev or None)
        mon.start()
        r.monitor = mon
        return f"monitor on: {dev or 'default output'}"

    def c_outputs(self, a):
        import radio_client as rc
        return rc.list_devices() or "no output devices found"

    # replies
    def c_replies(self, a):
        saved = sl.load_replies(self.radio.storage)
        return [f"{'+' if k in saved else ' '} {k:<20} {label}" for k, label in sl.REPLY_LABELS] + \
               ["placeholders: " + " ".join("{" + p + "}" for p in sl.PLACEHOLDERS)]

    def c_reply(self, a):
        keys = [k for k, _ in sl.REPLY_LABELS]
        if not a or a[0] not in keys:
            raise CtlError("reply KEY [TEXT|default], KEY: " + " ".join(keys))
        key = a[0]
        saved = sl.load_replies(self.radio.storage)
        if len(a) == 1:
            return saved.get(key, sl.DEFAULT_REPLIES[key]).replace("\n", "\\n")
        text = " ".join(a[1:])
        if text == "default":
            saved.pop(key, None)
        else:
            saved[key] = text.replace("\\n", "\n")
        try:
            kept = sl.save_replies(self.radio.storage, dict(sl.DEFAULT_REPLIES, **saved))
        except OSError as e:
            raise CtlError(f"not saved: {e}")
        if self.radio.lx:
            self.radio.lx.reload_replies()
        return f"reply {key} " + ("back to default" if key not in kept else "saved") + f" ({len(kept)} custom)"


# local channel
class Server:
    """Text lines in, reply lines out (two spaces in front), then 'ok' or 'err REASON'."""

    def __init__(self, station, storage):
        self.st = station
        self.storage = storage
        self.info_path = os.path.join(storage, CTL_INFO)
        self.sock_path = os.path.join(storage, CTL_SOCK)
        self.token = None
        self.sock = None

    def start(self):
        self._clean()
        if hasattr(socket, "AF_UNIX") and os.name != "nt" and len(self.sock_path.encode()) < 100:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old = os.umask(0o177)  # socket file for the owner only
            try:
                s.bind(self.sock_path)
            finally:
                os.umask(old)
            info = {"unix": self.sock_path}
            where = f"nc -U {self.sock_path}"
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            self.token = secrets.token_hex(16)
            info = {"port": s.getsockname()[1], "token": self.token}
            where = f"127.0.0.1:{info['port']}, token in {self.info_path}"
        s.listen(4)
        self.sock = s
        fd = os.open(self.info_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f)
        threading.Thread(target=self._accept, daemon=True).start()
        return where

    def _clean(self):
        for p in (self.sock_path, self.info_path):
            try:
                os.remove(p)
            except OSError:
                pass

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass
        self._clean()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    @staticmethod
    def _send(c, lines, end):
        data = "".join(f"  {l}\n" for l in lines) + end + "\n"
        c.sendall(data.encode("utf-8", "replace"))

    def _client(self, c):
        f = c.makefile("r", encoding="utf-8", errors="replace", newline="\n")
        try:
            if self.token:
                first = f.readline().strip()
                if first != f"auth {self.token}":
                    self._send(c, [], "err auth TOKEN first (token in ctl.json)")
                    return
            self._send(c, [f"radio-lxst {rl.__version__} station {self.st.radio.name}, help lists the commands"], "ok")
            for line in f:
                line = line.strip()
                if not line:
                    continue
                cmd = line.split()[0].lower()
                if cmd in ("quit", "exit", "bye"):
                    self._send(c, ["bye"], "ok")
                    return
                if cmd in ("watch", "follow"):
                    self._stream(c, cmd, line.split()[1:])
                    continue
                try:
                    self._send(c, self.st.run(line), "ok")
                except CtlError as e:
                    msg = str(e).split("\n")
                    self._send(c, msg[1:], "err " + msg[0])
                except Exception as e:
                    RNS.log(f"Control command '{line}' failed: {e}", RNS.LOG_ERROR)
                    self._send(c, [], f"err failed: {e}")
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def _stream(self, c, cmd, a):
        """watch / follow until the client sends a line or goes away."""
        try:
            every = max(1.0, float(a[0])) if cmd == "watch" and a else 5.0
        except ValueError:
            self._send(c, [], "err watch [SECONDS]")
            return
        seq = self.st.log_seq
        next_t = 0.0
        while not self.st.stop_all.is_set():
            if cmd == "watch" and time.monotonic() >= next_t:
                next_t = time.monotonic() + every
                with self.st.lock:
                    lines = [time.strftime("[%H:%M:%S]")] + self.st.c_status([])
                c.sendall("".join(f"  {l}\n" for l in lines).encode("utf-8", "replace"))
            elif cmd == "follow" and self.st.log_seq != seq:
                new = min(self.st.log_seq - seq, LOG_KEEP)
                seq = self.st.log_seq
                lines = list(self.st.log_lines)[-new:]
                c.sendall("".join(f"  {l}\n" for l in lines).encode("utf-8", "replace"))
            r, _, _ = select.select([c], [], [], 0.3)
            if r:
                break  # Enter (or the line itself) stops it; the reader reads that line and skips it if empty
        self._send(c, [], "ok")


# client side: radio_lxst.py --ctl [COMMAND]
def _open(storage):
    """Socket to the running station; OSError if there is none."""
    try:
        with open(os.path.join(storage, CTL_INFO), encoding="utf-8") as f:
            info = json.load(f)
    except ValueError as e:
        raise OSError(e)
    if "unix" in info:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(info["unix"])
    else:
        c = socket.create_connection(("127.0.0.1", info["port"]), timeout=10)
        c.settimeout(None)
        c.sendall(f"auth {info['token']}\n".encode())
    return c


def _connect(storage):
    if not os.path.isfile(os.path.join(storage, CTL_INFO)):
        sys.exit(f"no station running with {storage} (or started without the control channel)")
    try:
        return _open(storage)
    except OSError as e:
        sys.exit(f"cannot reach the station ({e}): is it running?")


def query(storage, line):
    """Reply lines of one command to the running station, None if it is not running or says err."""
    try:
        c = _open(storage)
        c.settimeout(10)
        f = c.makefile("r", encoding="utf-8", errors="replace", newline="\n")
        c.sendall((line + "\n").encode("utf-8"))
        out, greeted = [], False
        for l in f:
            l = l.rstrip("\n")
            if l == "ok" and not greeted:
                greeted, out = True, []
            elif l == "ok":
                c.close()
                return out
            elif l.startswith("err"):
                break
            else:
                out.append(l[2:] if l.startswith("  ") else l)
        c.close()
    except OSError:
        pass
    return None


def _reply(f, show=True):
    """Print reply lines until ok / err; True if ok."""
    for line in f:
        line = line.rstrip("\n")
        if line == "ok":
            return True
        if line.startswith("err"):
            print(line[4:] or "error", file=sys.stderr)
            return False
        if show:
            print(line[2:] if line.startswith("  ") else line, flush=True)
    print("the station closed the connection", file=sys.stderr)
    return False


def _one(c, f, line):
    c.sendall((line + "\n").encode("utf-8"))
    if line.split()[0].lower() in ("watch", "follow"):
        try:
            return _reply(f)
        except KeyboardInterrupt:
            c.sendall(b"\n")
            return _reply(f)
    return _reply(f)


def ctl_main(storage, words):
    c = _connect(storage)
    f = c.makefile("r", encoding="utf-8", errors="replace", newline="\n")
    if not _reply(f, show=not words):
        sys.exit(1)
    if words:
        line = " ".join(f'"{w}"' if (not w or any(ch.isspace() for ch in w)) else w for w in words)
        ok = _one(c, f, line)
        c.close()
        sys.exit(0 if ok else 1)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.split()[0].lower() in ("quit", "exit", "bye"):
            break
        _one(c, f, line)
        if line.split()[0].lower() == "shutdown":
            break
    c.close()
