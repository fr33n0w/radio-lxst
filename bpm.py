"""BPM and beat grid of an audio file, numpy only (ffmpeg decodes).
Onset envelope (spectral flux) -> autocorrelation for a first guess -> fine search with a comb over the
whole excerpt, which also gives where the beats fall. Made for steady tempos (house, techno, pop)."""
import os
import re
import sys
import json
import time
import threading
import subprocess

import numpy as np

SR = 11025
N_FFT = 1024
HOP = 128
MIN_BPM, MAX_BPM = 70.0, 180.0
PRIOR_BPM = 120.0  # octave errors: the guess leans to this
EXCERPT = 120  # s analysed, from the middle of the track
BPM_FILE = "bpm.json"  # in the storage dir


def _decode(path, start, dur):
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1"]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", path, "-vn", "-t", f"{dur:.2f}", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True, timeout=300,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return np.frombuffer(out[:len(out) // 4 * 4], np.float32)


BEAT_LAG = 0.02  # s: the flux peaks this much before the kick attack (measured on synthetic kicks)
KICK_HZ = (30, 150)  # band of the kick: where the beat is (hats sit on the off-beat in house)


def onset_envelope(x):
    """Spectral flux per hop, mean removed: (all bands, kick band). Peaks where notes and drums start."""
    win = np.hanning(N_FFT).astype(np.float32)
    n = 1 + (len(x) - N_FFT) // HOP
    if n < 2:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)
    lo, hi = (int(f * N_FFT / SR) for f in KICK_HZ)
    flux = np.empty(n, np.float32)
    low = np.empty(n, np.float32)
    prev = None
    for a in range(0, n, 2048):  # chunks keep the memory low
        b = min(n, a + 2048)
        idx = np.arange(a, b)[:, None] * HOP + np.arange(N_FFT)[None, :]
        mag = np.log1p(1000 * np.abs(np.fft.rfft(x[idx] * win, axis=1))).astype(np.float32)
        d = np.maximum(np.diff(mag, axis=0, prepend=mag[:1] if prev is None else prev), 0)
        flux[a:b] = d.sum(axis=1)
        low[a:b] = d[:, lo:hi + 1].sum(axis=1)
        prev = mag[-1:]
    k = int(0.4 * SR / HOP)  # local mean over 0.4 s
    out = []
    for f in (flux, low):
        f[0] = 0
        out.append(np.maximum(f - np.convolve(f, np.ones(k) / k, mode="same"), 0))
    return out[0], out[1]


def _comb(env, bpm, harmonics=4):
    """Strength of a beat grid at bpm over the whole envelope; also its phase (k=1)."""
    p = 60.0 / bpm * SR / HOP
    n = np.arange(len(env))
    s, ph = 0.0, 0.0
    for k in range(1, harmonics + 1):
        z = np.dot(env, np.exp(-2j * np.pi * k * n / p))
        s += abs(z) / k
        if k == 1:
            ph = np.angle(z)
    return s, ph


def _pulse(env, first, p):
    """Beat hits vs the quarter points between beats (empty in a steady beat): mean ratio, ~1 = no beat."""
    fps = SR / HOP
    w = max(1, int(0.03 * fps))

    def strength(shift):
        cs = np.arange(first + shift * p, len(env) - w, p).astype(int)
        return np.array([env[max(0, c - w):c + w + 1].max() for c in cs]) if len(cs) else np.zeros(1)

    on = strength(0.0).mean()
    off = np.mean(np.concatenate((strength(0.25), strength(0.75))))
    return float(on / off) if off > 0 else (10.0 if on > 0 else 0.0)


def tempo(env, low=None):
    """(bpm, confidence 0..1, first beat in s from the excerpt start); low = kick band envelope for the beats."""
    fps = SR / HOP
    e = env - env.mean()
    ac = np.fft.irfft(np.abs(np.fft.rfft(e, 2 * len(e))) ** 2)[:len(e)]
    lags = np.arange(int(fps * 60 / MAX_BPM), int(fps * 60 / MIN_BPM) + 1)
    bpms = 60 * fps / lags
    w = np.exp(-0.5 * (np.log2(bpms / PRIOR_BPM)) ** 2)  # one octave wide
    score = ac[lags] * w
    # a lag also gets support from its double (bars of 2 beats)
    dbl = np.clip(lags * 2, 0, len(ac) - 1)
    score = score + 0.5 * ac[dbl] * w
    guess = bpms[int(np.argmax(score))]
    # fine search around it: the comb over the full excerpt pins the tempo to ~0.01 BPM
    cands = np.arange(guess * 0.96, guess * 1.04, 0.05)
    s = np.array([_comb(env, b)[0] for b in cands])
    b0 = cands[int(np.argmax(s))]
    cands = np.arange(b0 - 0.06, b0 + 0.06, 0.005)
    s2 = np.array([_comb(env, b)[0] for b in cands])
    bpm = float(cands[int(np.argmax(s2))])
    best, ph = _comb(env, bpm)
    conf = float(max(0.0, min(1.0, 1 - np.median(s) / best))) if best > 0 else 0.0
    if low is not None and low.any():
        ph = _comb(low, bpm, harmonics=1)[1]
    p = 60.0 / bpm * fps
    first = ((-ph / (2 * np.pi)) % 1.0) * p  # frames: first beat of the grid
    # how many grid beats really have a hit on them: low on ambient, pads, speech
    pulse = _pulse(low if low is not None and low.any() else env, first, p)
    first_s = (first * HOP + N_FFT / 2) / SR + BEAT_LAG
    return bpm, conf, first_s, pulse


LOCAL_SPAN = 0.06  # local tempo searched within +-6% of the track BPM
LOCAL_CONF = 0.5   # comb confidence needed to trust a local tempo


def local_tempo(x, sr, near):
    """Tempo of this stretch of audio, searched near the BPM of the whole track (a DJ set or a live track
    moves): (bpm, confidence 0..1), (None, 0) if too short."""
    x = np.asarray(x, np.float32)
    if sr != SR:
        x = np.interp(np.arange(int(len(x) * SR / sr)) * (sr / SR), np.arange(len(x)), x).astype(np.float32)
    if len(x) < 8 * 60.0 / near * SR:
        return None, 0.0
    env, _ = onset_envelope(x)
    cands = np.arange(near * (1 - LOCAL_SPAN), near * (1 + LOCAL_SPAN), 0.05)
    s = np.array([_comb(env, b)[0] for b in cands])
    b0 = cands[int(np.argmax(s))]
    fine = np.arange(b0 - 0.06, b0 + 0.06, 0.005)
    b = float(fine[int(np.argmax([_comb(env, v)[0] for v in fine]))])
    best = float(s.max())
    return b, (float(max(0.0, min(1.0, 1 - np.median(s) / best))) if best > 0 else 0.0)


ATTACK_HZ = (30, 200)  # kick body + punch: up to 120 Hz only, a bass over the kick hid it (mixes galloped)
ATTACK_BIN = 0.002     # s
KICK_CLEAR = 0.4       # clarity needed to trust a kick phase (breaks without a kick stay under it)
KICK_VERSION = 3       # "kick" in bpm.json: beat measured on the kick attack; older entries are analysed again


ALIGN_AGREE = 0.012  # s: a quarter of the window agrees with the whole within this
ALIGN_CLEAR = 0.15   # clarity under this: too few kicks to line up


def align(ref, x, sr, bpm):
    """How late the kicks of x come against those of ref (same tempo, same start): (s in -beat/2..beat/2, trust 0..1).
    Kick attacks by beat_phase, the measure that agreed with the ear (a cross-correlation of the low profiles
    was pulled by a bass right after the kick). Trust = share of the 4 quarters of the window giving the same lag."""
    p = 60.0 / bpm
    n = min(len(ref), len(x))
    if n < 8 * p * sr:
        return 0.0, 0.0

    def lag_of(a, b):
        (po, co), (pi, ci) = beat_phase(ref[a:b], sr, bpm), beat_phase(x[a:b], sr, bpm)
        d = (pi - po + p / 2) % p - p / 2
        return d, min(co, ci)

    lag, clear = lag_of(0, n)
    if clear < ALIGN_CLEAR:
        return float(lag), 0.0
    q = n // 4
    agree = 0
    for i in range(4):
        d, _ = lag_of(i * q, (i + 1) * q)
        agree += abs((d - lag + p / 2) % p - p / 2) <= ALIGN_AGREE
    return float(lag), agree / 4


def beat_phase(x, sr, bpm):
    """For a known tempo: (first kick attack in s from the start of x, clarity 0..1).
    Low band energy (5 ms), its rise folded over one beat: the strongest rise is the kick attack.
    The spectral flux (93 ms window, log) put it 20-80 ms early depending on the kick: tracks galloped."""
    x = np.asarray(x, np.float32)
    if sr != SR:
        x = np.interp(np.arange(int(len(x) * SR / sr)) * (sr / SR), np.arange(len(x)), x).astype(np.float32)
    p = 60.0 / bpm
    if len(x) < 2 * p * SR:
        return 0.0, 0.0
    f = np.fft.rfftfreq(len(x), 1 / SR)
    X = np.fft.rfft(x)
    X[(f < ATTACK_HZ[0]) | (f > ATTACK_HZ[1])] = 0
    # analytic signal: a smooth envelope, a sustained bass does not ripple into fake attacks
    env = np.abs(np.fft.ifft(np.concatenate((X, np.zeros(len(x) - len(X), X.dtype))) * 2)).astype(np.float32)
    k = int(0.005 * SR)
    env = np.convolve(env, np.ones(k, np.float32) / k, mode="same")
    nb = max(8, int(round(p / ATTACK_BIN)))
    t = np.arange(len(env)) / SR
    b = np.minimum((t % p / p * nb).astype(int), nb - 1)
    fold = np.bincount(b, env, nb) / np.maximum(np.bincount(b, None, nb), 1)
    fold = np.convolve(np.concatenate((fold[-2:], fold, fold[:2])), np.ones(5) / 5, mode="valid")  # circular
    peak, base = float(fold.max()), float(fold.min())
    if peak <= 0:
        return 0.0, 0.0
    # the kick is the loudest hit of every beat (the steepest rise can be a ducked off-beat bass);
    # its attack: back from the peak to half its height
    i = int(np.argmax(fold))
    half = base + (peak - base) / 2
    for _ in range(nb // 4):
        if fold[(i - 1) % nb] < half:
            break
        i -= 1
    return (i % nb + 0.5) / nb * p, float(max(0.0, 1 - np.median(fold) / peak))


def probe(path):
    """Length in s with ffprobe, 0.0 if unknown."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                              "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return float(out.strip())
    except Exception:
        return 0.0


def loudness(path):
    """Integrated loudness in LUFS (EBU R128, ffmpeg ebur128) of the whole file, None if unknown."""
    kw = {"creationflags": 0x00004000 | getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
    try:
        err = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-threads", "1", "-i", path, "-vn",
                              "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                             capture_output=True, text=True, errors="replace", timeout=300, **kw).stderr
        m = re.findall(r"Integrated loudness:\s+I:\s+(-?[\d.]+) LUFS", err)
        v = float(m[-1]) if m else None
        return round(v, 1) if v is not None and v > -70 else None
    except Exception:
        return None


def analyse(path, duration=None):
    """{'bpm', 'conf', 'beat'} where beat = a beat time in s from the start of the file."""
    if duration is None:
        duration = probe(path)
    start = max(0.0, (duration - EXCERPT) / 2) if duration else 0.0
    x = _decode(path, start, EXCERPT)
    if len(x) < SR * 8:
        return None
    env, low = onset_envelope(x)
    bpm, conf, first, pulse = tempo(env, low)
    ph, clear = beat_phase(x, SR, bpm)  # kick attack: the same measure the beatmix uses on the mix
    if clear >= KICK_CLEAR:
        first = ph
    return {"bpm": round(bpm, 2), "conf": round(conf, 2), "pulse": round(pulse, 2), "beat": round(float(start + first), 4),
            "dur": round(duration, 1), "kick": KICK_VERSION}


def analyse_child(path):
    """analyse() in a child process at low priority: the station threads keep the GIL and the CPU."""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        cmd = [sys.executable, "--bpm-json", path]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--json", path]
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00004000 | getattr(subprocess, "CREATE_NO_WINDOW", 0)  # below normal
    else:
        kw["preexec_fn"] = lambda: os.nice(10)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600, **kw).stdout.strip()
    return json.loads(out.splitlines()[-1]) if out else None


class BpmCache:
    """bpm.json in the storage dir, keyed by path; a file changed on disk is analysed again.
    One background worker, one track at a time, low priority."""

    MAX = 20000

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.data = self._load()
        self.queue = []
        self.busy = None  # path being analysed now
        self.dur_only = set()  # queued only for the length or the loudness
        self.version = 0
        self._wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    def _load(self):
        try:
            with open(self.store, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        with self.lock:
            keep = sorted(self.data, key=lambda k: self.data[k].get("t", 0), reverse=True)[:self.MAX]
            data = {k: self.data[k] for k in keep}
        tmp = self.store + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f)
            os.replace(tmp, self.store)
        except OSError:
            pass

    @staticmethod
    def _stamp(path):
        try:
            st = os.stat(path)
            return [int(st.st_mtime), st.st_size]
        except OSError:
            return None

    def get(self, path):
        """The cached result if still valid for the file, else None."""
        with self.lock:
            r = self.data.get(path)
        if r and r.get("f") == self._stamp(path):
            return r
        return None

    def want(self, paths, force=False):
        """Queue tracks for analysis (missing ones only, unless force)."""
        with self.lock:
            for p in paths:
                if force:
                    self.dur_only.discard(p)
                if p in self.queue or p == self.busy:
                    continue
                r = self.data.get(p) or {}
                if force or not r.get("f") == self._stamp(p) or (r.get("bpm") and r.get("kick") != KICK_VERSION):
                    self.queue.append(p)
                elif "dur" not in r or "lufs" not in r:  # cached before these were kept: only them
                    self.dur_only.add(p)
                    self.queue.append(p)
            if force:  # asked by hand: before the automatic ones
                fr = [p for p in self.queue if p in set(paths)]
                self.queue = fr + [p for p in self.queue if p not in set(fr)]
                self.version += 1
        self._wake.set()

    def waiting(self, path):
        with self.lock:
            return path == self.busy or path in self.queue

    def pending(self):
        with self.lock:
            return len(self.queue) + (1 if self.busy else 0)

    def _worker(self):
        while True:
            self._wake.wait()
            with self.lock:
                if not self.queue:
                    self._wake.clear()
                    continue
                p = self.busy = self.queue.pop(0)
                quick = p in self.dur_only
                self.dur_only.discard(p)
                old = dict(self.data.get(p) or {})
            if quick:
                r = dict(old)
            else:
                try:
                    r = analyse_child(p)
                except Exception:
                    r = None
                r = dict(r or {"bpm": None}, f=self._stamp(p), t=int(time.time()))
            if "dur" not in r:
                r["dur"] = round(probe(p), 1)
            if "lufs" not in r:
                r["lufs"] = loudness(p)
            with self.lock:
                self.data[p] = r
                self.busy = None
                self.version += 1
            self._save()
            time.sleep(0.05 if quick else 0.5)  # let the station breathe between tracks


if __name__ == "__main__":
    if sys.argv[1:2] == ["--json"]:
        print(json.dumps(analyse(sys.argv[2])))
        sys.exit(0)
    for f in sys.argv[1:]:
        t0 = time.time()
        print(analyse(f), f"{time.time() - t0:.1f} s", os.path.basename(f))
