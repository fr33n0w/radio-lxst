#!/usr/bin/env python3
# radio-lxst: internet radio over Reticulum, listeners "call" it with any LXST phone (Sideband, rnphone).

import os
import sys
import json
import time
import types
import random
import signal
import ctypes
import argparse
import threading
import subprocess
from collections import deque

import numpy as np
import RNS
from RNS.vendor import umsgpack as mp

try:
    import pycodec2  # noqa: F401
except Exception:
    # LXST imports pycodec2 even if only Opus is used
    sys.modules["pycodec2"] = types.ModuleType("pycodec2")

import bpm as bpmdet

from LXST.Codecs import Opus
from LXST.Codecs.libs.pyogg import opus, OpusEncoder


def keep_path():
    """LXST's pyogg adds to PATH with ';' on Linux too, glued to the last entry: under systemd that one is /usr/bin
    and ffmpeg is gone. Repair PATH now and after every library it loads."""
    if os.name == "nt":
        return
    os.environ["PATH"] = ":".join(p.split(";")[0] for p in os.environ.get("PATH", "").split(":"))
    try:
        from LXST.Codecs.libs.pyogg.library_loader import ExternalLibrary
    except Exception:
        return
    orig = ExternalLibrary.load_other
    if getattr(orig, "_keeps_path", False):
        return

    def load_other(*a, **k):
        path = os.environ.get("PATH", "")
        try:
            return orig(*a, **k)
        finally:
            os.environ["PATH"] = path

    load_other._keeps_path = True
    ExternalLibrary.load_other = staticmethod(load_other)


keep_path()

__version__ = "0.7"

APP_NAME = "lxst"
ASPECT = "telephony"
STATION_ASPECT = ("radiolxst", "station")
FIELD_META = "radiolxst"
FIELD_LISTENER = "radiolxst_listener"  # the name a client sends us inside the call
META_REFRESH = 30
ANNOUNCE_MIN_EVERY = 20  # minutes, periodic announces never more often
ANNOUNCE_MIN_GAP = 600   # seconds between any two announces (manual, on air, stop)
STATUS_MAX_PER_MIN = 120  # status requests answered per minute, all askers together
ANNOUNCE_SPACING = 5     # seconds between the announces of one round
LXMF_MIN_EVERY_H = 1     # LXMF address announce, hours
NAME_RETRY = 30          # seconds between name lookups of a listener still unnamed
FIELD_SIGNALLING = 0x00
FIELD_FRAMES = 0x01
CODEC_OPUS = b"\x01"

STATUS_BUSY = 0x00
STATUS_AVAILABLE = 0x03
STATUS_RINGING = 0x04
STATUS_CONNECTING = 0x05
STATUS_ESTABLISHED = 0x06
PREFERRED_PROFILE = 0xFF

SAMPLE_RATE = 48000
CHANNELS = 2
# audio moves in 60 ms frames; Steady only packs two of them in one 120 ms packet, so it changes live
FRAME_MS = 60
OUT_AVG_S = 3.0  # OUT level on the station display: seconds averaged (LUFS short-term is 3 s)
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2
MAX_QUEUE_FRAMES = int(3000 / FRAME_MS)
PACKET_MS = 60  # packets to our clients: 60 High, 120 Steady (more jitter tolerance, less bitrate)


PHONE_60 = True  # LXST phones get 60 ms whatever the quality (the LXST Opus presets; Sideband plays 120 too)


def set_frame_ms(ms):
    global PACKET_MS
    PACKET_MS = 120 if int(ms) >= 120 else 60


def set_phone_60(on):
    global PHONE_60
    PHONE_60 = bool(on)


def quality_kb():
    return 28 if PACKET_MS >= 120 else 56
# LXST phones (Sideband, rnphone, Columba) get the standard 60 ms Opus packets: Columba's decoder holds 60 ms at most
PHONE_FRAME_MS = 60
ANSWER_DELAY = 1.0
AUDIO_EXTS = (".mp3", ".ogg", ".opus", ".flac", ".wav", ".m4a", ".aac")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# caller profile (LXST Telephony) -> Opus music profile we send
PROFILE_MAP = {
    0x10: Opus.PROFILE_AUDIO_MIN, 0x20: Opus.PROFILE_AUDIO_MIN, 0x30: Opus.PROFILE_AUDIO_LOW,
    0x40: Opus.PROFILE_AUDIO_MEDIUM, 0x50: Opus.PROFILE_AUDIO_HIGH, 0x60: Opus.PROFILE_AUDIO_HIGH,
    0x70: Opus.PROFILE_AUDIO_MEDIUM, 0x80: Opus.PROFILE_AUDIO_MEDIUM,
}
CALLER_PROFILE_NAMES = {0x10: "ULBW", 0x20: "VLBW", 0x30: "LBW", 0x40: "MQ",
                        0x50: "HQ", 0x60: "SHQ", 0x70: "ULL", 0x80: "LL"}
DEFAULT_PROFILE = Opus.PROFILE_AUDIO_MEDIUM


class _Src:
    samplerate = SAMPLE_RATE


class LongOpusEncoder(OpusEncoder):
    """pyogg encoder without its 60 ms limit (libopus >= 1.2 takes 80/100/120 ms)."""

    def encode(self, pcm):
        if self._encoder is None:
            self._encoder = self._create_encoder()
        frame_size = len(pcm) // 2 // self._channels
        buf = (ctypes.c_ubyte * len(pcm)).from_buffer_copy(pcm)
        n = opus.opus_encode(self._encoder, ctypes.cast(buf, ctypes.POINTER(opus.opus_int16)),
                             ctypes.c_int(frame_size), self._output_buffer_ptr, self._max_bytes_per_frame)
        if n < 0:
            raise RuntimeError(opus.opus_strerror(n).decode("utf-8"))
        return memoryview(self._output_buffer).cast("c")[:n]


class RadioOpus(Opus):
    """Opus with the frame size capped to one Link packet."""
    MAX_FRAME_BYTES = 420

    def __init__(self, profile):
        super().__init__(profile=profile)
        self.opus_encoder = LongOpusEncoder()
        self.opus_encoder.set_application(self.profile_application(profile))

    def update_bitrate(self, frame_duration_ms):
        self.bitrate_ceiling = self.profile_bitrate_ceiling(self.profile)
        b = min(self.max_bytes_per_frame(self.bitrate_ceiling, frame_duration_ms), self.MAX_FRAME_BYTES)
        self.opus_encoder.set_max_bytes_per_frame(b)


class Limiter:
    """Peak limiter: 5 ms look-ahead ramps inside the frame, smooth release, never above the ceiling."""

    BLOCK = SAMPLE_RATE // 200

    def __init__(self, ceiling_db=-2.0, release_ms=150):
        self.ceiling_db = ceiling_db
        self.release_ms = release_ms
        self.gain = 1.0
        self.reduction_db = 0.0

    def process(self, x):
        ceiling = 10 ** (self.ceiling_db / 20)
        n = x.shape[0]
        nb = n // self.BLOCK
        peaks = np.abs(x[:nb * self.BLOCK]).reshape(nb, self.BLOCK, -1).max(axis=(1, 2))
        rel = np.exp(-self.BLOCK / (SAMPLE_RATE * self.release_ms / 1000))
        if nb == 0:
            return np.clip(x * self.gain, -ceiling, ceiling)
        g = self.gain
        t = np.empty(nb)
        for i, p in enumerate(peaks):
            need = min(1.0, ceiling / p) if p > 0 else 1.0
            g = need if need < g else need + (g - need) * rel
            t[i] = g
        # each block ramps between gains that are <= its own target: no overs, no steps
        starts = np.minimum(np.concatenate(([self.gain], t[:-1])), t)
        ends = np.minimum(t, np.concatenate((t[1:], t[-1:])))
        frac = np.arange(self.BLOCK) / self.BLOCK
        curve = (starts[:, None] + (ends - starts)[:, None] * frac[None, :]).ravel()
        if n > curve.size:
            curve = np.concatenate((curve, np.full(n - curve.size, ends[-1])))
        self.gain = t[-1]
        self.reduction_db = float(-20 * np.log10(t.min()))
        return np.clip(x * curve[:, None], -ceiling, ceiling)


SILENCE = 10 ** (-50 / 20)  # below -50 dBFS = silence for the trim
SILENCE_KEEP = 20           # s of silence kept inside a track, longer gaps are cut
FOLDER_AHEAD = 25           # frames queued ahead in folder mode (1.5 s against a busy PC)
PLAYLIST_FILE = "playlist.json"  # in the storage dir: track order per folder
PLAYED_FILE = "played.json"  # in the storage dir: last played time per track
NORM_LUFS = (-18, -8)  # Normalize targets allowed: -14 like streaming services, up to -8 for a hot master
NORM_MAX_BOOST = 12   # dB: a very quiet track is raised at most this much (the limiter holds the peaks)
FOLDER_HEADROOM = 0.25  # folder frames carry 12 dB of room above full scale, up to the limiter
MIX_LOG_FILE = "beatmix.jsonl"


def norm_gain_db(target, lufs):
    """dB Normalize gives a track of that loudness."""
    return min(NORM_MAX_BOOST, target - lufs)


def pcm_frame(src, pcm):
    """int16 frame bytes of a source -> float frame at its real level (may be above 1.0 before the limiter)."""
    return np.frombuffer(pcm, dtype=np.int16).reshape(FRAME_SAMPLES, CHANNELS) / (32768.0 * getattr(src, "headroom", 1.0))  # in the storage dir: one line per change of track with beatmix on


def list_audio(folder):
    """Audio files of the folder and all its subfolders, in path order."""
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        out += [os.path.join(root, f) for f in files if f.lower().endswith(AUDIO_EXTS)]
    return sorted(out, key=lambda p: os.path.relpath(p, folder).lower())


class Playlist:
    """Track order of a folder, saved per folder in a json file (new files at the end, missing ones dropped).
    next() is the track after the last one picked in the order of now, so it can be edited while on air."""

    MAX_FOLDERS = 20
    MAX_PLAYED = 20000
    session = {}  # path -> time played since the station started (the check mark)

    def __init__(self, folder, store=None, shuffle=False, scan=True):
        self.folder = os.path.abspath(folder)
        self.store, self.shuffle = store, shuffle
        self.played_store = os.path.join(os.path.dirname(store), PLAYED_FILE) if store else None
        self.lock = threading.RLock()
        self.last_played, self.plays = self._load_played()  # path -> last time / times played, kept
        self.order = []      # absolute paths
        self.extra = None    # files and folders added from outside the folder (absolute), None = not loaded
        self.removed = set()  # tracks taken out of the list by hand (the files stay on disk)
        self.current = None  # last track picked by next()
        self._idx = 0        # its index, used if it leaves the list
        self._forced = None  # play_now() target
        self.version = 0     # bumped at every change: the GUI redraws on it
        self.scanned = False
        if scan:
            self.rescan()

    @staticmethod
    def _under(path, root):
        try:
            return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
        except ValueError:  # other drive
            return False

    def rel(self, path):
        """Name shown: path in the folder; added tracks as <added folder>/... or the file name."""
        if self._under(path, self.folder):
            return os.path.relpath(path, self.folder)
        for e in self.extra or ():
            if os.path.isdir(e) and self._under(path, e):
                return os.path.join(os.path.basename(e), os.path.relpath(path, e))
        return os.path.basename(path)

    def _key(self, path):
        # saved: relative inside the folder, absolute for added tracks (join() keeps an absolute one)
        return os.path.relpath(path, self.folder) if self._under(path, self.folder) else path

    def _load_lists(self, saved=None):
        # under self.lock: added sources and removed tracks, once
        if self.extra is None:
            saved = self._load().get(self.folder, {}) if saved is None else saved
            self.extra = list(saved.get("extra", []))
            self.removed = {os.path.join(self.folder, r) for r in saved.get("removed", [])}

    def add(self, paths):
        """Add files or folders from anywhere: they stay where they are, only the list keeps them.
        Removed tracks come back when added again (also from the playlist folder)."""
        with self.lock:
            self._load_lists()
            for p in paths:
                p = os.path.abspath(p)
                self.removed = {r for r in self.removed if not (r == p or self._under(r, p))}
                if p not in self.extra and not self._under(p, self.folder):
                    self.extra.append(p)
        self.rescan()
        self.save()

    def remove(self, paths):
        """Take tracks out of the list; the files on disk are not touched. Saved: a rescan does not bring
        them back."""
        with self.lock:
            self._load_lists()
            gone = set(paths)
            for p in gone:
                if p in self.extra:
                    self.extra.remove(p)  # an added file: just forget it
                else:
                    self.removed.add(p)
            self.order = [p for p in self.order if p not in gone]
            self._changed()

    def clear(self):
        """Empty the list: added sources forgotten, folder tracks removed. Files stay on disk; add the
        folder again to bring its tracks back. The track on air plays to its end."""
        with self.lock:
            self._load_lists()
            self.removed |= {p for p in self.order if self._under(p, self.folder)}
            self.extra = []
            self.order = []
            self._idx, self._forced = 0, None
            self._changed()

    def remove_added(self, path):
        """Forget a file or folder added from outside, with its tracks; the files stay on disk."""
        with self.lock:
            self._load_lists()
            if path not in self.extra:
                return False
            self.extra.remove(path)
            self.removed = {r for r in self.removed if not (r == path or self._under(r, path))}
        self.rescan()
        self.save()
        return True

    def _load(self):
        try:
            with open(self.store, encoding="utf-8") as f:
                return json.load(f).get("folders", {})
        except Exception:
            return {}

    def save(self):
        if not self.store:
            return
        with self.lock:
            entry = {"order": [self._key(p) for p in self.order], "t": int(time.time())}
            if self.extra:
                entry["extra"] = list(self.extra)
            if self.removed:
                entry["removed"] = sorted(self._key(p) for p in self.removed)
        folders = self._load()
        folders[self.folder] = entry
        keep = sorted(folders, key=lambda k: folders[k].get("t", 0), reverse=True)[:self.MAX_FOLDERS]
        tmp = self.store + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"folders": {k: folders[k] for k in keep}}, f)
            os.replace(tmp, self.store)
        except OSError as e:
            RNS.log(f"Playlist not saved: {e}", RNS.LOG_WARNING)

    def _load_played(self):
        """({path: last time}, {path: times played}); the file holds [time, times] (old files: time only)."""
        last, plays = {}, {}
        try:
            with open(self.played_store, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return last, plays
        for k, v in data.items():
            if isinstance(v, (int, float)):
                last[k], plays[k] = v, 1
            elif isinstance(v, list) and len(v) == 2:
                last[k], plays[k] = v
        return last, plays

    def mark_played(self, path):
        t = int(time.time())
        with self.lock:
            Playlist.session[path] = t
            self.last_played[path] = t
            self.plays[path] = self.plays.get(path, 0) + 1
            self._changed(save=False)
        if not self.played_store:
            return
        last, plays = self._load_played()  # other folders may have written it
        last[path], plays[path] = t, plays.get(path, 0) + 1
        keep = sorted(last, key=last.get, reverse=True)[:self.MAX_PLAYED]
        tmp = self.played_store + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump({k: [last[k], plays.get(k, 1)] for k in keep}, f)
            os.replace(tmp, self.played_store)
        except OSError as e:
            RNS.log(f"Played times not saved: {e}", RNS.LOG_WARNING)

    def played(self, path):
        """(played since the station started, last played time or None)"""
        return path in Playlist.session, self.last_played.get(path)

    def set_unplayed(self, paths):
        with self.lock:
            for p in paths:
                Playlist.session.pop(p, None)
            self._changed(save=False)

    def _changed(self, save=True):
        self.version += 1
        if save:
            self.save()

    def rescan(self):
        """Read the folder again: saved or current order first, new files after it."""
        saved = self._load().get(self.folder, {})
        with self.lock:
            self._load_lists(saved)
            extra, removed = list(self.extra), set(self.removed)
        try:
            files = list_audio(self.folder)
        except OSError:
            files = []
        for e in extra:  # added from outside: a missing one is skipped, not forgotten (unplugged disk)
            try:
                more = list_audio(e) if os.path.isdir(e) else [e] if os.path.isfile(e) else []
            except OSError:
                more = []
            seen = set(files)
            files += [p for p in more if p not in seen]
        files = [p for p in files if p not in removed]
        with self.lock:
            known = self.order or [os.path.join(self.folder, r) for r in saved.get("order", [])]
            have = set(files)
            kept = [p for p in known if p in have]
            seen = set(kept)
            new = [p for p in files if p not in seen]
            if self.shuffle and not kept:
                random.shuffle(new)
            order = kept + new
            first, self.scanned = not self.scanned, True
            if order != self.order or first:
                self.order = order
                self._changed(save=False)  # saved only when the user changes the order

    def items(self):
        with self.lock:
            return list(self.order)

    def _pick(self):
        if self._forced in self.order:
            p, self._forced = self._forced, None
            return p
        self._forced = None
        i = self.order.index(self.current) + 1 if self.current in self.order else self._idx
        return self.order[i] if i < len(self.order) else None

    def next(self):
        with self.lock:
            p = self._pick()
        if p is None:
            # end of the list (or empty): read the folder again, reshuffle if asked, start over
            self.rescan()
            with self.lock:
                if not self.order:
                    return None
                if self.shuffle:
                    random.shuffle(self.order)
                    self._changed()
                p = self.order[0]
        with self.lock:
            self.current = p
            self._idx = self.order.index(p)
        return p

    def move(self, paths, delta):
        """Move the given tracks up (-1) or down (+1) by one place."""
        with self.lock:
            o = self.order
            idx = sorted((o.index(p) for p in paths if p in o), reverse=delta > 0)
            for i in idx:
                j = i + delta
                if 0 <= j < len(o) and o[j] not in paths:
                    o[i], o[j] = o[j], o[i]
            self._changed()

    def set_order(self, paths):
        with self.lock:
            have = set(self.order)
            order = [p for p in paths if p in have]
            order += [p for p in self.order if p not in set(order)]
            if order != self.order:
                self.order = order
                self._changed()

    def play_next(self, paths):
        """Put the tracks right after the one on air, in the given order."""
        with self.lock:
            rest = [p for p in self.order if p not in paths]
            at = rest.index(self.current) + 1 if self.current in rest else 0
            self.order = rest[:at] + [p for p in paths if p in self.order] + rest[at:]
            self._changed()

    def play_now(self, path):
        with self.lock:
            self._forced = path

    def shuffle_now(self):
        with self.lock:
            random.shuffle(self.order)
            self._changed()

    def sort_now(self, key=None, reverse=False):
        """A-Z (Z-A with reverse), or by key(path): None values last, equal ones A-Z."""
        with self.lock:
            name = lambda p: self.rel(p).lower()
            if key is None:
                self.order.sort(key=name, reverse=reverse)
            else:
                vals = {p: key(p) for p in self.order}
                have = sorted((p for p in self.order if vals[p] is not None), key=name)
                have.sort(key=vals.get, reverse=reverse)  # stable: ties keep A-Z
                self.order = have + sorted((p for p in self.order if vals[p] is None), key=name)
            self._changed()


def _ramp(n, up):
    # equal-power curve: a crossfade keeps the loudness steady
    t = (np.arange(n, dtype=np.float32) + 0.5) / max(1, n)
    return np.sin(t * np.pi / 2) if up else np.cos(t * np.pi / 2)


def _last_loud(x):
    loud = np.flatnonzero(np.abs(x).max(axis=1) > SILENCE) if len(x) else []
    return int(loud[-1]) if len(loud) else -1


BEATMIX_MAX = 0.08      # tempo change allowed to match two tracks (+-8%)
BEATMIX_RAMP = (4, 4)   # after the mix, back to its own tempo in 4 steps of 4 beats
BAR = 4                 # beats per bar: mixes start on a bar of the outgoing track
BEATMIX_AHEAD = 6       # s queued before a beatmix change of track (analysis, test render, next ffmpeg start)
BEATMIX_FADE = 0.5      # share of the mix window the incoming takes to come up, and the outgoing to go down
ATEMPO_LAG = 1024 / SAMPLE_RATE  # s ffmpeg atempo skips after its first window (measured: 1024 samples)
SKIP_LEAD = 2           # s: Next track with beatmix mixes on the first bar at least this far ahead
SEEK_FADE = int(0.02 * 48000)  # samples: the jump of a seek fades out and in over this
SEEK_BEFORE_MIX = 4     # s: a seek to the very end stops this far before the beatmix window, which then plays


def _ramp_power(n, up):
    # beatmix: kicks in phase add up, so gains sum to 1 (sin^2 + cos^2) instead of equal power
    return _ramp(n, up) ** 2


def _octave_bpm(bpm, target):
    """bpm, its double or its half: the one nearest to target (a detector can be an octave off)."""
    return min((bpm / 2, bpm, bpm * 2), key=lambda b: abs(np.log(b / target)))


LOCAL_MIN = 0.1  # BPM: a local tempo this far from the track one replaces it (closer: the 120 s measure is finer)


def _local_bpm(x, sr, bpm):
    """Tempo measured on the mix window itself when it clearly differs from the track BPM (DJ sets), else None."""
    try:
        loc, conf = bpmdet.local_tempo(x, sr, bpm)
    except Exception:
        return None
    if loc and conf >= bpmdet.LOCAL_CONF and abs(loc - bpm) > LOCAL_MIN:
        return loc
    return None


PHASE_CLEAR = bpmdet.KICK_CLEAR  # beat clarity needed to trust the phase measured on the mix region
PHASE_SECS = 30    # s of audio measured at the start of the incoming track


def _wrap_ms(d, p):
    """Time difference d (s) folded into one beat p, in ms from -p/2 to +p/2."""
    return round(((d + p / 2) % p - p / 2) * 1000, 1)


class _Pending:
    """FIFO of decoded samples: push at the end, take from the front, amortized copies."""

    def __init__(self):
        self.a = np.zeros((1 << 16, CHANNELS), np.float32)
        self.s = self.e = 0

    def __len__(self):
        return self.e - self.s

    def push(self, x):
        live = self.e - self.s
        if self.e + len(x) > len(self.a):
            if live + len(x) <= len(self.a) // 2:
                self.a[:live] = self.a[self.s:self.e]
            else:
                b = np.zeros((2 * (live + len(x)), CHANNELS), np.float32)
                b[:live] = self.a[self.s:self.e]
                self.a = b
            self.s, self.e = 0, live
        self.a[self.e:self.e + len(x)] = x
        self.e += len(x)

    def take(self, n):
        v = self.a[self.s:self.s + n].copy()
        self.s += len(v)
        return v

    def array(self):
        return self.a[self.s:self.e].copy()


def _intro_beat(path, bpm, beat, limit=60, diag=None, n=0):
    """(file second where a beatmixed track starts: the beat just before its music begins, BPM of the intro).
    The grid and tempo from the middle of the track are checked on the intro itself. None if silent.
    diag (dict) gets what was measured, for the mix log."""
    sr = bpmdet.SR
    try:
        out = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-t", str(limit), "-i",
                              path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
                             capture_output=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return None
    x = np.frombuffer(out[:len(out) // 4 * 4], np.float32)
    loud = np.flatnonzero(np.abs(x) > SILENCE)
    if not len(loud):
        return None
    first = float(loud[0]) / sr
    p = 60 / bpm
    a = int(first * sr)
    secs = min(PHASE_SECS, max(8.0, n * p))  # the beats that go into the mix
    loc = _local_bpm(x[a:a + int(secs * sr)], sr, bpm)
    if diag is not None:
        diag["in_local_bpm"] = round(loc, 3) if loc else None
    if loc:
        bpm, p = loc, 60 / loc
        secs = min(PHASE_SECS, max(8.0, n * p))
    ph, clear = bpmdet.beat_phase(x[a:a + int(secs * sr)], sr, bpm)
    if diag is not None:
        diag.update(in_first=round(first, 3), in_clear=round(clear, 2), in_shift_ms=_wrap_ms(first + ph - beat, p),
                    in_measured=clear >= PHASE_CLEAR)
    if clear >= PHASE_CLEAR:
        beat = first + ph  # measured here: wins over the grid of the middle
    s0 = beat + np.floor((first + 0.03 - beat) / p) * p
    return (s0 + p if s0 < 0 else s0), bpm


LOOP_CHOICES = (4, 8, 16)  # beatmix loop out / loop in length in beats
LOOP_TIMES = (4, 8, 16)    # times the loop plays
LOOP_SEAM = int(0.005 * SAMPLE_RATE)  # crossfade at each restart of a loop: no click at the jump


def _loop_start(x, k, times):
    """x played from its start: the first k samples times over, then x goes on from k (seamless, x[k:] follows)."""
    if len(x) <= k:
        return x  # shorter than its loop
    return np.concatenate((_looped(x[:k], k * times, x[k:]), x[k:]))


def _looped(seg, n, cont):
    """seg played over and over for n samples; each restart fades in over what really followed seg (cont)."""
    k = len(seg)
    y = np.tile(seg, (n // k + 1, 1))[:n].copy()
    f = min(LOOP_SEAM, len(cont), k)
    if f:
        up = np.linspace(0.0, 1.0, f, dtype=np.float32)[:, None]
        for s in range(k, n, k):
            m = min(f, n - s)
            y[s:s + m] = cont[:m] * (1 - up[:m]) + seg[:m] * up[:m]
    return y


ALIGN_BEATS = 32   # beats of the incoming track rendered to line its kicks up with the outgoing ones
ALIGN_MATCH = 0.75  # 3 of 4 quarters of the window must agree (noise: 1 at most)
ALIGN_AGAIN = 0.015  # s: a correction bigger than this is tested once more
ALIGN_PASSES = 2
ALIGN_REF_OFF = 0.04  # s: outgoing kicks of the test window this far from the mix point = test not trusted


def _track_cmd(path, plan=None, seconds=None):
    """ffmpeg reading a track as s16le stereo: whole, or beatmixed (plan = s0, beat, tempos) from s0.
    seconds: stop the output there (the test render of the start of a mix)."""
    sr = SAMPLE_RATE
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if plan:
        s0, p, tempos = plan
        # atempo puts everything after its first window ATEMPO_LAG early: start that much before s0
        lead = ATEMPO_LAG * tempos[0]
        ss, pad = (s0 - lead, 0.0) if s0 >= lead else (0.0, lead - s0)
        cmds = [f"{lead + j * p:.6f} atempo tempo {(tempos[j] if j < len(tempos) else 1.0):.6f}"
                for j in range(1, len(tempos) + 1) if (tempos[j] if j < len(tempos) else 1.0) != tempos[j - 1]]
        # few tempo commands: each one costs atempo a little drift
        send = f"asendcmd=c='{';'.join(cmds)}'," if cmds else ""  # same BPM: no commands, ffmpeg refuses an empty one
        delay = f"adelay=delays={pad * 1000:.3f}:all=1," if pad else ""
        af = (f"asetpts=PTS-STARTPTS,aresample={sr},{delay}asetnsamples=n=64:p=0,{send}"
              f"atempo={tempos[0]:.6f}")
        cmd += ["-ss", f"{ss:.4f}", "-i", path, "-vn", "-af", af]
    else:
        cmd += ["-i", path, "-vn"]
    if seconds:
        cmd += ["-t", f"{seconds:.4f}"]
    return cmd + ["-f", "s16le", "-ac", str(CHANNELS), "-ar", str(sr), "pipe:1"]


class _Grid:
    """Beat grid of the track on air, to place the next mix on its beats.
    start = output sample where track second t0 is heard; tempos = per beat from t0 (beatmixed in), then 1;
    extra = s of output a loop in added before the track went on (every beat is heard that much later)."""

    def __init__(self, start, t0, beat, p, tempos=(), extra=0.0):
        self.start, self.t0, self.beat, self.p, self.tempos = start, t0, beat, p, list(tempos)
        self.extra = extra

    def first_index(self):
        # first beat at or after t0 (the beat t0 itself when the track was beatmixed in)
        return int(np.ceil((self.t0 - self.beat) / self.p - 0.01))

    def tempo_at(self, k):
        j = k - self.first_index()
        return self.tempos[j] if 0 <= j < len(self.tempos) else 1.0

    def offset(self, k):
        """Seconds after start that beat k is heard."""
        k0 = self.first_index()
        b0 = self.beat + k0 * self.p - self.t0  # first beat after t0, in file seconds
        j = k - k0
        if not self.tempos or j <= 0:
            return self.extra + b0 + j * self.p
        t = self.tempos[:j]
        return self.extra + b0 + sum(self.p / x for x in t) + (j - len(t)) * self.p


def probe_duration(path):
    """Track length in seconds with ffprobe, None if unknown."""
    return probe_track(path)[0]


def tag_title(tags, name):
    """The file name, with the artist from the tags in front when the name lacks it ('Artist - Title')."""
    t = {str(k).lower(): str(v).strip() for k, v in (tags or {}).items() if str(v).strip()}
    artist = t.get("artist") or t.get("album_artist") or ""
    if not artist or artist.lower() in name.lower():
        return name
    return f"{artist} - {t.get('title') or name}"


def probe_track(path):
    """(length in s or None, tags dict) with one ffprobe."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags",
                              "-of", "json", path], capture_output=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        fmt = json.loads(out.decode("utf-8", "replace") or "{}").get("format") or {}
        d = float(fmt.get("duration") or 0)
        return (d if d > 0 else None), fmt.get("tags") or {}
    except Exception:
        return None, {}


class Source:
    """ffmpeg -> 48 kHz stereo PCM frames; stream URL or folder playlist.
    Folder: fade, mix (crossfade, wins over fade between tracks) in seconds, 0 = off; trim = cut silence;
    beats = beatmix over that many beats when both tracks have a BPM (bpm_of(path) -> {'bpm', 'beat'}), else mix.
    loop_out / loop_in: in a beatmix the outgoing track plays its loop_n beats from the mix point loop_times times
    (or until the mix ends) / the incoming one its first loop_n beats loop_times times, then goes on."""

    def __init__(self, url=None, folder=None, shuffle=False, fade=0, mix=0, trim=False, playlist=None,
                 beats=0, bpm_of=None, mix_log=None, norm=None, loop_out=False, loop_in=False, loop_n=8,
                 loop_times=4):
        self.url, self.folder, self.shuffle = url, folder, shuffle
        self.fade, self.mix, self.trim = fade, mix, trim
        self.beats, self.bpm_of = beats, bpm_of
        self.loop_out, self.loop_in, self.loop_n, self.loop_times = loop_out, loop_in, loop_n, loop_times
        self.on_track = None  # called with the name of each folder track that starts
        self._grid = None  # beat grid of the track on air
        self._tail = None  # outgoing track waiting under the cursor for a beatmix
        self.last_mix = ""  # how the last change of track went, for the log/tests
        self.mix_log = mix_log  # file: one JSON line per change of track with beatmix on
        self.norm = norm  # () -> target LUFS or None: each track gets the gain to reach it (loudness from bpm_of)
        self._gain = 1.0  # gain of the track being added
        self._out_diag = None  # why the track that ended left no beatmix tail
        self._check = None  # beatmix being read: both tracks kept to measure how their kicks line up
        self.mix_window = None  # (start, end) monotonic s of the mix being heard
        self.mix_paths = ()  # the two files of that mix
        self.playlist = playlist  # folder: Playlist shared with the GUI, made here if None
        # folder: _flush paces the queue (up to BEATMIX_AHEAD s at a change of track), a bounded deque would
        # silently drop the oldest frames there = a jump in the music
        self.frames = deque() if folder else deque(maxlen=MAX_QUEUE_FRAMES)
        # folder frames are stored 12 dB down: two full tracks mixed or a normalized track can go over
        # 0 dBFS, and only the limiter after them may bring that back (pcm_frame undoes it)
        self.headroom = FOLDER_HEADROOM if folder else 1.0
        self.now_playing = ""
        self.now_path = None  # folder: file on air now
        self.state = "starting"
        self.running = True
        self.proc = None
        self._skip = False
        self._seek = None  # s in the track on air asked by the station bar
        self.stream_name = ""  # icy-name of a stream URL
        self.stream_title = ""  # its StreamTitle (track), when the server sends it
        self.track_dur = None  # s, folder tracks only
        self._track_t0 = None  # monotonic time the track is heard at second track_lead
        self._track_lead = 0.0
        threading.Thread(target=self._loop, daemon=True).start()

    def _probe_loop(self):
        # icy-name and StreamTitle (Icecast/Shoutcast), not every server sends them: one short read every 20 s
        while self.running:
            try:
                out = subprocess.run(["ffprobe", "-v", "error", "-user_agent", UA, "-show_entries",
                                      "format_tags=icy-name,StreamTitle", "-of", "json", self.url],
                                     capture_output=True, text=True, timeout=20,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                tags = (json.loads(out or "{}").get("format") or {}).get("tags") or {}
                self.stream_name = str(tags.get("icy-name") or self.stream_name).strip()[:64]
                title = str(tags.get("StreamTitle") or "").strip().strip("-").strip()[:64]
                if title != self.stream_title:
                    self.stream_title = title
                    self.now_playing = title or "live stream"
            except Exception:
                pass
            for _ in range(20):
                if not self.running:
                    return
                time.sleep(1)

    def live_text(self):
        """Station display line for live sources."""
        if not self.url:
            return ""
        return f"HTTP STREAM: {self.stream_name or self.url}" + (f" - {self.stream_title}" if self.stream_title else "")

    def track_progress(self, clamp=True):
        """(position, duration) in seconds of what goes on air now, None if not a file."""
        if not self.folder or not self.track_dur or self._track_t0 is None:
            return None
        pos = self._track_lead + time.monotonic() - self._track_t0
        return (max(0.0, min(self.track_dur, pos)) if clamp else min(self.track_dur, pos)), self.track_dur

    def _track_started(self, name, lead, path=None):
        # the track sits after what is still queued: output buffer + frame queue
        wait = (self._cur - self._flushed) / SAMPLE_RATE + len(self.frames) * FRAME_MS / 1000
        self.now_playing, self.now_path, self.state = name, path, "playing"
        self.track_dur, self._track_t0, self._track_lead = self._next_dur, time.monotonic() + wait, lead
        if path and self.playlist:
            self.playlist.mark_played(path)
        if self.on_track:
            self.on_track(name)

    def _loop(self):
        if self.folder:
            return self._folder_loop()
        threading.Thread(target=self._probe_loop, daemon=True).start()
        extra = ["-user_agent", UA, "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
        while self.running:
            self.now_playing = self.stream_title or "live stream"
            self.state = "connecting"
            cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *extra, "-i", self.url,
                   "-f", "s16le", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "pipe:1"]
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                while self.running:
                    chunk = self.proc.stdout.read(FRAME_BYTES)
                    if len(chunk) < FRAME_BYTES:
                        break
                    self.state = "playing"
                    self.frames.append(chunk)
                self.proc.kill()
            except FileNotFoundError:
                self.state = "ffmpeg not found"
                RNS.log("ffmpeg not found in PATH", RNS.LOG_ERROR)
                time.sleep(10)
            except Exception as e:
                RNS.log(f"Source error: {e}", RNS.LOG_ERROR)
            if self.running:
                self.state = "reconnecting"
                RNS.log("Stream ended, reconnecting in 3 s", RNS.LOG_WARNING)
                time.sleep(3)

    # folder mode: ffmpeg decodes ahead, we pace on the output queue and overlap-add the tracks
    def _folder_loop(self):
        self._out = np.zeros((0, CHANNELS), np.float32)
        self._cur = 0      # where the next track sample is added; before it the output is final
        self._overlap = 0  # samples of the previous track still under the cursor (crossfade)
        self._flushed = 0  # output samples already sent
        if self.playlist is None:
            self.playlist = Playlist(self.folder, shuffle=self.shuffle)
        pl = self.playlist
        fails = 0
        while self.running:
            pl.shuffle = self.shuffle  # the GUI changes it live
            f = pl.next()
            if f is None:
                self.state = "no audio files"
                time.sleep(10)
                continue
            if self._play_file(f):
                fails = 0
            else:
                fails += 1
                if fails >= len(pl.order) and self.running:
                    self.state = "no playable files"
                    fails = 0
                    time.sleep(10)

    def _mark_mix(self, n, path=None):
        # the mix is heard after what is still queued: n samples from the cursor
        start = time.monotonic() + (self._cur - self._flushed) / SAMPLE_RATE + len(self.frames) * FRAME_MS / 1000
        self.mix_window = (start, start + n / SAMPLE_RATE)
        self.mix_paths = tuple(p for p in (self.now_path, path) if p)

    def mixing(self):
        """True while two tracks are heard together (beatmix or crossfade)."""
        w = self.mix_window
        return bool(w and w[0] <= time.monotonic() <= w[1])

    def mixing_paths(self):
        """The two files heard together now, () when no mix is heard."""
        return self.mix_paths if self.mixing() else ()

    def _track_gain(self, path):
        """Linear gain that brings the track to the Normalize target, 1.0 when off or loudness unknown."""
        try:
            target = self.norm() if callable(self.norm) else self.norm
            lufs = (self.bpm_of(path) or {}).get("lufs") if target is not None and self.bpm_of else None
        except Exception:
            return 1.0
        if lufs is None:
            return 1.0
        return float(10 ** (norm_gain_db(target, lufs) / 20))

    def _bpm_info(self, path):
        try:
            r = self.bpm_of(path) if self.bpm_of else None
        except Exception:
            r = None
        if r and r.get("bpm") and r.get("beat") is not None:
            return float(r["bpm"]), float(r["beat"])
        return None

    def _write_mix_log(self, d):
        if not self.mix_log:
            return
        try:
            with open(self.mix_log, "a", encoding="utf-8") as f:
                # numpy values (np.bool_, np.float64) as plain ones
                f.write(json.dumps(dict(d, t=time.strftime("%Y-%m-%d %H:%M:%S")), ensure_ascii=False,
                                   default=lambda v: v.item() if hasattr(v, "item") else str(v)) + "\n")
        except Exception as e:
            RNS.log(f"Mix log not saved: {e}", RNS.LOG_WARNING)

    def _log_mix(self, d, ok):
        """One line per change of track with beatmix on: what was planned, or why it fell back to mix/fade."""
        a, b = d.get("from", "?"), d.get("to", "?")
        if not ok:
            RNS.log(f"Beatmix {a} -> {b}: not possible, {d.get('why', '?')}", RNS.LOG_NOTICE)
            self._write_mix_log(dict(d, ok=False))
            return
        out = (f"outro beat measured {d.get('out_shift_ms', 0):+.0f} ms from the grid" if d.get("out_measured")
               else "outro beat from the grid (not clear)") + f", clarity {d.get('out_clear', 0):.2f}"
        inn = (f"intro beat measured {d.get('in_shift_ms', 0):+.0f} ms from the grid" if d.get("in_measured")
               else "intro beat from the grid (not clear)") + f", clarity {d.get('in_clear', 0):.2f}"
        for k, what in (("out_local_bpm", "outro"), ("in_local_bpm", "intro")):
            if d.get(k):
                inn += f"; {what} measured at {d[k]:.2f} BPM"
        RNS.log(f"Beatmix {a} -> {b}: {d.get('n')} beats at {d.get('heard', 0):.2f} BPM, {b} {d.get('to_bpm', 0):.2f} "
                f"BPM stretched x{d.get('r', 1):.4f}; {out}; {inn}", RNS.LOG_NOTICE)

    def _measure_mix(self, c):
        """Kicks of the two tracks over the mix, measured apart: + = the new one comes late. Start and end
        of the window tell a phase error (same offset) from a tempo error (offset that grows)."""
        d = c["d"]
        try:
            out, inn = c["out"], np.concatenate(c["in"])
            n = min(len(out), len(inn))
            bpm = d.get("heard") or 120
            p = 60 / bpm
            half = n // 2

            def off(a, b):
                po, co = bpmdet.beat_phase(out[a:b], SAMPLE_RATE, bpm)
                pi, ci = bpmdet.beat_phase(inn[a:b], SAMPLE_RATE, bpm)
                return _wrap_ms(pi - po, p), round(min(co, ci), 2)

            (s, cs), (e, ce) = off(0, half), off(half, n)
            d.update(start_ms=s, end_ms=e, start_clear=cs, end_clear=ce)
            verdict = ("not clear (few kicks)" if min(cs, ce) < 0.15 else
                       "in time" if max(abs(s), abs(e)) <= 15 else
                       "drifting (tempo)" if abs(e - s) > 15 else "off by the same amount (phase)")
            d["verdict"] = verdict
            RNS.log(f"Beatmix {d.get('from', '?')} -> {d.get('to', '?')} heard: kicks {s:+.0f} ms at the start, "
                    f"{e:+.0f} ms at the end (clarity {cs:.2f}/{ce:.2f}), {verdict}", RNS.LOG_NOTICE)
        except Exception as e:
            d["measure_error"] = str(e)
        self._write_mix_log(dict(d, ok=True))

    def _resolve_tail(self, fade, mix):
        """No beatmix into the next track: the outgoing tail ends like a normal mix or fade."""
        tl, self._tail = self._tail, None
        end = tl["end"] - self._flushed
        ov = min(mix or fade, end)
        if ov > 0:
            self._out[end - ov:end] *= _ramp(ov, False)[:, None]
        self._overlap = ov if mix else 0
        self._cur = tl["end"] - self._overlap

    def _commit_beatmix(self, n, beat):
        """The outgoing tail plays n samples from the mix point, going down in the last half of them, then
        silence. Returns the fade in of the incoming track (the first half)."""
        tl, self._tail = self._tail, None
        a, end = tl["at"] - self._flushed, tl["end"] - self._flushed
        n = max(0, min(n, end - a))
        fo = int(n * BEATMIX_FADE)
        self._out[a + n - fo:a + n] *= _ramp_power(fo, False)[:, None]
        self._out[a + n:end] = 0
        self._cur, self._overlap = tl["at"], 0
        return int(n * BEATMIX_FADE)

    def _loop_tail(self, tl, beats, times):
        """Loop out: from the mix point the outgoing track plays its first beats `times` times, then goes on from
        the end of the loop until the mix ends (its window in the buffer is rewritten)."""
        sr = SAMPLE_RATE
        pb = 60 / tl["bpm"] * sr
        a = tl["at"] - self._flushed
        win = min(int(round(tl["n"] * pb)), len(self._out) - a)
        k = int(round(beats * pb))
        if k <= LOOP_SEAM or k >= win:
            return
        y = _loop_start(self._out[a:a + win].copy(), k, times)
        self._out[a:a + win] = y[:win]

    def _align_in(self, path, tl, plan, d, loop_w=0, times=1):
        """Closed loop before a beatmix: the start of the incoming track is rendered with the very chain that
        will play it (looped too, loop_w samples, if it loops in), its kicks are lined up with the outgoing window
        already in the buffer, s0 is moved by what is left. Whatever the seek, atempo or the beat detection got
        wrong is taken out here."""
        sr = SAMPLE_RATE
        p_heard = 60 / tl["bpm"]
        nb = min(tl["n"], ALIGN_BEATS)
        w = int(nb * p_heard * sr)
        a = tl["at"] - self._flushed
        ref = self._out[a:a + w].mean(axis=1)
        need = max(loop_w + LOOP_SEAM, w - (times - 1) * loop_w) if loop_w else w
        # the mix point sits on the outgoing kick: a window whose loudest hit is elsewhere (an off-beat bass
        # louder than the kick) would line the new kicks up with that bass
        po, _ = bpmdet.beat_phase(ref, sr, tl["bpm"])
        off = (po + p_heard / 2) % p_heard - p_heard / 2
        if abs(off) > ALIGN_REF_OFF:
            d.update(align_ref_ms=round(off * 1000, 1), align_used=False)
            RNS.log(f"Beatmix {d.get('from', '?')} -> {d.get('to', '?')}: outgoing beat not on the mix point "
                    f"({off * 1000:+.0f} ms, off-beat bass?), test left out", RNS.LOG_NOTICE)
            return plan
        # a big correction moves what the loop in repeats too: test-render once more then
        for k in range(ALIGN_PASSES):
            try:
                raw = subprocess.run(_track_cmd(path, plan, need / sr), capture_output=True,
                                     timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            except Exception as e:
                d["align"] = f"render failed: {e}"
                return plan
            x = np.frombuffer(raw[:len(raw) // 4 * 4], np.int16).reshape(-1, CHANNELS).mean(axis=1) / 32768
            if loop_w:
                x = _loop_start(x[:, None], loop_w, times)[:w, 0]
            lag, match = bpmdet.align(ref, x, sr, tl["bpm"])
            ok = match >= ALIGN_MATCH
            tag = "" if k == 0 else "2"
            d.update({f"align{tag}_ms": round(float(lag) * 1000, 1), f"align{tag}_match": round(float(match), 2),
                      f"align{tag}_used": bool(ok)})
            RNS.log(f"Beatmix {d.get('from', '?')} -> {d.get('to', '?')}: incoming kicks {lag * 1000:+.0f} ms against "
                    f"the outgoing ones, match {match:.2f}" + (", corrected" if ok else ", left as is")
                    + (" (second test)" if k else ""), RNS.LOG_NOTICE)
            if not ok:
                return plan
            s0, p, tempos = plan
            # late by lag in heard time = lag * tempo in the file: start that much further in
            plan = s0 + lag * tempos[0], p, tempos
            if abs(lag) <= ALIGN_AGAIN:
                break
        return plan

    def _plan_tail(self, g, pos, tail, n, diag=None):
        """Mix point for the next track: a bar of this one with n beats of music after it.
        tail = the last samples of the track, after pos. (sample offset from g.start, n, BPM heard there) or None.
        diag (dict) gets what was measured, for the mix log."""
        if diag is None:
            diag = {}
        if n <= 0:
            return None
        sr = SAMPLE_RATE
        end_s = (pos + len(tail)) / sr
        lo = (self._flushed - g.start) / sr  # already sent: too late to mix there
        k0 = g.first_index()
        kmax = k0 + int(end_s / g.p * (1 + BEATMIX_MAX)) + 2
        diag.update(out_clear=0.0, out_measured=False, out_shift_ms=0.0)
        mono = tail.mean(axis=1)
        for k in range(kmax, k0 - 1, -1):
            if (k - k0) % BAR:
                continue
            o = g.offset(k)
            p_out = g.p / g.tempo_at(k)
            if o + n * p_out > end_s + p_out / 2 or o < pos / sr:
                continue
            # kick attack measured on the mix window itself: the grid of the middle of the track is not enough
            a = int(round(o * sr)) - pos
            loc = _local_bpm(mono[a:a + int(n * p_out * sr)], sr, 60 / p_out)
            diag["out_local_bpm"] = round(loc, 3) if loc else None
            if loc:
                p_out = 60 / loc
                if o + n * p_out > end_s + p_out / 2:
                    continue
            # a break in the window can blur the whole: then its start, where the outgoing track is still full
            for m in (n, n // 2, n // 4):
                if m < min(n, 8):
                    break
                ph, clear = bpmdet.beat_phase(mono[a:a + int(m * p_out * sr)], sr, 60 / p_out)
                if clear >= PHASE_CLEAR:
                    break
            diag.update(out_clear=round(clear, 2), out_measured=False, out_shift_ms=0.0)
            if clear >= PHASE_CLEAR:
                b = a / sr + pos / sr + ph
                o2 = b + round((o - b) / p_out) * p_out
                diag.update(out_measured=True, out_shift_ms=round((o2 - o) * 1000, 1))
                o = o2
            if o + n * p_out > end_s + 0.001:
                continue
            if o < max(0.0, lo):
                diag["why"] = "mix point already sent"
                return None
            return int(round(o * sr)), n, 60 / p_out
        diag["why"] = f"no bar with {n} beats left in the track"
        return None

    def _play_file(self, path):
        self._check = None  # a mix into a track too short to read it all is not measured
        self._seek = None   # a seek asked for the track before does not move this one
        self._gain = self._track_gain(path)
        sr = SAMPLE_RATE
        fade = int(max(0, self.fade) * sr)
        mix = int(max(0, self.mix) * sr)
        beats = int(max(0, self.beats or 0))
        trim = self.trim
        info = self._bpm_info(path) if beats else None
        tl, plan = self._tail, None
        name = os.path.splitext(os.path.basename(path))[0]
        od, self._out_diag = self._out_diag, None
        d = dict((tl or {}).get("diag") or od or {}, to=name, to_bpm=info[0] if info else None)
        if tl:
            d.update(heard=round(tl["bpm"], 2), n=tl["n"])
        lb = int(self.loop_n or 0) if tl else 0
        times = max(1, int(self.loop_times or 1))
        lout = lb if self.loop_out and lb <= (tl or {}).get("n", 0) else 0  # the loop must be in the mix window
        lin = lb if self.loop_in and times > 1 else 0
        # the incoming stream keeps tempo r while the outgoing one is heard: through its loop and what is left
        in_beats = (lin + max(0, tl["n"] - lin * times)) if lin else (tl or {}).get("n", 0)
        if tl and info:
            eff = _octave_bpm(info[0], tl["bpm"])
            r = tl["bpm"] / eff
            d.update(to_bpm=round(eff, 2), r=round(r, 4))
            ib = _intro_beat(path, eff, info[1], diag=d, n=tl["n"]) if abs(r - 1) <= BEATMIX_MAX else None
            if ib and abs(tl["bpm"] / ib[1] - 1) > BEATMIX_MAX:
                d["why"] = f"intro at {ib[1]:.2f} BPM vs {tl['bpm']:.2f}"
                ib = None
            s0 = ib[0] if ib else None
            if ib:
                eff = ib[1]
                r = tl["bpm"] / eff
                d.update(to_bpm=round(eff, 2), r=round(r, 4))
                p = 60 / eff
                steps, per = BEATMIX_RAMP
                tempos = [r] * in_beats + [r + (1 - r) * (s + 1) / (steps + 1) for s in range(steps)
                                           for _ in range(per)]
                plan = (s0, p, tempos)
                d.update(s0=round(s0, 3))
            elif "why" not in d:
                d["why"] = (f"tempo {tl['bpm']:.2f} vs {eff:.2f} BPM, {abs(r - 1) * 100:.1f}% > {BEATMIX_MAX * 100:.0f}%"
                            if abs(r - 1) > BEATMIX_MAX else "no music in the intro")
        elif tl:
            d["why"] = f"no BPM for {name}"
        if tl and not plan:
            self._resolve_tail(fade, mix)
        if beats and od is not None or tl:
            self._log_mix(d, plan is not None)
        hold = mix or fade  # the end of the track is only known at EOF: keep this much back
        if info:
            p = plan[1] if plan else 60 / info[0]
            hold = max(hold, int((beats + BAR + 1) * p * (1 + BEATMIX_MAX) * sr + BEATMIX_AHEAD * sr) + FRAME_SAMPLES)
        keep_sil = SILENCE_KEEP * sr
        lin_w = loop_extra = 0  # samples of the incoming loop (0 = no loop in), output it adds before the track goes on
        if plan:
            if lout and times > 1:
                self._loop_tail(tl, lout, times)
            lin_w = int(round(lin * 60 / tl["bpm"] * sr)) if lin else 0
            loop_extra = (times - 1) * lin_w
            d.update(loop_out=lout if times > 1 else 0, loop_in=lin, loop_times=times)
            plan = self._align_in(path, tl, plan, d, lin_w, times)
        cmd = _track_cmd(path, plan)
        try:
            self.proc = proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError:
            self.state = "ffmpeg not found"
            RNS.log("ffmpeg not found in PATH", RNS.LOG_ERROR)
            time.sleep(10)
            return False
        self._next_dur, tags = probe_track(path)
        shown = tag_title(tags, name)
        if plan:
            mix_at, win = tl["at"], int(round(tl["n"] * 60 / tl["bpm"] * sr))
            a = mix_at - self._flushed
            # both tracks as heard in the mix, unfaded: their kicks are compared once the window is read
            self._check = {"d": d, "need": win, "out": self._out[a:a + win].mean(axis=1).copy(), "in": [], "got": 0}
            fade_in = self._commit_beatmix(win, int(60 / tl["bpm"] * sr))
            fin = _ramp_power(fade_in, True)
            self._mark_mix(win, path)
            # looped in: the last time round the loop is where the track really goes on
            self._grid = _Grid(mix_at, plan[0], info[1], plan[1], plan[2], loop_extra / sr)
            self.last_mix = f"beatmix {tl['n']} beats at {tl['bpm']:.2f} BPM, tempo x{plan[2][0]:.4f}"
        else:
            fade_in = self._overlap or fade
            fin = _ramp(fade_in, True)
            self._grid = None
            if tl:
                self.last_mix = "mix" if self._overlap else ("fade" if fade else "cut")
            if self._overlap:
                self._mark_mix(self._overlap, path)
        lead_trim = trim and not plan  # a beatmixed track starts on its beat
        cut_gaps = trim and not info   # the beat grid needs the track in one piece
        acc = _Pending()  # decoded, not yet added: grows without copying it all at every frame
        pend = None       # the same as an array, once the reading loop is over
        pos = 0          # track samples already added to the output
        started = False  # first audible sample seen (trim)
        silent = 0       # silent samples at the end of what we read
        lead = 0         # silent samples cut at the start
        skipped = False
        skip_at = None   # Next track with beatmix: mix point, s from the track start on air
        seek_in = 0      # samples of fade in after a seek

        def read_more(pend, need):
            raw = proc.stdout.read(need * CHANNELS * 2) if need > 0 else b""
            raw = raw[:len(raw) // 4 * 4]
            if not raw:
                return pend
            return np.concatenate((pend, np.frombuffer(raw, np.int16).reshape(-1, CHANNELS).astype(np.float32) / 32768))

        def begin():
            if info and not plan:
                self._grid = _Grid(self._cur, lead / sr, info[1], 60 / info[0])
            # a loop in holds the track at its start: the bar counts from that much earlier
            self._track_started(shown, (plan[0] - loop_extra / sr * plan[2][0] if plan else lead / sr), path)

        lbuf = []  # loop in: the start of the incoming track, gathered until the loop and its seam are read

        def loop_in_now():
            seg = np.concatenate(lbuf)
            lbuf.clear()
            return _loop_start(seg, lin_w, times)

        try:
            while self.running:
                if self._skip:
                    if lbuf:
                        acc.push(loop_in_now())
                    self._skip, skipped = False, True
                    # settings of now, even if changed during this track
                    fade, mix = int(max(0, self.fade) * sr), int(max(0, self.mix) * sr)
                    g, bn = self._grid, int(max(0, self.beats or 0))
                    pend = acc.array()
                    if g and bn and pos:
                        # beatmix on the first bar a little ahead: this track ends after the mix beats
                        lo = (self._flushed - g.start) / sr + SKIP_LEAD
                        k = g.first_index()
                        while g.offset(k) < lo:
                            k += BAR
                        skip_at = g.offset(k)
                        p_out = g.p / g.tempo_at(k)
                        need = int((skip_at + bn * p_out + p_out / 2) * sr) - pos
                        pend = read_more(pend, need - len(pend))[:max(0, need)]
                    else:
                        pend = read_more(pend, (mix or fade) - len(pend))
                        pend = pend[:mix or fade]  # a beatmix holds back much more: skip now
                    break  # what is in pend fades out
                if self._seek is not None:
                    t, self._seek = self._seek, None
                    p2 = self._seek_to(t, path, name, info, pos, fin, lin_w)
                    if p2:
                        proc, acc, silent, seek_in = p2, _Pending(), 0, SEEK_FADE
                raw = proc.stdout.read(FRAME_BYTES)
                if len(raw) < 4:
                    break
                x = np.frombuffer(raw[:len(raw) // 4 * 4], np.int16).reshape(-1, CHANNELS).astype(np.float32) / 32768
                if seek_in:
                    k, seek_in = min(seek_in, len(x)), 0
                    x[:k] *= _ramp(k, True)[:, None]
                if lin_w:
                    lbuf.append(x)
                    if sum(len(b) for b in lbuf) < lin_w + LOOP_SEAM:
                        continue
                    x, lin_w = loop_in_now(), 0
                if trim:
                    loud = _last_loud(x)
                    if not started and lead_trim:
                        if loud < 0:
                            lead += len(x)
                            continue
                        first = int(np.flatnonzero(np.abs(x).max(axis=1) > SILENCE)[0])
                        lead += first
                        x = x[first:]
                        loud -= first
                    if loud < 0:
                        silent += len(x)
                        if silent > keep_sil and cut_gaps:
                            continue  # long gap inside the track: cut
                    else:
                        silent = len(x) - 1 - loud
                started = True
                acc.push(x)
                n = len(acc) - hold - (min(silent, keep_sil) if trim else 0)
                if n >= FRAME_SAMPLES:
                    if pos == 0:
                        begin()
                    pos = self._add(acc.take(n), pos, fin)
                    if not self._flush():
                        return True
        finally:
            try:
                proc.kill()
            except Exception:
                pass
        if lbuf:
            acc.push(loop_in_now())
            started = True
        if pend is None:
            pend = acc.array()
        if not started:
            if plan:
                self._cur = max(self._cur, mix_at + win)  # the faded tail stays, nothing under it
            return False
        if trim and skip_at is None:
            pend = pend[:_last_loud(pend) + 1]
        if pos == 0 and len(pend):
            begin()
        g, beats = self._grid, int(max(0, self.beats or 0))
        tail = None
        if g and beats and (not skipped or skip_at is not None):
            # what comes before any possible mix point goes out now, with a deeper queue: the work at the
            # change of track (phase, next intro, ffmpeg start) must not leave the listeners without audio
            whole, pos0 = pend, pos
            if skip_at is None:
                safe = len(pend) - int((beats + BAR + 1) * g.p * (1 + BEATMIX_MAX) * sr)
            else:
                safe = int((skip_at - g.p) * sr) - pos
            if safe >= FRAME_SAMPLES:
                pos = self._add(pend[:safe], pos, fin)
                pend = pend[safe:]
                if not self._flush(BEATMIX_AHEAD):
                    return True
            od = {"from": name, "from_bpm": round(60 / g.p, 2), "skip": skipped}
            tail = self._plan_tail(g, pos0, whole, beats, od)
            if not tail:
                self._out_diag = od
        elif beats:
            self._out_diag = {"from": name, "skip": skipped,
                              "why": f"no BPM for {name}" if not g else "skipped right at the start"}
        if tail:
            # the end stays as it is under the cursor: the next track decides beatmix or a normal mix
            pos = self._add(pend, pos, fin)
            self._tail = {"at": g.start + tail[0], "end": self._cur, "n": tail[1], "bpm": tail[2], "diag": od}
            self._cur, self._overlap = self._tail["at"], 0
            self._flush(BEATMIX_AHEAD)
            return True
        out_len = min(mix or fade, len(pend) + pos)
        if out_len and len(pend):
            k = min(out_len, len(pend))
            pend = pend.copy()
            pend[-k:] *= _ramp(k, False)[:, None]
        pos = self._add(pend, pos, fin)
        # crossfade: the next track starts under the last mix seconds of this one
        self._overlap = min(mix, pos, self._cur - self._flushed) if mix else 0
        self._cur -= self._overlap
        self._flush()
        return True

    def _add(self, x, pos, fin):
        c = self._check
        if c and c["got"] < c["need"]:
            k = min(len(x), c["need"] - c["got"])
            c["in"].append(x[:k].mean(axis=1))
            c["got"] += k
            if c["got"] >= c["need"]:
                self._check = None
                threading.Thread(target=self._measure_mix, args=(c,), daemon=True).start()
        if self._gain != 1.0:
            x = x * np.float32(self._gain)
        if pos < len(fin):
            k = min(len(fin) - pos, len(x))
            x = x.copy()
            x[:k] *= fin[pos:pos + k, None]
        start = self._cur - self._flushed
        end = start + len(x)
        if end > len(self._out):
            self._out = np.concatenate((self._out, np.zeros((end - len(self._out), CHANNELS), np.float32)))
        self._out[start:end] += x
        self._cur += len(x)
        return pos + len(x)

    def _flush(self, ahead_s=0):
        """Send the final part of the output as frames, waiting for room in the queue.
        ahead_s: let the queue grow to that many seconds (it drains back by itself after)."""
        ready = self._cur - self._flushed
        ahead = max(FOLDER_AHEAD, int(ahead_s * 1000 / FRAME_MS))
        while ready >= FRAME_SAMPLES:
            while len(self.frames) >= ahead:
                if not self.running:
                    return False
                time.sleep(FRAME_MS / 4000)
            f = self._out[:FRAME_SAMPLES]
            self.frames.append((np.clip(f * self.headroom, -1, 32767 / 32768) * 32768).astype(np.int16).tobytes())
            self._out = self._out[FRAME_SAMPLES:]
            self._flushed += FRAME_SAMPLES
            ready -= FRAME_SAMPLES
        return self.running

    def skip(self):
        self._skip = True

    def seek(self, t):
        """Jump to t s in the track on air (folder only, not during a mix)."""
        if self.folder:
            self._seek = max(0.0, float(t))

    def _seek_to(self, t, path, name, info, pos, fin, lin_w):
        """Restart ffmpeg at t s of the file; the grid follows so the next mix stays on the beats.
        Returns the new ffmpeg process, None when refused (mix still heard, tempo still ramping)."""
        sr = SAMPLE_RATE
        g = self._grid
        busy = not pos or lin_w or pos < len(fin) or self._check is not None
        if g and g.tempos and not busy:
            busy = self._cur < g.start + g.offset(g.first_index() + len(g.tempos)) * sr
        if busy:
            RNS.log(f"Seek ignored in {name}: a mix is still playing", RNS.LOG_NOTICE)
            return None
        dur = self.track_dur
        beats = int(max(0, self.beats or 0))
        if dur:
            room = (beats + BAR + 1) * g.p * (1 + BEATMIX_MAX) + SEEK_BEFORE_MIX if g and beats else 1
            t = max(0.0, min(t, dur - room))
        cmd = _track_cmd(path)
        i = cmd.index("-i")
        cmd[i:i] = ["-ss", f"{t:.3f}"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            RNS.log(f"Seek failed: {e}", RNS.LOG_WARNING)
            return None
        try:
            self.proc.kill()
        except Exception:
            pass
        self.proc = proc
        # what is already in the output fades out, the new part fades in (no click at the jump)
        a = self._cur - self._flushed
        k = min(SEEK_FADE, a)
        if k > 0:
            self._out[a - k:a] *= _ramp(k, False)[:, None]
        self._out[a:] = 0
        if g and info:
            # output sample self._cur is now file second t, at the track's own tempo
            self._grid = _Grid(self._cur - pos, t - pos / sr, info[1], 60 / info[0])
        wait = (self._cur - self._flushed) / sr + len(self.frames) * FRAME_MS / 1000
        self._track_t0, self._track_lead = time.monotonic() + wait, t
        RNS.log(f"Seek to {int(t) // 60}:{int(t) % 60:02d} in {name}", RNS.LOG_NOTICE)
        return proc

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass


SWITCH_WAIT = 15  # s the old source keeps playing while the new one connects
LIVE_MAX_FRAMES = 4  # live input: drop old frames past this, keeps latency low


def _soundcard():
    if sys.platform == "win32":
        from LXST.Platforms.windows import soundcard
        from radio_client import _com_init
        _com_init()  # COM is per thread on Windows
    elif sys.platform == "darwin":
        from LXST.Platforms.darwin import soundcard
    else:
        from LXST.Platforms.linux import soundcard
    return soundcard


def _device_label(dev):
    return ("Loopback: " if getattr(dev, "isloopback", False) else "") + dev.name


def list_inputs():
    """Input device labels (mics, line in, virtual cables, loopback of outputs)."""
    from radio_client import com_call
    return com_call(lambda: [_device_label(d) for d in _soundcard().all_microphones(include_loopback=True)]) or []


def default_output_name():
    from radio_client import com_call
    return com_call(lambda: _soundcard().default_speaker().name) or ""


class LineSource:
    """Live sound card input (mic, line in, VB-Cable, loopback) -> 48 kHz stereo PCM frames."""

    url = folder = None
    stream_name = stream_title = ""

    def live_text(self):
        return f"LINE INPUT: {self.device}"

    def __init__(self, device):
        self.device = device
        self.frames = deque(maxlen=MAX_QUEUE_FRAMES)
        self.now_playing = device
        self.state = "starting"
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _open(self):
        sc = _soundcard()
        devs = sc.all_microphones(include_loopback=True)
        for d in devs:
            if _device_label(d) == self.device:
                return d
        low = self.device.lower()
        for d in devs:
            if low in _device_label(d).lower():
                return d
        return None

    def _loop(self):
        while self.running:
            from radio_client import AUDIO_LOCK, locked_audio
            dev = None
            try:
                with AUDIO_LOCK:
                    dev = self._open()
                    ch = max(1, int(dev.channels)) if dev else 1
            except Exception as e:
                RNS.log(f"Input error: {e}", RNS.LOG_ERROR)
            if dev is None:
                self.state = "input device not found"
                RNS.log(f"Input device not found: {self.device}", RNS.LOG_ERROR)
                time.sleep(5)
                continue
            try:
                with locked_audio(lambda: dev.recorder(samplerate=SAMPLE_RATE, channels=min(ch, CHANNELS),
                                                       blocksize=FRAME_SAMPLES // 4)) as rec:
                    self.state = "live"
                    while self.running:
                        x = np.asarray(rec.record(numframes=FRAME_SAMPLES), dtype=np.float32)
                        if x.ndim == 1:
                            x = x[:, None]
                        if x.shape[0] != FRAME_SAMPLES:
                            continue
                        if x.shape[1] == 1:
                            x = np.repeat(x, 2, axis=1)
                        pcm = (np.clip(x[:, :2], -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                        while len(self.frames) >= LIVE_MAX_FRAMES:
                            self.frames.popleft()
                        self.frames.append(pcm)
            except Exception as e:
                RNS.log(f"Input error on {self.device}: {e}", RNS.LOG_ERROR)
                self.state = "input error, retrying"
                time.sleep(3)

    def skip(self):
        pass

    def stop(self):
        self.running = False


def source_label(src):
    return src.url or src.folder or getattr(src, "device", "") or "?"


class Listener:
    def __init__(self, link):
        self.link = link
        self.identity = None
        self.user = ""  # display name from the listener's LXMF announce
        self.name_tried = 0
        self.user_sent = False  # the name came from the client itself
        self.caller_profile = None
        self.profile = DEFAULT_PROFILE
        self.state = "connecting"
        self.since = time.time()
        self.sent_bytes = 0
        self.failed = 0
        self.client = False  # a radio-lxst client (sent its name in the call): takes packets longer than 60 ms

    def packet_ms(self):
        return PACKET_MS if self.client or not PHONE_60 else PHONE_FRAME_MS

    @property
    def name(self):
        return self.identity.hash.hex() if self.identity else "?"

    @property
    def label(self):
        return self.name + (f" ({self.user})" if self.user else "")

    def lookup_user(self):
        from station_lxmf import lxmf_hash, lxmf_name
        self.name_tried = time.time()
        if self.identity and not self.user and not self.user_sent:
            self.user = lxmf_name(lxmf_hash(self.identity))[:32]


class Radio:
    def __init__(self, identity, name="radio-lxst", storage=None, lxmf_on=True):
        self.identity, self.name = identity, name
        self.storage = storage or os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage")
        self.lx = None  # StationLXMF, made when LXMF is first switched on
        self._lxmf_every_h = 3
        self.last_lxmf_announce = 0
        self.source = None
        self._old_source = None  # still playing during a source switch, until the crossfade ends
        self._xf_pos = self._xf_len = 0
        self._xf_deadline = 0.0
        self.monitor = None  # local listening, a Player from radio_client
        self.on_air = False
        self.announce_on_start = True
        self._every = 60
        self.last_announce = 0
        self.announce_pending = False  # one held back by the 10 min gap: sent when the gap is over
        self.stamps_done = 0
        self.stamps_ready = False
        self.pending_source = None  # On air pressed while stamps are still being made
        self.listeners = {}
        self.slots = 0  # max listeners, 0 = no limit
        self.lock = threading.Lock()
        self.encoders = {}
        self._half = None  # first 60 ms of a Steady packet
        self.total_calls = 0
        self.peak_listeners = 0
        self.total_bytes = 0
        self.max_late = 0.0
        self.out_power = 0.0  # what goes on air, 3 s average (L+R power, like LUFS without its weighting)
        self.norm_lufs = None  # Normalize target for folder tracks, None = off
        self.underruns = 0  # empty frames of a playing folder source
        self._underrun_log = 0.0
        self.levels = (0.0, 0.0)
        self.on_air_since = None
        self.gain_db = 0.0
        self.limiter_on = True
        self.limiter = Limiter()

        self.dest = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, ASPECT)
        self.dest.set_link_established_callback(self._link_established)
        # station directory entry, heard by radio_client
        self.station = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, *STATION_ASPECT)
        # path responses re-announce with the default app data: keep name and info in them too
        self.station.set_default_app_data(self.station_info)
        # live state (on air, listeners, track) on request: GET "status" over a Link
        self.status_hits = []
        self.station.register_request_handler("status", response_generator=self._status_request,
                                              allow=RNS.Destination.ALLOW_ALL)
        from station_stats import Stats
        self.stats = Stats(self)
        if lxmf_on:
            self.lxmf_on = True
        threading.Thread(target=self._prepare_stamps, daemon=True).start()
        threading.Thread(target=self._announce_loop, daemon=True).start()
        threading.Thread(target=self._meta_loop, daemon=True).start()
        threading.Thread(target=self._send_loop, daemon=True).start()

    @property
    def announce_every_min(self):
        return self._every

    @announce_every_min.setter
    def announce_every_min(self, minutes):
        minutes = max(0, int(minutes))
        self._every = max(ANNOUNCE_MIN_EVERY, minutes) if minutes else 0

    def announce_wait(self):
        """Seconds until the next announce is allowed, 0 = now."""
        return max(0.0, self.last_announce + ANNOUNCE_MIN_GAP - time.time())

    @property
    def identity_hash(self):
        return self.identity.hash.hex()

    @property
    def lxmf_hash(self):
        return RNS.Destination.hash(self.identity, "lxmf", "delivery").hex()

    @property
    def lxmf_on(self):
        return bool(self.lx and self.lx.enabled)

    @lxmf_on.setter
    def lxmf_on(self, on):
        if on and self.lx is None:
            from station_lxmf import StationLXMF
            self.lx = StationLXMF(self, self.storage)
            RNS.log(f"LXMF autoresponder on, {len(self.lx.subs)} subscriber(s)", RNS.LOG_NOTICE)
            if self.stamps_ready:
                threading.Thread(target=self._stamp_one, args=(self.lx.dest,), daemon=True).start()
        elif self.lx:
            self.lx.set_enabled(bool(on))

    def is_subscribed(self, identity):
        if not identity or not self.lx:
            return False
        return RNS.Destination.hash(identity, "lxmf", "delivery").hex() in self.lx.subs

    @property
    def lxmf_every_h(self):
        return self._lxmf_every_h

    @lxmf_every_h.setter
    def lxmf_every_h(self, hours):
        self._lxmf_every_h = max(LXMF_MIN_EVERY_H, int(hours))

    def _stamp_dests(self):
        return (self.dest, self.station) + ((self.lx.dest,) if self.lxmf_on else ())

    @property
    def stamps_total(self):
        return len(self._stamp_dests())

    def _stamp_one(self, d):
        if not getattr(RNS.Transport, "STAMP_DESTINATIONS", False) or not hasattr(d, "_await_stamp") \
                or d.stamp is not None:
            return
        t = time.time()
        try:
            d._await_stamp()
        except Exception as e:
            RNS.log(f"Stamp for {RNS.prettyhexrep(d.hash)} failed: {e}", RNS.LOG_ERROR)
        if time.time() - t > 1:
            RNS.log(f"Stamp for {RNS.prettyhexrep(d.hash)} ready in {time.time() - t:.0f} s", RNS.LOG_NOTICE)

    def _prepare_stamps(self):
        """Make the announce stamps (RNS 1.6.1+) before going on air: done once, then reused from disk."""
        if getattr(RNS.Transport, "STAMP_DESTINATIONS", False):
            dests = self._stamp_dests()
            todo = [d for d in dests if hasattr(d, "_await_stamp") and d.stamp is None]
            self.stamps_done = len(dests) - len(todo)
            for d in todo:
                t = time.time()
                try:
                    d._await_stamp()
                except Exception as e:
                    RNS.log(f"Stamp for {RNS.prettyhexrep(d.hash)} failed: {e}", RNS.LOG_ERROR)
                self.stamps_done += 1
                if time.time() - t > 1:
                    RNS.log(f"Stamp {self.stamps_done}/{len(dests)} ready in {time.time() - t:.0f} s",
                            RNS.LOG_NOTICE)
        self.stamps_done = self.stamps_total
        self.stamps_ready = True
        src, self.pending_source = self.pending_source, None
        if src:
            self.start(src)

    def start(self, source):
        if not self.stamps_ready:
            # nobody could find the station yet, and making stamps would stall the audio
            if self.pending_source:
                self.pending_source.stop()
            self.pending_source = source
            RNS.log("Preparing stamps (first start only, can take a few minutes), on air when ready",
                    RNS.LOG_NOTICE)
            return
        if self.on_air:
            self.stop()
        self.encoders = {}
        self._half = None  # first 60 ms of a Steady packet
        source.on_track = self.stats.track
        self.source = source
        self.on_air = True
        self.on_air_since = time.time()
        RNS.log(f"{self.name} on air", RNS.LOG_NOTICE)
        if self.announce_on_start:
            self.announce()
        if self.lxmf_on:
            self.lx.notify_on_air()

    def switch_source(self, source, fade_s=0):
        """Change the audio source while on air, listeners stay connected.
        The old one plays until the new one has audio, then they crossfade over fade_s seconds."""
        old, prev = self.source, self._old_source
        if prev is not None:
            prev.stop()  # switched again during a switch
        self._xf_pos, self._xf_len = None, max(FRAME_SAMPLES, int(fade_s * SAMPLE_RATE))
        self._xf_deadline = time.monotonic() + SWITCH_WAIT
        self._old_source = old if self.on_air else None
        source.on_track = self.stats.track
        self.source = source
        if old and not self.on_air:
            old.stop()
        RNS.log(f"Source switched to {source_label(source)}", RNS.LOG_NOTICE)

    @property
    def shown_source(self):
        # what listeners hear: the old source until the crossfade starts
        old = self._old_source
        return old if old is not None and self._xf_pos is None else self.source

    def _underrun(self, src):
        # a folder that is playing should never run dry: each empty frame is a hole listeners hear
        if not getattr(src, "folder", None) or src.state != "playing":
            return
        self.underruns += 1
        now = time.monotonic()
        if now - self._underrun_log >= 10:
            self._underrun_log = now
            RNS.log(f"Folder source late: {self.underruns} frame(s) of {FRAME_MS} ms missing so far "
                    f"(now: {src.now_playing})", RNS.LOG_WARNING)

    def _switch_frame(self, old, new):
        def pop(s):
            try:
                return pcm_frame(s, s.frames.popleft())
            except IndexError:
                return None
        if self._xf_pos is None:
            if len(new.frames) < 2 and time.monotonic() < self._xf_deadline:
                return pop(old)  # new one still connecting: no gap
            self._xf_pos = 0
        a, b = pop(old), pop(new)
        t = np.clip((self._xf_pos + np.arange(FRAME_SAMPLES) + 0.5) / self._xf_len, 0, 1)[:, None] * (np.pi / 2)
        self._xf_pos += FRAME_SAMPLES
        if self._xf_pos >= self._xf_len and self._old_source is old:
            self._old_source = None
            old.stop()
        if a is None and b is None:
            return None
        out = np.zeros((FRAME_SAMPLES, CHANNELS))
        if a is not None:
            out += a * np.cos(t)
        if b is not None:
            out += b * np.sin(t)
        return out

    def stop(self):
        if self.pending_source:
            self.pending_source.stop()
            self.pending_source = None
        if self._old_source is not None:
            self._old_source.stop()
            self._old_source = None
        was_on = self.on_air
        self.on_air = False
        self.stats.off_air(self.on_air_since)
        self.on_air_since = None
        with self.lock:
            ls = list(self.listeners.values())
        for l in ls:
            try:
                l.link.teardown()
            except Exception:
                pass
        if self.source:
            self.source.stop()
            self.source = None
        self.levels = (0.0, 0.0)
        RNS.log(f"{self.name} off air", RNS.LOG_NOTICE)

    def announce(self):
        """One round, 5 s apart: telephony, station, LXMF when due. False if the last round was under 10 min ago."""
        wait = self.announce_wait()
        if wait:
            self.announce_pending = True
            RNS.log(f"Announce held, sent in {wait / 60:.0f} min (one every {ANNOUNCE_MIN_GAP // 60} min)",
                    RNS.LOG_NOTICE)
            return False
        self.announce_pending = False
        self.last_announce = time.time()
        lxmf = self.lxmf_on and time.time() - self.last_lxmf_announce >= self.lxmf_every_h * 3600
        if lxmf:
            self.last_lxmf_announce = time.time()
        threading.Thread(target=self._announce_round, args=(lxmf,), daemon=True).start()
        return True

    def _announce_round(self, lxmf):
        parts = [("telephony", self.dest.announce),
                 ("station", lambda: self.station.announce(app_data=self.station_info()))]
        if lxmf:
            parts.append(("LXMF", self.lx.announce))
        for i, (_, send) in enumerate(parts):
            if i:
                time.sleep(ANNOUNCE_SPACING)
            try:
                send()
            except Exception as e:
                RNS.log(f"Announce failed: {e}", RNS.LOG_ERROR)
        what = ", ".join(p[0] for p in parts)
        if self.stamps_ready:
            RNS.log(f"Announced {self.name} ({what})", RNS.LOG_NOTICE)
        else:
            RNS.log(f"Announce of {self.name} ({what}) queued until the stamps are ready", RNS.LOG_NOTICE)

    def source_kind(self):
        src = self.source
        if src is None:
            return ""
        return "folder playlist" if src.folder else ("stream" if src.url else "live input")

    @staticmethod
    def live_np(src):
        # clients see "Live streaming" plus the stream track title when there is one
        return "Live streaming" + (f" - {src.stream_title}" if src.stream_title else "")

    def now_playing_text(self):
        src = self.shown_source
        return (src.now_playing or "-" if src.folder else self.live_np(src)) if src else "-"

    def meta(self):
        with self.lock:
            n = sum(1 for l in self.listeners.values() if l.state == "listening")
        src = self.shown_source
        np_ = (src.now_playing if src.folder else self.live_np(src)) if src else ""
        m = {"v": __version__, "n": self.name[:48], "l": n, "s": self.slots, "np": np_[:64],
             "kb": quality_kb(), "air": self.on_air}
        prog = src.track_progress(clamp=False) if src and hasattr(src, "track_progress") else None
        if src and src.stream_name:
            m["sn"] = src.stream_name[:64]  # stream name only, never the URL or the device
        if prog:  # tp < 0 = the track starts in -tp seconds
            m["tp"], m["td"] = round(prog[0], 1), round(prog[1], 1)  # track position / length
        return m

    def is_full(self, but=None):
        """All listener slots taken (ringing callers count too, so two at once cannot both get in)."""
        if not self.slots:
            return False
        with self.lock:
            n = sum(1 for l in self.listeners.values() if l is not but and l.state in ("listening", "ringing"))
        return n >= self.slots

    def station_info(self):
        # the announce says who we are, never what changes: the state is asked with a status request
        return mp.packb({"v": __version__, "n": self.name[:48], "kb": quality_kb()})

    def _status_request(self, path, data, request_id, link_id, remote_identity, requested_at):
        now = time.time()
        with self.lock:
            hits = [t for t in self.status_hits if now - t < 60]
            if len(hits) >= STATUS_MAX_PER_MIN:
                self.status_hits = hits
                return None  # too many: no answer, the asker sees "no reply"
            hits.append(now)
            self.status_hits = hits
        self.stats.inc("status_requests")
        return self.meta()

    # in-call info: LXST only reads keys 0x00/0x01, other phones ignore this one
    def _send_meta(self, lsts, meta):
        for lst in lsts:
            # q/ms: what this listener gets (Opus profile, packet length)
            data = mp.packb({FIELD_META: dict(meta, q=Opus.profile_name(lst.profile), ms=lst.packet_ms())})
            link = lst.link
            try:
                if link.status == RNS.Link.ACTIVE:
                    RNS.Packet(link, data, create_receipt=False).send()
            except Exception as e:
                RNS.log(f"Meta send failed: {e}", RNS.LOG_DEBUG)

    def _meta_loop(self):
        last, last_t = None, 0
        while True:
            time.sleep(1)
            if not self.on_air:
                last = None
                continue
            with self.lock:
                unnamed = [l for l in self.listeners.values()
                           if l.identity and not l.user and time.time() - l.name_tried >= NAME_RETRY]
            for l in unnamed:
                l.lookup_user()
            m = self.meta()
            key = {k: v for k, v in m.items() if k != "tp"}  # the position alone is no change
            if key != last or time.time() - last_t >= META_REFRESH:
                with self.lock:
                    lsts = [l for l in self.listeners.values() if l.state == "listening"]
                if lsts:
                    self._send_meta(lsts, m)
                last, last_t = key, time.time()

    def _announce_loop(self):
        while True:
            time.sleep(5)
            every = self.announce_every_min
            if self.announce_pending and not self.announce_wait():
                # held by the 10 min gap: presence only, so only if still on air
                self.announce_pending = False
                if self.on_air:
                    self.announce()
            elif self.on_air and every and time.time() - self.last_announce >= every * 60:
                self.announce()

    # call setup, server side of LXST Telephony
    def _signal(self, link, signals):
        try:
            RNS.Packet(link, mp.packb({FIELD_SIGNALLING: signals}), create_receipt=False).send()
        except Exception as e:
            RNS.log(f"Signal failed: {e}", RNS.LOG_DEBUG)

    def _link_established(self, link):
        if not self.on_air:
            link.teardown()
            return
        lst = Listener(link)
        with self.lock:
            self.listeners[link.link_id] = lst
        link.set_link_closed_callback(self._link_closed)
        link.set_remote_identified_callback(self._identified)
        link.set_packet_callback(lambda data, packet: self._packet(lst, data))
        self._signal(link, [STATUS_AVAILABLE])

    def _identified(self, link, identity):
        lst = self.listeners.get(link.link_id)
        if not lst:
            return
        lst.identity = identity
        lst.lookup_user()
        if self.is_full(but=lst):
            # BUSY: Sideband/rnphone show "busy", our clients "station full"
            lst.state = "full"
            self.stats.inc("refused")
            self._send_meta([lst], self.meta())
            self._signal(link, [STATUS_BUSY])
            RNS.log(f"Listener {lst.label} refused: all {self.slots} slots taken", RNS.LOG_NOTICE)
            threading.Timer(2.0, link.teardown).start()
            return
        lst.state = "ringing"
        self._signal(link, [STATUS_RINGING])
        threading.Timer(ANSWER_DELAY, self._answer, args=(lst,)).start()

    def _answer(self, lst):
        if lst.link.status != RNS.Link.ACTIVE or not self.on_air:
            return
        lst.profile = PROFILE_MAP.get(lst.caller_profile, DEFAULT_PROFILE)
        self._signal(lst.link, [STATUS_CONNECTING])
        self._signal(lst.link, [STATUS_ESTABLISHED])
        lst.state = "listening"
        lst.since = time.time()
        self.total_calls += 1
        with self.lock:
            n = sum(1 for l in self.listeners.values() if l.state == "listening")
        self.peak_listeners = max(self.peak_listeners, n)
        self.stats.listen(lst, n)
        self._send_meta([lst], self.meta())
        RNS.log(f"Listener {lst.label} tuned in ({self.profile_label(lst)})", RNS.LOG_NOTICE)

    def _packet(self, lst, data):
        # caller audio is ignored, only signalling and the listener's name matter
        try:
            msg = mp.unpackb(data)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        info = msg.get(FIELD_LISTENER)
        if isinstance(info, dict) and not lst.client:
            lst.client = True
            if PACKET_MS > PHONE_FRAME_MS:
                RNS.log(f"Listener {lst.name} is a radio-lxst client: {PACKET_MS} ms packets", RNS.LOG_VERBOSE)
                if lst.state == "listening":
                    self._send_meta([lst], self.meta())
        if isinstance(info, dict) and info.get("n"):
            name = "".join(c for c in str(info["n"]) if c.isprintable()).strip()[:32]
            if name and name != lst.user:
                old, lst.user, lst.user_sent = lst.user, name, True
                RNS.log(f"Listener {lst.name} is {name}" + (f" (was {old})" if old else ""), RNS.LOG_NOTICE)
        if FIELD_SIGNALLING not in msg:
            return
        sigs = msg[FIELD_SIGNALLING]
        for s in (sigs if isinstance(sigs, list) else [sigs]):
            if isinstance(s, int) and s >= PREFERRED_PROFILE:
                lst.caller_profile = s - PREFERRED_PROFILE
                if lst.state == "listening":
                    lst.profile = PROFILE_MAP.get(lst.caller_profile, DEFAULT_PROFILE)
                    RNS.log(f"Listener {lst.label} switched to {self.profile_label(lst)}", RNS.LOG_NOTICE)
                    self._send_meta([lst], self.meta())

    def _link_closed(self, link):
        with self.lock:
            lst = self.listeners.pop(link.link_id, None)
        if lst and lst.state == "listening":
            self.stats.left(lst)
            RNS.log(f"Listener {lst.label} left after {RNS.prettytime(time.time() - lst.since)}", RNS.LOG_NOTICE)

    @staticmethod
    def profile_label(lst):
        cp = CALLER_PROFILE_NAMES.get(lst.caller_profile, "default")
        return f"{cp} -> Opus {Opus.profile_name(lst.profile)}" + ("" if lst.client else ", phone")

    def _encoder(self, profile, ms):
        # one encoder per profile and packet length: each Opus stream stays continuous
        enc = self.encoders.get((profile, ms))
        if enc is None:
            enc = RadioOpus(profile=profile)
            enc.source = _Src()
            self.encoders[(profile, ms)] = enc
        return enc

    # one encode per profile in use, same frame to every listener of that profile
    def _send_loop(self):
        next_t = time.monotonic()
        while True:
            next_t += FRAME_MS / 1000
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                self.max_late = max(self.max_late, -delay)
                if delay < -1:
                    next_t = time.monotonic()
            src = self.source
            if not self.on_air or src is None:
                continue
            old = self._old_source
            if old is not None:
                frame = self._switch_frame(old, src)
                if frame is None:
                    self.levels = (0.0, 0.0)
                    continue
            else:
                try:
                    pcm = src.frames.popleft()
                except IndexError:
                    self.levels = (0.0, 0.0)
                    self._underrun(src)
                    continue
                frame = pcm_frame(src, pcm)
            if self.gain_db:
                frame = frame * 10 ** (self.gain_db / 20)
            if self.limiter_on:
                frame = self.limiter.process(frame)
            else:
                self.limiter.reduction_db = 0.0
                frame = np.clip(frame, -1.0, 1.0)
            peak = np.abs(frame).max(axis=0)
            self.levels = (float(peak[0]), float(peak[1]))
            self.out_power += (float(np.mean(frame * frame)) * 2 - self.out_power) * (FRAME_MS / 1000 / OUT_AVG_S)
            mon = self.monitor
            if mon is not None:
                mon.push(frame)
            with self.lock:
                active = [l for l in self.listeners.values() if l.state == "listening"]
            # Steady: two frames in one 120 ms packet, the first one waits here
            half, pair = self._half, None
            if any(l.packet_ms() > FRAME_MS for l in active):
                if half is None:
                    self._half = frame
                else:
                    pair, self._half = np.concatenate((half, frame)), None
            else:
                self._half = None
            if not active:
                continue
            packets = {}
            for lst in active:
                ms = lst.packet_ms()
                if ms > FRAME_MS and pair is None:
                    continue
                key = (lst.profile, ms)
                if key not in packets:
                    try:
                        enc = self._encoder(lst.profile, ms)
                        data = enc.encode(pair if ms > FRAME_MS else frame)
                        packets[key] = mp.packb({FIELD_FRAMES: CODEC_OPUS + bytes(data)})
                    except Exception as e:
                        RNS.log(f"Encode error: {e}", RNS.LOG_ERROR)
                        packets[key] = None
                data = packets[key]
                if not data or lst.link.status != RNS.Link.ACTIVE:
                    continue
                if RNS.Packet(lst.link, data, create_receipt=False).send() is False:
                    lst.failed += 1
                else:
                    lst.sent_bytes += len(data)
                    self.total_bytes += len(data)

    def snapshot(self):
        with self.lock:
            ls = list(self.listeners.values())
        now = time.time()
        rows = []
        for l in ls:
            on = l.state == "listening"
            rows.append({
                "id": l.name, "user": l.user, "state": l.state, "sub": self.is_subscribed(l.identity), "profile": self.profile_label(l) if on else "",
                "time": now - l.since if on else 0,
                "kbps": l.sent_bytes * 8 / max(1, now - l.since) / 1000 if on else 0,
                "lost": l.failed,
            })
        src = self.shown_source
        return {
            "on_air": self.on_air, "uptime": now - self.on_air_since if self.on_air_since else 0,
            "listeners": sum(1 for r in rows if r["state"] == "listening"), "rows": rows,
            "peak": self.peak_listeners, "calls": self.total_calls, "bytes": self.total_bytes,
            "buffer_ms": len(src.frames) * FRAME_MS if src else 0,
            "source_state": src.state if src else "off", "now_playing": src.now_playing if src else "",
            "progress": src.track_progress() if src and hasattr(src, "track_progress") else None,
            "mixing": src.mixing() if src and hasattr(src, "mixing") else False,
            "live_text": src.live_text() if src else "",
            "levels": self.levels, "late_ms": self.max_late * 1000,
            "out_db": -0.691 + 10 * np.log10(self.out_power) if self.on_air and self.out_power > 1e-9 else None,
            "gain_db": self.gain_db, "limiter_on": self.limiter_on,
            "ceiling_db": self.limiter.ceiling_db,
            "reduction_db": self.limiter.reduction_db if self.limiter_on and self.on_air else 0.0,
            "next_announce": (self.last_announce + self.announce_every_min * 60 - now)
            if self.on_air and self.announce_every_min else None,
            "announce_wait": self.announce_wait(),
            "stamps_ready": self.stamps_ready, "stamps_done": self.stamps_done,
            "stamps_total": self.stamps_total,
            "waiting": self.pending_source is not None,
        }

    def status_text(self):
        s = self.snapshot()
        lines = [f"[{time.strftime('%H:%M:%S')}] {self.name}  listeners {s['listeners']}  "
                 f"(total calls {s['calls']}, buffer {s['buffer_ms']} ms, {PACKET_MS} ms packets, "
                 f"send late max {s['late_ms']:.0f} ms)"]
        if s["waiting"]:
            lines[0] += f"  preparing stamps {s['stamps_done']}/{s['stamps_total']}, on air when ready"
        self.max_late = 0.0
        for r in s["rows"]:
            dur = RNS.prettytime(r["time"]) if r["state"] == "listening" else r["state"]
            lines.append(f"   {r['id']}" + (f" ({r['user']})" if r["user"] else "") + f"  {r['profile']}  {dur}  {r['kbps']:.1f} kbit/s"
                         + (f"  lost {r['lost']}" if r["lost"] else ""))
        return "\n".join(lines)


def load_identity(path):
    if os.path.isfile(path):
        return RNS.Identity.from_file(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)  # a bare file name too
    ident = RNS.Identity()
    ident.to_file(path)
    return ident


def lock_station(storage):
    """One station per storage folder (= identity folder): returns the lock (keep it open) or None if taken."""
    os.makedirs(storage, exist_ok=True)
    f = open(os.path.join(storage, "station.lock"), "a+")
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def already_on(storage):
    return (f"another radio-lxst station is already running with {storage} "
            "(one station per identity folder: give the other one its own identity)")


def settings_path(path, base):
    """Relative paths in a settings file are taken from the file's own folder."""
    return path if not path or os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def load_settings_file(path):
    """GUI settings.json -> argparse defaults (station keys only) + the source of its mode."""
    with open(path, encoding="utf-8") as f:
        s = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    d, src = {}, {}
    plain = {"name": "name", "rnsconfig": "rnsconfig", "frame_ms": "frame_ms", "shuffle": "shuffle",
             "trim_silence": "trim_silence", "announce_every_min": "announce_every", "lxmf_every_h": "lxmf_every",
             "slots": "slots", "gain_db": "gain", "limiter_ceiling_db": "ceiling"}
    for k, dest in plain.items():
        if s.get(k) not in (None, ""):
            d[dest] = s[k]
    if s.get("identity"):
        d["identity"] = settings_path(s["identity"], base)
    if d.get("rnsconfig"):
        d["rnsconfig"] = settings_path(d["rnsconfig"], base)
    if "lxmf_on" in s:
        d["no_lxmf"] = not s["lxmf_on"]
    if "limiter_on" in s:
        d["no_limiter"] = not s["limiter_on"]
    if "phone_60" in s:
        d["phone_any"] = not s["phone_60"]
    if s.get("fade_on"):
        d["fade"] = s.get("fade_s", 3)
    if s.get("mix_on"):
        d["mix"] = s.get("mix_s", 5)
    if s.get("beat_on"):
        d["beatmix"] = s.get("beat_n", 32)
    d.update(loop_out=bool(s.get("loop_out")), loop_in=bool(s.get("loop_in")), loop=s.get("loop_n", 8),
             loop_times=s.get("loop_times", 4))
    if s.get("norm_on"):
        d["normalize"] = s.get("norm_lufs", -14)
    mode = s.get("mode", "url")
    if mode == "folder" and s.get("folder"):
        src["folder"] = settings_path(s["folder"], base)
    elif mode == "line" and s.get("input_device"):
        src["input"] = s["input_device"]
    elif mode == "url" and s.get("url"):
        src["url"] = s["url"]
    return d, src, s.get("announce_on_start", True)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Internet radio over Reticulum LXST")
    ap.add_argument("--settings", metavar="FILE",
                    help="start with the values of a GUI settings.json, options given here win")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--url", help="existing stream (http/https, Icecast, Shoutcast...)")
    g.add_argument("--folder", help="folder of audio files, played in a loop")
    g.add_argument("--input", metavar="DEVICE", help="live sound card input: mic, line in, VB-Cable, 'Loopback: ...' (name or part of it)")
    g.add_argument("--list-inputs", action="store_true", help="list the input devices and exit")
    ap.add_argument("--shuffle", action="store_true", help="shuffle the folder playlist")
    ap.add_argument("--fade", type=float, default=0, metavar="SEC", help="folder: fade in/out of each track, 0 = off")
    ap.add_argument("--mix", type=float, default=0, metavar="SEC",
                    help="folder: crossfade between tracks (wins over --fade), 0 = off")
    ap.add_argument("--trim-silence", action="store_true", help="folder: cut the silence at start/end of tracks")
    ap.add_argument("--beatmix", type=int, default=0, metavar="BEATS",
                    help="folder, experimental: mix tracks on the beat over BEATS beats (BPM found in background), 0 = off")
    ap.add_argument("--loop-out", action="store_true", help="beatmix: the outgoing track loops from the mix point")
    ap.add_argument("--loop-in", action="store_true", help="beatmix: the incoming track loops its first beats")
    ap.add_argument("--loop", type=int, default=8, choices=LOOP_CHOICES, metavar="BEATS",
                    help=f"beatmix loop length {LOOP_CHOICES}")
    ap.add_argument("--loop-times", type=int, default=4, choices=LOOP_TIMES, metavar="N",
                    help=f"times the loop plays {LOOP_TIMES}")
    ap.add_argument("--normalize", type=int, default=None, metavar="LUFS",
                    help=f"folder: bring every track to this loudness ({NORM_LUFS[0]} to {NORM_LUFS[1]}, e.g. -14)")
    ap.add_argument("--name", default="radio-lxst", help="station name in the announce")
    ap.add_argument("--identity", default=os.path.join(here, "storage", "identity"))
    ap.add_argument("--rnsconfig", default=None, help="Reticulum config dir")
    ap.add_argument("--frame-ms", type=int, choices=[60, 120], default=60,
                    help="120 = steadier on slow paths (max ~28 kbit/s), 60 = up to 56 kbit/s")
    ap.add_argument("--announce-every", type=int, default=60, help=f"minutes between announces (min {ANNOUNCE_MIN_EVERY}), 0 = only at start")
    ap.add_argument("--no-lxmf", action="store_true",
                    help="no LXMF address: no autoresponder, no subscriber messages, one announce less")
    ap.add_argument("--lxmf-every", type=int, default=3, metavar="HOURS",
                    help=f"hours between LXMF address announces (min {LXMF_MIN_EVERY_H})")
    ap.add_argument("--slots", type=int, default=0, help="max listeners at once (limits the upload), 0 = no limit")
    ap.add_argument("--gain", type=float, default=0.0, help="output volume in dB (-24..+12)")
    ap.add_argument("--no-limiter", action="store_true", help="disable the peak limiter")
    ap.add_argument("--phone-any", action="store_true",
                    help="Steady sends 120 ms packets to phones too (default: phones always get 60 ms)")
    ap.add_argument("--ceiling", type=float, default=-2.0, help="limiter ceiling in dBFS")
    ap.add_argument("--status", type=int, default=15, help="seconds between status lines, 0 = none")
    ap.add_argument("--ctl", nargs="*", metavar="COMMAND",
                    help="talk to the station running with this identity: one command (e.g. --ctl next), "
                         "or none for a prompt; --ctl help lists them")
    ap.add_argument("--stats", action="store_true",
                    help="statistics of the station with this identity (this run and all time) and exit")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    pre, _ = ap.parse_known_args()
    announce_on_start = True
    src = {}
    raw = {}
    if pre.settings:
        try:
            d, src, announce_on_start = load_settings_file(pre.settings)
            with open(pre.settings, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            ap.error(f"cannot read --settings {pre.settings}: {e}")
        ap.set_defaults(**d)
    a = ap.parse_args()
    storage = os.path.dirname(os.path.abspath(a.identity))
    if a.ctl is not None:
        import station_ctl
        return station_ctl.ctl_main(storage, a.ctl)
    if a.stats:
        import station_ctl
        import station_stats
        lines = station_ctl.query(storage, "stats")
        if lines is None:
            # taken = the GUI station is running (it has no control channel)
            lock = lock_station(storage) if os.path.isdir(storage) else None
            running = lock is None and os.path.isdir(storage)
            if lock:
                lock.close()
            lines = station_stats.offline_text(storage, running=running)
        print("\n".join(lines))
        return
    if a.list_inputs:
        print("\n".join(list_inputs()) or "no input devices found")
        return
    if not (a.url or a.folder or a.input):  # source from the settings file only if none given here
        a.url, a.folder, a.input = src.get("url"), src.get("folder"), src.get("input")
    if a.frame_ms not in (60, 120):
        ap.error(f"frame_ms must be 60 or 120, not {a.frame_ms}")
    set_frame_ms(a.frame_ms)
    set_phone_60(not a.phone_any)
    lock = lock_station(storage)
    if lock is None:
        sys.exit(already_on(storage))

    import station_ctl
    s = settings_from_args(a, raw, pre.settings, announce_on_start)
    st = station_ctl.Station(storage, s, os.path.abspath(pre.settings) if pre.settings else None)

    def log_line(line):
        print(line, flush=True)
        st.add_log(line)
    RNS.Reticulum(configdir=a.rnsconfig, loglevel=RNS.LOG_NOTICE + a.verbose)
    RNS.logdest = RNS.LOG_CALLBACK
    RNS.logcall = log_line
    import rns_watch
    rns_watch.start()
    import update_check
    update_check.watch(__version__, lambda v: RNS.log(f"New version {v} on GitHub: {update_check.REPO_URL}",
                                                      RNS.LOG_NOTICE))
    radio = Radio(load_identity(a.identity), s["name"], storage=storage, lxmf_on=bool(s["lxmf_on"]))
    st.radio = radio
    st.apply_output()
    ctl = station_ctl.Server(st, storage)
    try:
        where = ctl.start()
    except OSError as e:
        ctl, where = None, f"off ({e})"
    has_src = a.url or a.folder or a.input
    if has_src:
        try:
            st.go_on_air()
        except station_ctl.CtlError as e:
            print(f"  Not on air: {e}")

    print(f"\n  {s['name']}  " + (("on air" if radio.on_air else "preparing stamps, on air when ready")
                                 if has_src else "off air: no source set"))
    print(f"  Call this identity from Sideband / rnphone:  {radio.identity_hash}")
    print(f"  LXST destination: {radio.dest.hash.hex()}   LXMF: "
          + (radio.lxmf_hash if radio.lxmf_on else "off"))
    print(f"  Source: {a.url or a.folder or a.input or '-'}")
    print(f"  Control: {where}")
    print(f"  Commands from another terminal: python radio_lxst.py"
          + (f" --settings {pre.settings}" if pre.settings else f" --identity {a.identity}") + " --ctl\n")
    def _term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)  # kill / systemd stop: same clean stop as Ctrl+C
    try:
        while not st.stop_all.wait(a.status if a.status > 0 else 3600):
            if a.status > 0:
                print(radio.status_text(), flush=True)
    except KeyboardInterrupt:
        pass
    radio.stats.save()
    if radio.monitor:
        radio.monitor.stop()
    radio.stop()
    if ctl:
        ctl.close()
    time.sleep(0.5)
    import multiprocessing
    for p in multiprocessing.active_children():
        p.kill()  # stamp workers still hashing: they ignore SIGTERM (RNS handler inherited by the fork)


def settings_from_args(a, raw, settings_file, announce_on_start):
    """The station settings in the GUI settings.json keys: the file, then what the command line says."""
    import station_ctl
    s = dict(station_ctl.DEFAULTS)
    s.update(raw)
    base = os.path.dirname(os.path.abspath(settings_file)) if settings_file else os.getcwd()
    for k in ("identity", "folder", "rnsconfig"):
        s[k] = settings_path(s[k], base)
    s.update(name=a.name, frame_ms=a.frame_ms, shuffle=bool(a.shuffle), trim_silence=bool(a.trim_silence),
             announce_every_min=a.announce_every, lxmf_every_h=a.lxmf_every, slots=max(0, a.slots),
             gain_db=max(-24.0, min(12.0, a.gain)), limiter_ceiling_db=min(0.0, a.ceiling),
             lxmf_on=not a.no_lxmf, limiter_on=not a.no_limiter, phone_60=not a.phone_any, identity=os.path.abspath(a.identity),
             rnsconfig=a.rnsconfig or "", announce_on_start=bool(announce_on_start),
             loop_out=bool(a.loop_out), loop_in=bool(a.loop_in), loop_n=a.loop, loop_times=a.loop_times)
    s["fade_on"] = a.fade > 0
    if a.fade > 0:
        s["fade_s"] = max(1, min(30, int(a.fade)))
    s["mix_on"] = a.mix > 0
    if a.mix > 0:
        s["mix_s"] = max(1, min(30, int(a.mix)))
    s["beat_on"] = a.beatmix > 0
    if a.beatmix > 0:
        s["beat_n"] = a.beatmix
    s["norm_on"] = a.normalize is not None
    if a.normalize is not None:
        s["norm_lufs"] = max(NORM_LUFS[0], min(NORM_LUFS[1], a.normalize))
    if a.input:
        s.update(mode="line", input_device=a.input)
    elif a.folder:
        s.update(mode="folder", folder=os.path.abspath(a.folder))
    elif a.url:
        s.update(mode="url", url=a.url)
    return s


if __name__ == "__main__":
    sys.modules.setdefault("radio_lxst", sys.modules[__name__])  # station_ctl sees this module, not a copy
    main()
