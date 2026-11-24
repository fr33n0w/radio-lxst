#!/usr/bin/env python3
# radio-lxst client: tunes in to a radio-lxst station (or any LXST phone) and plays it with a deep buffer.

import os
import sys
import json
import time
import types
import argparse
import threading
from collections import deque

import numpy as np
import RNS
from RNS.vendor import umsgpack as mp

try:
    import pycodec2  # noqa: F401
except Exception:
    sys.modules["pycodec2"] = types.ModuleType("pycodec2")

from LXST.Codecs import Opus

__version__ = "0.7"

STATION_ASPECT = "radiolxst.station"
FIELD_SIGNALLING = 0x00
FIELD_FRAMES = 0x01
FIELD_META = "radiolxst"  # station info inside the call, ignored by plain LXST phones
FIELD_LISTENER = "radiolxst_listener"  # our name, sent to the station inside the call
STATUS_BUSY, STATUS_REJECTED, STATUS_AVAILABLE, STATUS_RINGING = 0x00, 0x01, 0x03, 0x04
STATUS_CONNECTING, STATUS_ESTABLISHED = 0x05, 0x06
PREFERRED_MODE, PREFERRED_PROFILE = 0xF0, 0xFF
MODE_HALF_DUPLEX = 0x02
SAMPLE_RATE = 48000
CHANNELS = 2

PROFILES = {"SHQ": 0x60, "HQ": 0x50, "MQ": 0x40, "LBW": 0x30, "VLBW": 0x20, "ULBW": 0x10}

CARD_BUFFER_S = 0.5             # sound card buffer: covers the 150-200 ms the audio thread waits for the GIL during a stamp
CARD_KEEP_S = 30                # card left open (silent) this long after Stop
CARD_DRY_S = 0.015              # card silent longer than this = a gap heard (counted as CARD)
STALL_LOG_S = 0.08              # client process frozen longer than this: logged with the time
DRIFT_RATE = 0.002              # frames played this much shorter/longer to follow the card clock (inaudible)
DRIFT_MARGIN_S = 0.5            # starts when the buffer is this far from the chosen size
DRIFT_STOP_S = 0.1              # stops when back this close
PATH_WAIT = 20                  # seconds for a path answer
ANSWER_WAIT = 30                # seconds for the station to answer
DIAL_TRIES = 2                  # second try drops the path first: the first one can be stale
RETRY_WAITS = (15, 30, 60, 120)  # pauses before redialing after a drop while playing
HANGUP_WAITS = (15, 30, 60)     # same after the station hung up, if reconnect is on
NO_AUDIO_S = 20                 # playing but no audio this long = lost (RNS notices a dead link much later)
STEADY_S = 60                   # this much good listening resets the retry count
STATUS_EVERY = 180              # seconds between status requests to one station
STATUS_BACKOFF_MAX = 20         # no reply: the wait doubles up to this many times STATUS_EVERY (path requests flood)
STATUS_MIN_GAP = 15             # Refresh button: not more often than this per station
STATUS_WINDOW = 24 * 3600       # only stations heard (announce or answer) in the last day are asked
STATUS_PARALLEL = 1             # status requests at once: parallel path/link stamps paused the audio 80-150 ms
STATUS_TIMEOUT = 25             # path + link + answer
NO_ANSWER = "no answer"


def drop_path(dh):
    """Forget a path here and in the shared rnsd (like rnpath -d), so the next request gets a fresh one."""
    try:
        RNS.Reticulum.get_instance().drop_path(dh)
    except Exception:
        pass
    try:
        with RNS.Transport.path_table_lock:
            RNS.Transport.path_table.pop(dh, None)
    except Exception:
        pass


class _Sink:
    channels = CHANNELS
    samplerate = SAMPLE_RATE


_com_patched = False


def _com_patch(soundcard):
    """LXST sets COM up on a thread after a WMI query (platform.win32_ver, COM itself on Python 3.12) and shuts it
    down at every player close: both crashed the process now and then. The version comes without COM, COM stays up."""
    global _com_patched
    if _com_patched:
        return
    _com_patched = True
    import types
    ver = (str(sys.getwindowsversion().major), "", "", "")
    soundcard.platform = types.SimpleNamespace(win32_ver=lambda: ver)
    soundcard._com.release_com = lambda: None
    soundcard._Player.play = _safe_play


def _safe_play(self, data):
    """LXST's play() releases all the free card space even when the data is shorter: the rest is played as
    garbage (a click per frame not a multiple of the card period, a long gap after any pause). Write only our data."""
    import numpy
    data = numpy.ascontiguousarray(data, dtype="float32")
    if data.ndim == 1:
        data = data[:, None]
    nch = len(set(self.channelmap))
    if data.shape[1] == 1 and nch != 1:
        data = numpy.tile(data, [1, nch])
    data = data[:, sorted(range(len(self.channelmap)), key=lambda k: self.channelmap[k])]
    if data.shape[1] != nch:
        raise TypeError(f"second dimension of data must be equal to the number of channels, not {data.shape[1]}")
    from LXST.Platforms.windows.soundcard import _ffi
    room = min(2400, max(1, self.buffersize // 2))
    while len(data):
        # wait for room for 50 ms (or the rest): every card call gives up the GIL, few calls = few waits for it
        free = self._render_available_frames()
        if free < min(len(data), room):
            time.sleep(0.005)
            continue
        towrite = min(free, len(data))
        raw = numpy.ascontiguousarray(data[:towrite]).tobytes()
        buffer = self._render_buffer(towrite)
        _ffi.memmove(buffer[0], raw, len(raw))
        self._render_release(towrite)
        data = data[towrite:]


def _com_init():
    # LXST's Windows backend only sets up COM in the thread that imports it first
    if sys.platform == "win32":
        from LXST.Platforms.windows import soundcard
        _com_patch(soundcard)
        soundcard._com.init_com()


class _AudioLock:
    """LXST's Windows soundcard crashes if two threads enumerate or open devices at once, also station and client
    opening the same card from two processes: a thread lock plus a Windows mutex shared by our processes."""

    def __init__(self):
        self._lock = threading.RLock()
        self._owned = []  # per nested enter, only touched with _lock held
        self._mutex = None
        if sys.platform == "win32":
            try:
                import ctypes
                self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                self._k32.CreateMutexW.restype = ctypes.c_void_p
                self._k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
                self._k32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
                self._mutex = self._k32.CreateMutexW(None, False, "Local\\radio-lxst-audio")
            except Exception:
                self._mutex = None

    def __enter__(self):
        self._lock.acquire()
        owned = False
        if self._mutex:
            # 0 = ours, 0x80 = left by a process that died with it: ours too; on timeout go on unlocked
            owned = self._k32.WaitForSingleObject(self._mutex, 5000) in (0, 0x80)
        self._owned.append(owned)
        return self

    def __exit__(self, *exc):
        if self._owned.pop():
            self._k32.ReleaseMutex(self._mutex)
        self._lock.release()


AUDIO_LOCK = _AudioLock()


class locked_audio:
    """Create, enter and exit an LXST player/recorder under AUDIO_LOCK; play/record stay unlocked."""

    def __init__(self, make):
        self.make = make

    def __enter__(self):
        with AUDIO_LOCK:
            _com_init()
            self.cm = self.make()
            return self.cm.__enter__()

    def __exit__(self, *exc):
        with AUDIO_LOCK:
            return self.cm.__exit__(*exc)


_com_jobs = None


def com_call(fn, timeout=5):
    """Run fn on one long-lived audio thread: keeps COM alive on Windows, the GUI thread never touches it."""
    global _com_jobs
    if _com_jobs is None:
        import queue
        _com_jobs = queue.Queue()

        def worker():
            _com_init()
            while True:
                f, box, done = _com_jobs.get()
                try:
                    with AUDIO_LOCK:
                        box.append(f())
                except Exception as e:
                    RNS.log(f"Audio device error: {e}", RNS.LOG_WARNING)
                done.set()

        threading.Thread(target=worker, daemon=True).start()
    box, done = [], threading.Event()
    _com_jobs.put((fn, box, done))
    done.wait(timeout)
    return box[0] if box else None


def list_devices():
    """Output device names."""
    def job():
        from LXST.Sinks import Backend
        return [s.name for s in Backend().all_speakers()]
    return com_call(job) or []


def fit_channels(f, ch):
    """Stereo frame -> device channel count (mono sink = average, surround = L/R on the front pair)."""
    ch = ch if isinstance(ch, int) else len(ch)
    if ch == f.shape[1]:
        return f
    if ch == 1:
        return f.mean(axis=1, keepdims=True)
    out = np.zeros((f.shape[0], ch), dtype=f.dtype)
    n = min(ch, f.shape[1])
    out[:, :n] = f[:, :n]
    return out


_players = [None]  # [watch started] + Players, for the stall watch


def stall_watch():
    """Logs when the whole process froze (GIL held, PC busy), with the time to match a click heard. Longer than
    the card buffer while playing = the card ran dry: counted as CARD (the card's own padding is not exact enough)."""
    if _players[0]:
        return
    _players[0] = True

    def run():
        while True:
            t = time.perf_counter()
            time.sleep(0.01)
            late = time.perf_counter() - t - 0.01
            if late > STALL_LOG_S:
                heard = [p for p in _players[1:] if p.state == "playing" and late - p.card_s > CARD_DRY_S]
                dry = late - min((p.card_s for p in heard), default=CARD_BUFFER_S)
                for p in heard:
                    p.card_gaps += 1
                RNS.log(f"Client paused {late * 1000:.0f} ms" + (f", sound card silent ~{dry * 1000:.0f} ms"
                                                                 if heard else ""), RNS.LOG_WARNING)

    threading.Thread(target=run, daemon=True).start()


class _Resampler:
    """Windowed-sinc resampler that keeps its phase across frames (per-frame linear interpolation wobbled the treble)."""
    HALF = 12
    PHASES = 512
    _table = None

    def __init__(self, channels):
        self.channels = channels
        self.buf = np.zeros((self.HALF, channels), dtype=np.float32)
        self.pos = float(self.HALF)  # input index of the next output sample
        self.taps = np.arange(-self.HALF + 1, self.HALF + 1)
        if _Resampler._table is None:
            d = (np.arange(self.PHASES + 1) / self.PHASES)[:, None] - self.taps[None, :]
            w = np.sinc(d) * (0.42 + 0.5 * np.cos(np.pi * d / self.HALF) + 0.08 * np.cos(2 * np.pi * d / self.HALF))
            _Resampler._table = (w / w.sum(axis=1, keepdims=True)).astype(np.float32)

    def run(self, f, step):
        self.buf = np.concatenate([self.buf, f.astype(np.float32)])
        last = len(self.buf) - self.HALF  # an output sample needs HALF input samples after it
        k = max(0, int(np.ceil((last - self.pos) / step)))
        p = self.pos + step * np.arange(k)
        p = p[p < last]
        base = np.floor(p).astype(int)
        w = self._table[np.rint((p - base) * self.PHASES).astype(int)]
        idx = base[:, None] + self.taps[None, :]
        out = np.stack([(w * self.buf[:, c][idx]).sum(axis=1) for c in range(self.channels)], axis=1)
        self.pos = (p[-1] + step) if len(p) else self.pos
        drop = int(np.floor(self.pos)) - self.HALF
        if drop > 0:
            self.buf = self.buf[drop:]
            self.pos -= drop
        return out


class Player:
    """Jitter buffer + sound card output. Starts after `prefill` seconds of audio, rebuffers on underrun."""

    def __init__(self, prefill=2.0, device=None, max_extra=4.0):
        self.prefill = prefill
        self.max_extra = max_extra
        self.device = device
        self.volume_db = 0.0
        self.muted = False
        self.frames = deque()
        self.buffered = 0
        self.lock = threading.Lock()
        self.state = "idle"
        self.underruns = 0
        self.card_gaps = 0  # sound card ran dry while our buffer had audio (CPU busy): heard, not a network drop
        self.overflows = 0  # frames thrown away because the buffer was full
        self.drift_s = 0.0  # s taken out (+) or added (-) to follow the card's clock
        self._step = 1.0
        self._rs = None
        self.levels = (0.0, 0.0)
        self.card_s = CARD_BUFFER_S
        self.out_power = 0.0  # what reaches the card, 3 s average (L+R power, like the station's OUT)
        self.running = False
        self._run_id = 0
        self._wake = threading.Event()
        self._thread = None

    @property
    def out_db(self):
        return -0.691 + 10 * np.log10(self.out_power) if self.state == "playing" and self.out_power > 1e-9 else None

    @property
    def buffer_s(self):
        return self.buffered / SAMPLE_RATE

    def push(self, frame):
        # resampled here, not in the audio thread: its many numpy calls each wait for the GIL while a stamp runs
        frame = self._follow_clock(frame)
        with self.lock:
            self.frames.append(frame)
            self.buffered += len(frame)
            limit = (self.prefill + self.max_extra) * SAMPLE_RATE
            while self.buffered > limit and len(self.frames) > 1:
                self.buffered -= len(self.frames.popleft())
                self.overflows += 1  # audio thrown away: heard as a jump

    def _follow_clock(self, f):
        """The card's clock is never the station's: the buffer creeps up (or down) for hours. While it is off the
        chosen size, play DRIFT_RATE faster (slower) through a continuous sinc resampler: no jump, treble intact."""
        off = self.buffer_s - self.prefill if self.state == "playing" else 0.0
        if abs(off) > DRIFT_MARGIN_S:
            self._step = 1 + DRIFT_RATE if off > 0 else 1 - DRIFT_RATE
        elif abs(off) < DRIFT_STOP_S:
            self._step = 1.0
        if self._rs is None or self._rs.channels != f.shape[1]:
            if self._step == 1.0:
                return f
            self._rs = _Resampler(f.shape[1])
        out = self._rs.run(f, self._step)
        self.drift_s += (len(f) - len(out)) / SAMPLE_RATE
        return out.astype(f.dtype)

    def start(self):
        if self.running:
            return
        self.state = "buffering"
        self._step, self._rs = 1.0, None
        self._run_id += 1
        self.running = True
        if self._thread is None:
            stall_watch()
            _players.append(self)
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
        self._wake.set()

    def _worker(self):
        # one audio thread for the whole life: no overlapping players, no reused COM thread ids
        _com_init()
        while True:
            self._wake.wait()
            self._wake.clear()
            if self.running:
                self._run()

    def stop(self):
        self.running = False
        with self.lock:
            self.frames.clear()
            self.buffered = 0
        self.state = "idle"
        self.levels = (0.0, 0.0)

    def _pop(self):
        with self.lock:
            if not self.frames:
                return None
            f = self.frames.popleft()
            self.buffered -= len(f)
            return f

    @staticmethod
    def _card_empty(player):
        try:
            return player.currentpadding == 0  # Windows only: samples still queued in the card
        except Exception:
            return False

    def _run(self):
        device = self.device
        try:
            from LXST.Sinks import Backend
            with AUDIO_LOCK:
                backend = Backend(preferred_device=device)
        except Exception as e:
            RNS.log(f"No audio output: {e}", RNS.LOG_ERROR)
            self.state = "no audio output"
            return
        # sound card buffer: at 20 ms a busy PC let it run dry (clicks with a full jitter buffer)
        with locked_audio(lambda: backend.get_player(samples_per_frame=int(SAMPLE_RATE * self.card_s))) as player:
            while True:
                self._play(player, self._run_id)
                # the card stays open a while: closing and opening it again soon after crashed LXST on Windows
                idle = time.monotonic()
                while not self.running and time.monotonic() - idle < CARD_KEEP_S:
                    self._wake.wait(0.5)
                if not self.running or self.device != device:
                    break
                self._wake.clear()
        if self.running:
            self._wake.set()  # another device: the worker opens it

    def _play(self, player, run_id):
        target = self.prefill
        fed = False
        while self.running and run_id == self._run_id:
            if self.state == "buffering":
                if self.buffer_s >= target:
                    self.state = "playing"
                else:
                    time.sleep(0.05)
                    continue
            f = self._pop()
            if f is None:
                self.underruns += 1
                self.state = "buffering"
                self.levels = (0.0, 0.0)
                target = max(0.5, self.prefill / 2)  # refill faster after a dropout
                fed = False  # the card empties while we refill: not a card gap
                continue
            if self.muted:
                f = np.zeros_like(f)  # still paced and consumed: unmute is live, no delay
            elif self.volume_db:
                f = np.clip(f * 10 ** (self.volume_db / 20), -1.0, 1.0)
            peak = np.abs(f).max(axis=0)
            self.levels = (float(peak[0]), float(peak[-1]))
            self.out_power += (float(np.mean(f * f)) * 2 - self.out_power) * min(1.0, len(f) / SAMPLE_RATE / 3)
            if fed and self._card_empty(player):
                self.card_gaps += 1
            player.play(fit_channels(f, getattr(player, "channels", CHANNELS)).astype(np.float32))
            fed = True


_monitor = None


def monitor_player(device=None):
    """The station's monitor, one Player reused: a new one per toggle opened the card while the old one closed it."""
    global _monitor
    if _monitor is None:
        _monitor = Player(prefill=0.3, device=device, max_extra=0.5)
        _monitor.card_s = 0.2  # the station's monitor: low delay
    _monitor.device = device
    return _monitor


def announce_emitted(destination_hash):
    """Emission time of the announce just received (signed by the station, repeaters cannot change it), 0 if unknown."""
    try:
        blobs = RNS.Transport.path_table[destination_hash][4]  # IDX_PT_RANDBLOBS
        return int.from_bytes(blobs[-1][5:10], "big") if blobs else 0
    except Exception:
        return 0


class Station:
    def __init__(self, call_id, name="", listeners=None, now_playing="", kbps=None, on_air=True):
        self.call_id, self.name, self.listeners = call_id, name, listeners
        self.now_playing, self.kbps, self.on_air = now_playing, kbps, on_air
        self.slots = 0  # 0 = no limit or unknown
        self.emitted = 0  # when the station sent its last announce (its own clock)
        self.legacy = False  # before 0.4 the state came in the announce, newer stations answer a status request
        self.checked = 0  # last status request (answered or not)
        self.fails = 0  # status requests in a row without a reply
        self.version = ""  # radio-lxst version the station sent (announce or status)
        self.fav = False  # starred by the user: listed first
        self.heard = time.time()

    @property
    def listeners_text(self):
        if self.listeners is None:
            return ""
        return f"{self.listeners}/{self.slots}" if self.slots else str(self.listeners)

    @property
    def quality(self):
        # what the station streams: High 60 ms packets (56 kbit/s max), Steady 120 ms (28)
        if not self.kbps:
            return ""
        return "High" if self.kbps >= 56 else "Steady"

    @property
    def hops(self):
        dh = RNS.Destination.hash_from_name_and_identity("lxst.telephony", bytes.fromhex(self.call_id))
        return RNS.Transport.hops_to(dh) if RNS.Transport.has_path(dh) else None

    def to_dict(self):
        return {"call_id": self.call_id, "name": self.name, "listeners": self.listeners,
                "now_playing": self.now_playing, "kbps": self.kbps, "on_air": self.on_air, "heard": self.heard,
                "slots": self.slots, "emitted": self.emitted, "legacy": self.legacy, "checked": self.checked,
                "version": self.version, "fav": self.fav}


class Directory:
    """Stations heard through radiolxst.station announces, kept on disk between runs."""

    aspect_filter = STATION_ASPECT

    def __init__(self, path):
        self.path = path
        self.stations = {}
        self.on_change = None
        self.on_heard = None  # (call_id): a station really spoke (new announce)
        self._busy = set()
        self._busy_lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                for d in json.load(f):
                    s = Station(d["call_id"], d.get("name", ""), d.get("listeners"), d.get("now_playing", ""),
                                d.get("kbps"), d.get("on_air", True))
                    s.heard = d.get("heard", 0)
                    s.slots = d.get("slots", 0) or 0
                    s.emitted = d.get("emitted", 0) or 0
                    s.legacy = bool(d.get("legacy", False))
                    s.checked = d.get("checked", 0) or 0
                    s.version = str(d.get("version") or "")
                    s.fav = bool(d.get("fav", False))
                    self.stations[s.call_id] = s
        except Exception:
            pass
        RNS.Transport.register_announce_handler(self)

    def received_announce(self, destination_hash, announced_identity, app_data):
        try:
            cid = announced_identity.hash.hex()
            emitted = announce_emitted(destination_hash)
            old = self.stations.get(cid)
            if emitted and old and emitted <= old.emitted:
                # a repeater replaying an announce we already had: the station did not speak again
                RNS.log(f"Ignored replayed announce of {old.name or cid} "
                        f"(sent {time.strftime('%d/%m %H:%M', time.localtime(emitted))})", RNS.LOG_INFO)
                return
            if not app_data:
                # path response without data: only refresh, never wipe what we know
                if old:
                    old.heard, old.emitted = time.time(), max(old.emitted, emitted)
                return
            info = mp.unpackb(app_data)
            if not isinstance(info, dict):
                return
            if "air" in info:
                # station before 0.4: its state is in the announce
                s = Station(cid, str(info.get("n", ""))[:48], info.get("l"), str(info.get("np", ""))[:64],
                            info.get("kb"), bool(info.get("air", True)))
                s.slots = info.get("s") or 0
                s.fav = bool(old and old.fav)
                s.legacy = True
                RNS.log(f"Heard station {s.name or cid} ({'on air' if s.on_air else 'off air'})", RNS.LOG_NOTICE)
            else:
                # the announce only says the station exists: its state is asked (status request)
                s = old or Station(cid, on_air=False)
                s.name, s.kbps, s.legacy, s.heard = str(info.get("n", ""))[:48], info.get("kb"), False, time.time()
                s.checked, s.fails = 0, 0  # ask now
                RNS.log(f"Heard station {s.name or cid}", RNS.LOG_NOTICE)
            s.emitted = emitted
            s.version = str(info.get("v") or s.version)[:16]
            self.stations[cid] = s
            self.save()
            if callable(self.on_change):
                self.on_change()
            if callable(self.on_heard):
                self.on_heard(cid)
        except Exception as e:
            RNS.log(f"Bad station announce: {e}", RNS.LOG_DEBUG)

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump([s.to_dict() for s in list(self.stations.values())], f, indent=1)
        except OSError:
            pass

    # live state: asked to the station (GET "status" over a Link), never put in announces
    def poll(self, skip=()):
        """Ask the stations heard lately for their state, a few at a time, each every STATUS_EVERY s."""
        now = time.time()
        with self._busy_lock:
            for s in sorted(list(self.stations.values()), key=lambda s: s.checked):
                if len(self._busy) >= STATUS_PARALLEL:
                    break
                wait = STATUS_EVERY * min(2 ** s.fails, STATUS_BACKOFF_MAX)
                if s.legacy or s.call_id in skip or s.call_id in self._busy or now - s.heard > STATUS_WINDOW \
                        or now - s.checked < wait:
                    continue
                self._busy.add(s.call_id)
                s.checked = now
                threading.Thread(target=self._check, args=(s,), daemon=True).start()

    def refresh(self, skip=()):
        """Refresh button: ask again now (not more than once every STATUS_MIN_GAP s per station)."""
        for s in list(self.stations.values()):
            if not s.legacy and time.time() - s.checked >= STATUS_MIN_GAP:
                s.checked, s.fails = 0, 0
        self.poll(skip)

    def _check(self, s):
        try:
            # first failure only: later tries already ask a fresh path, no extra path requests for dead stations
            st = ask_status(s.call_id, drop_stale=s.now_playing != "no reply")
            if st == NO_ANSWER:
                # link up, request not answered: a station before 0.4 (or busy), keep what we know
                RNS.log(f"Station {s.name or s.call_id}: link ok, no status answer", RNS.LOG_INFO)
            elif st is None:
                if s.on_air or s.listeners or s.now_playing != "no reply":
                    RNS.log(f"Station {s.name or s.call_id}: no reply to status", RNS.LOG_INFO)
                s.on_air, s.listeners, s.now_playing = False, None, "no reply"
                s.fails += 1
            else:
                s.on_air, s.listeners = bool(st.get("air")), st.get("l")
                s.now_playing = str(st.get("np") or "")[:64] if s.on_air else "off air"
                s.slots = st.get("s") or 0
                s.kbps = st.get("kb", s.kbps)
                s.name = str(st.get("n") or s.name)[:48]
                s.heard, s.fails = time.time(), 0
                s.version = str(st.get("v") or s.version)[:16]
            self.save()
            if callable(self.on_change):
                self.on_change()
        finally:
            with self._busy_lock:
                self._busy.discard(s.call_id)

    def add(self, call_id):
        """A Call ID typed in (seen on a map, announce never heard here): listed now, name and state asked at once.
        Returns False if it is not a Call ID, None if already listed."""
        cid = call_id.strip().lower()
        if len(cid) != 32 or any(ch not in "0123456789abcdef" for ch in cid):
            return False
        if cid in self.stations:
            return None
        s = self.stations[cid] = Station(cid, on_air=False)
        s.now_playing, s.checked = "asking...", 0
        self.save()
        if callable(self.on_change):
            self.on_change()
        return True

    def mark_off(self, call_id):
        """The station hung up or refused us: show it off air until it announces again."""
        s = self.stations.get(call_id)
        if s and (s.on_air or s.listeners or s.now_playing != "offline"):
            s.on_air, s.listeners, s.now_playing = False, 0, "offline"
            self.save()

    def name_of(self, call_id):
        s = self.stations.get(call_id)
        return s.name if s else ""


def ask_status(call_id, timeout=STATUS_TIMEOUT, drop_stale=False):
    """The station's live state (dict of the in-call info), NO_ANSWER if the link came up but the request
    was not answered, None if the station cannot be reached (drop_stale: then forget the path, it may be old)."""
    try:
        ident_hash = bytes.fromhex(call_id)
        dh = RNS.Destination.hash_from_name_and_identity(STATION_ASPECT, ident_hash)
    except ValueError:
        return None
    end = time.time() + timeout
    if not RNS.Transport.has_path(dh):
        RNS.Transport.request_path(dh)
        while not RNS.Transport.has_path(dh) and time.time() < end:
            time.sleep(0.25)
    ident = RNS.Identity.recall(dh)
    if ident is None or ident.hash != ident_hash:
        return None
    dest = RNS.Destination(ident, RNS.Destination.OUT, RNS.Destination.SINGLE, *STATION_ASPECT.split("."))
    box, done = [], threading.Event()
    link = RNS.Link(dest)
    try:
        while link.status != RNS.Link.ACTIVE and link.status != RNS.Link.CLOSED and time.time() < end:
            time.sleep(0.1)
        if link.status != RNS.Link.ACTIVE:
            if drop_stale:
                drop_path(dh)
            return None
        link.request("status", None, response_callback=lambda r: (box.append(r.response), done.set()),
                     failed_callback=lambda r: done.set(), timeout=max(1.0, end - time.time()))
        done.wait(max(0.5, end - time.time()) + 1)
        return box[0] if box and isinstance(box[0], dict) else NO_ANSWER
    finally:
        try:
            link.teardown()
        except Exception:
            pass


def default_listener_name(identity):
    return f"Radio-LXST Client #{identity.hash.hex()[:4]}" if identity else "Radio-LXST Client"


class Client:
    def __init__(self, identity, prefill=3.0, device=None, nick=""):
        self.identity = identity
        self.nick = nick
        self.player = Player(prefill, device)
        self.link = None
        self.state = "idle"
        self.message = ""
        self.call_id = None
        self.station_name = ""
        self.profile = PROFILES["SHQ"]
        self.started = None
        self.bytes = 0
        self.frames = 0
        self.decoder = None
        self._gen = 0
        self.meta = {}
        self.retries = 0
        self.waits = RETRY_WAITS
        self.reconnect = False  # station hung up: try HANGUP_WAITS times before calling it offline
        self._back = 0.0  # when the station spoke again while we retry after its hang up: try now
        self.last_audio = 0.0
        self._retry_lock = threading.Lock()
        self._lock = threading.RLock()  # tune/hangup vs packets of the old link

    @property
    def listener_name(self):
        return (self.nick or "").strip()[:32] or default_listener_name(self.identity)

    def set_nick(self, nick):
        """The name the station shows for us; sent now if a call is on."""
        if (nick or "").strip() == (self.nick or "").strip():
            return
        self.nick = nick
        link = self.link
        if link is not None and self.state == "playing":
            self._send_name(link)

    def _send_name(self, link):
        try:
            RNS.Packet(link, mp.packb({FIELD_LISTENER: {"n": self.listener_name}}), create_receipt=False).send()
        except Exception:
            pass

    def tune(self, call_id, profile=None, name="", why=""):
        self.hangup()
        with self._lock:
            self._gen += 1
            gen = self._gen
            self.call_id = call_id.strip().lower()
            self.station_name = name
            self.meta = {}
            self.prev_meta = ({}, 0)
            self.np_heard_at = 0
        RNS.log(f"Tuning to {self.call_id}" + (f" ({why})" if why else ""), RNS.LOG_NOTICE)
        if profile is not None:
            self.profile = profile
        self.retries, self.waits, self._back = 0, RETRY_WAITS, 0.0
        self.state, self.message = "searching", "looking for a path"
        threading.Thread(target=self._dial, args=(gen,), daemon=True).start()

    def _dial(self, gen):
        try:
            ident_hash = bytes.fromhex(self.call_id)
            if len(ident_hash) != RNS.Reticulum.TRUNCATED_HASHLENGTH // 8:
                raise ValueError
        except ValueError:
            self._fail("invalid Call ID")
            return
        dh = RNS.Destination.hash_from_name_and_identity("lxst.telephony", ident_hash)
        why = None
        hung = self.retries and self.waits is HANGUP_WAITS  # one try per retry, fresh path from the second
        for i in range(1 if hung else DIAL_TRIES):
            why = self._attempt(gen, dh, fresh=i > 0 or (hung and self.retries > 1))
            if why == "station off air":
                break  # after a hang up: off air now, maybe back on air in a moment
            if why in (None, "ok", "rejected"):
                return
        if self.retries:
            self._schedule_retry(gen, why)
        else:
            self._fail(why)

    def _attempt(self, gen, dh, fresh):
        """One try: path (a fresh one if asked) + call. Returns ok, rejected, a failure reason, or None if cancelled."""
        self._back = 0.0
        if fresh:
            self.state, self.message = "searching", "asking for a fresh path"
            drop_path(dh)
        if not RNS.Transport.has_path(dh):
            RNS.Transport.request_path(dh)
            t = time.time()
            while not RNS.Transport.has_path(dh) and time.time() - t < PATH_WAIT:
                if gen != self._gen:
                    return None
                time.sleep(0.2)
        remote = RNS.Identity.recall(dh)
        if gen != self._gen:
            return None
        if not remote:
            return "no path to the station"
        if remote.hash.hex() != self.call_id:
            RNS.log(f"Path for {self.call_id} gave identity {remote.hash.hex()}", RNS.LOG_WARNING)
            return "wrong identity on the path"
        self.state, self.message = "connecting", (f"{RNS.Transport.hops_to(dh)} hops"
                                                  if RNS.Transport.has_path(dh) else "")
        dest = RNS.Destination(remote, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxst", "telephony")
        self.decoder = Opus()
        self.decoder.sink = _Sink()
        self.bytes = self.frames = 0
        link = RNS.Link(dest, closed_callback=lambda l: self._closed(l, gen))
        link.set_packet_callback(lambda data, packet: self._packet(link, data, gen))
        self.link = link
        t = time.time()
        while gen == self._gen and self.state in ("connecting", "ringing") and time.time() - t < ANSWER_WAIT \
                and link.status != RNS.Link.CLOSED:
            if self.state == "connecting" and self._back > t:
                RNS.log("The station is back: trying again now", RNS.LOG_NOTICE)
                break  # this link went out on the old path, the station just spoke on a new one
            time.sleep(0.2)
        if gen != self._gen:
            return None
        if self.state == "playing":
            return "ok"
        if self.state == "failed":
            return "rejected"
        off_air = self.state == "retrying" and self.message == "station off air"
        self.link = None
        try:
            link.teardown()
        except Exception:
            pass
        return "station off air" if off_air else "no answer"

    def _signal(self, link, signals):
        RNS.Packet(link, mp.packb({FIELD_SIGNALLING: signals}), create_receipt=False).send()

    def set_profile(self, profile):
        """New quality: kept for the next tune, and asked on the open call (the station switches codec)."""
        if profile == self.profile:
            return
        self.profile = profile
        link = self.link
        if self.state == "playing" and link is not None and link.status == RNS.Link.ACTIVE:
            try:
                self._signal(link, [PREFERRED_PROFILE + profile])
                RNS.log(f"Quality {profile:#04x} asked to the station", RNS.LOG_NOTICE)
            except Exception as e:
                RNS.log(f"Quality change failed: {e}", RNS.LOG_WARNING)

    def _packet(self, link, data, gen):
        if gen != self._gen:
            return
        try:
            msg = mp.unpackb(data)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        with self._lock:
            if gen == self._gen and link is self.link:
                self._handle(link, msg, gen, len(data))

    def _handle(self, link, msg, gen, size):
        for s in msg.get(FIELD_SIGNALLING, []) if isinstance(msg.get(FIELD_SIGNALLING), list) else []:
            if s == STATUS_AVAILABLE:
                link.identify(self.identity)
            elif s == STATUS_RINGING:
                self.state, self.message = "ringing", ""
                self._signal(link, [PREFERRED_PROFILE + self.profile, PREFERRED_MODE + MODE_HALF_DUPLEX])
            elif s == STATUS_ESTABLISHED:
                self.state, self.message = "playing", ""
                self.started = self.last_audio = time.time()
                self.player.start()
                self._send_name(link)
                threading.Thread(target=self._watch, args=(gen, link), daemon=True).start()
                RNS.log(f"Tuned in to {self.station_name or self.call_id}", RNS.LOG_NOTICE)
        meta = msg.get(FIELD_META)
        if isinstance(meta, dict):
            if meta.get("np") != self.meta.get("np"):
                self.prev_meta = (self.meta, getattr(self, "meta_t", 0))  # still heard until the buffer plays out
                try:
                    late = max(0.0, -float(meta.get("tp") or 0))  # the new track starts later at the station
                except (TypeError, ValueError):
                    late = 0.0
                self.np_heard_at = time.monotonic() + self.player.buffer_s + late
            self.meta, self.meta_t = meta, time.monotonic()
            if meta.get("n"):
                self.station_name = str(meta["n"])[:48]
        for s in msg.get(FIELD_SIGNALLING, []) if isinstance(msg.get(FIELD_SIGNALLING), list) else []:
            if s == STATUS_BUSY:
                m = self.meta
                full = f" ({m['l']}/{m['s']})" if m.get("s") and m.get("l") is not None else ""
                self._fail(f"station full{full}, try later")
            elif s == STATUS_REJECTED:
                if self.retries and self.waits is HANGUP_WAITS:
                    self.state, self.message = "retrying", "station off air"  # keep trying after a hang up
                else:
                    self._fail("station off air")
        frames = msg.get(FIELD_FRAMES)
        if frames is not None:
            for f in frames if isinstance(frames, list) else [frames]:
                if not f or f[0] != 0x01:
                    continue  # only Opus
                try:
                    pcm = self.decoder.decode(f[1:])
                    self.player.push(pcm)
                    self.last_audio = time.time()
                    self.frames += 1
                    self.bytes += size
                except Exception as e:
                    RNS.log(f"Decode error: {e}", RNS.LOG_DEBUG)

    def _closed(self, link, gen):
        if gen != self._gen or link is not self.link:
            return
        if self.state != "playing":
            return  # while connecting, _attempt sees the closed link
        self.player.stop()
        if link.teardown_reason == RNS.Link.TIMEOUT:
            RNS.log("Lost the station (network)", RNS.LOG_NOTICE)
            self._schedule_retry(gen, "lost the station", link)
            return
        RNS.log("Station closed the link", RNS.LOG_NOTICE)
        if self.reconnect:
            self.retries, self.waits = 0, HANGUP_WAITS
            self._schedule_retry(gen, "the station hung up", link)
            return
        self.state, self.message = "ended", "the station hung up"

    def _watch(self, gen, link):
        while gen == self._gen and self.link is link and self.state == "playing":
            time.sleep(1)
            if gen != self._gen or self.link is not link or self.state != "playing":
                return  # dropped while asleep: the retry count is not ours to reset any more
            now = time.time()
            if self.retries and now - self.started > STEADY_S:
                self.retries, self.waits = 0, RETRY_WAITS
            if now - self.last_audio > NO_AUDIO_S and gen == self._gen and self.link is link:
                RNS.log(f"No audio for {NO_AUDIO_S} s", RNS.LOG_NOTICE)
                self._schedule_retry(gen, "no audio from the station", link)
                return

    def _schedule_retry(self, gen, why, dropped=None):
        """After a drop while playing: try again with growing pauses, then give up."""
        with self._retry_lock:
            if gen != self._gen or (dropped is not None and dropped is not self.link):
                return  # cancelled, or this drop is already being handled
            if self.retries >= len(self.waits) and not self._back:
                if self.waits is HANGUP_WAITS:
                    # tried enough: say what the station really is now
                    self._fail("station off air" if why == "station off air" else "station offline")
                else:
                    self._fail(f"{why}, gave up after {len(self.waits)} retries")
                return
            if self.retries >= len(self.waits):
                wait = 0  # out of retries, but the station just spoke: one more, now
            else:
                wait = self.waits[self.retries]
                self.retries += 1
            link, self.link = self.link, None
            self.state = "retrying"
        if link:
            try:
                link.teardown()
            except Exception:
                pass
        self.player.stop()
        self.state = "retrying"
        RNS.log(f"{why}: retry {self.retries}/{len(self.waits)} in {wait} s", RNS.LOG_NOTICE)
        threading.Thread(target=self._retry, args=(gen, wait), daemon=True).start()

    def station_back(self, call_id):
        """An announce or on-air message from the station we retry after its hang up: no more waiting."""
        if self.reconnect and self.waits is HANGUP_WAITS and self.retries and call_id == self.call_id \
                and self.state in ("retrying", "searching", "connecting"):
            self._back = time.time()

    def _retry(self, gen, wait):
        end = time.time() + wait
        while time.time() < end and not self._back:
            if gen != self._gen:
                return
            self.message = f"retry {self.retries}/{len(self.waits)} in {end - time.time():.0f} s"
            time.sleep(0.5)
        if gen != self._gen:
            return
        self.state, self.message = "searching", f"retry {self.retries}/{len(self.waits)}"
        self._dial(gen)

    def _fail(self, why):
        self.state, self.message = "failed", why
        RNS.log(f"Tune failed: {why}", RNS.LOG_WARNING)
        self.player.stop()
        if self.link:
            try:
                self.link.teardown()
            except Exception:
                pass

    def hangup(self):
        with self._lock:
            self._gen += 1
        if self.link:
            try:
                self.link.teardown()
            except Exception:
                pass
            self.link = None
        self.player.stop()
        if self.state not in ("idle", "failed"):
            self.state, self.message = "idle", ""

    def _heard(self, meta, meta_t):
        # raw position of a meta's track at the speaker: the station sends it buffer_s earlier
        try:
            tp, td = float(meta["tp"]), float(meta["td"])
        except (KeyError, TypeError, ValueError):
            return None
        return (tp + time.monotonic() - meta_t - self.player.buffer_s, td) if td > 0 else None

    def shown_meta(self, meta):
        """(meta, received at) of what is heard now: the old track or source until the new one reaches the speaker."""
        prev, prev_t = getattr(self, "prev_meta", ({}, 0))
        if prev.get("np") and time.monotonic() < getattr(self, "np_heard_at", 0):
            return prev, prev_t
        return meta, getattr(self, "meta_t", 0)

    def heard_track(self, meta, meta_t):
        """(track name, (position, length) or None) as heard here."""
        cur = self._heard(meta, meta_t)
        if self.state != "playing" or not cur:
            return str(meta.get("np") or ""), None
        return str(meta.get("np") or ""), (max(0.0, min(cur[1], cur[0])), cur[1])

    def live_text(self, meta):
        """Moving text for a live station (stream or line input), "" for files or when not playing."""
        np_ = str(meta.get("np") or "")
        if self.state != "playing" or not (np_ == "Live streaming" or np_.startswith("Live streaming - ")):
            return ""
        sn = str(meta.get("sn") or "").strip()[:64]
        title = np_[len("Live streaming - "):].strip()
        return (f"LIVE STREAMING: {sn}" if sn else "LIVE STREAMING") + (f" - {title}" if title else "")

    def snapshot(self):
        el = time.time() - self.started if self.started and self.state == "playing" else 0
        p = self.player
        with self._lock:
            cid, name, meta = self.call_id or "", self.station_name, self.meta
        shown, shown_t = self.shown_meta(meta)
        track, prog = self.heard_track(shown, shown_t)
        return {
            "state": self.state, "message": self.message, "call_id": cid,
            "name": name, "listeners": meta.get("l"), "slots": meta.get("s") or 0, "track": track, "progress": prog,
            "live": self.live_text(shown), "v": str(meta.get("v") or "")[:16],
            "q": str(meta.get("q") or "")[:24], "ms": meta.get("ms"), "kb": meta.get("kb"),  # what this client gets (0.7 stations)
            "time": el, "kbps": self.bytes * 8 / el / 1000 if el > 1 else 0,
            "buffer_s": p.buffer_s, "player": p.state, "underruns": p.underruns, "card_gaps": p.card_gaps, "overflows": p.overflows, "levels": p.levels,
            "hops": (RNS.Transport.hops_to(self.link.destination.hash)
                     if self.link and RNS.Transport.has_path(self.link.destination.hash) else None),
        }


def lock_storage(storage):
    """Only one client per storage folder: returns the lock (keep it open) or None if another client has it."""
    os.makedirs(storage, exist_ok=True)
    f = open(os.path.join(storage, "client.lock"), "a+")
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


ALREADY_OPEN = "radio-lxst client is already open (one client at a time for the same storage folder)"


def load_identity(path):
    if os.path.isfile(path):
        return RNS.Identity.from_file(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)  # a bare file name too
    ident = RNS.Identity()
    ident.to_file(path)
    return ident


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Listen to a radio-lxst station")
    ap.add_argument("call_id", nargs="?", help="station Call ID (identity hash)")
    ap.add_argument("--list", type=int, metavar="SEC", help="listen for station announces for SEC seconds")
    ap.add_argument("--quality", choices=list(PROFILES), default="SHQ")
    ap.add_argument("--buffer", type=float, default=3.0, help="seconds of audio buffered before playing")
    ap.add_argument("--volume", type=float, default=0.0, help="dB")
    ap.add_argument("--device", default=None, help="output device (part of its name)")
    ap.add_argument("--identity", default=os.path.join(here, "storage", "client_identity"))
    ap.add_argument("--reconnect", action="store_true",
                    help=f"if the station hangs up, try {len(HANGUP_WAITS)} times before calling it offline")
    ap.add_argument("--nick", default="", help="your name shown by the station (default Radio-LXST Client #xxxx)")
    ap.add_argument("--cmd", default=None, help="send a command to the station (help, info, status, link, "
                                                 "subscribe, unsubscribe) and print the reply")
    ap.add_argument("--rnsconfig", default=None)
    a = ap.parse_args()
    lock = lock_storage(os.path.join(here, "storage"))
    if lock is None:
        sys.exit(ALREADY_OPEN)

    RNS.Reticulum(configdir=a.rnsconfig, loglevel=RNS.LOG_NOTICE)
    import rns_watch
    rns_watch.start()
    import update_check
    update_check.watch(__version__, lambda v: RNS.log(f"New version {v} on GitHub: {update_check.REPO_URL}",
                                                      RNS.LOG_NOTICE))
    directory = Directory(os.path.join(here, "storage", "stations.json"))
    if a.cmd and a.call_id:
        import client_lxmf
        ident = load_identity(a.identity)
        client_lxmf.ensure_stamp(ident)
        cm = client_lxmf.Commands(ident, os.path.join(here, "storage"), a.nick or default_listener_name(ident),
                                  directory)
        cid = a.call_id.strip().lower()
        n = len(cm.transcript(cid))
        cm.send(cid, a.cmd)
        t = time.time()
        while time.time() - t < client_lxmf.REPLY_WAIT:
            time.sleep(0.5)
            rows = cm.transcript(cid)[n:]
            if any(r["dir"] == "in" for r in rows):
                for r in rows:
                    if r["dir"] == "in":
                        print(r["text"])
                return
            if rows and rows[0]["state"].startswith("failed"):
                sys.exit(f"Command {rows[0]['state']}")
        sys.exit("No reply from the station")
    if a.list or not a.call_id:
        if a.list:
            print(f"Listening for stations for {a.list} s...")
            time.sleep(a.list)
        # their state, asked to each station
        t = time.time()
        for s in directory.stations.values():
            s.checked = 0
        while time.time() - t < 90:
            directory.poll()
            time.sleep(1)
            if not directory._busy and all(s.legacy or s.checked for s in directory.stations.values()
                                           if time.time() - s.heard <= STATUS_WINDOW):
                break
        for s in sorted(directory.stations.values(), key=lambda s: -s.heard):
            print(f"  {s.call_id}  {'ON ' if s.on_air else 'off'}  {s.name}  listeners {s.listeners_text}  "
                  f"{s.now_playing}")
        if not directory.stations:
            print("  no stations heard yet")
        return

    cl = Client(load_identity(a.identity), prefill=a.buffer, device=a.device, nick=a.nick)
    cl.player.volume_db = a.volume
    cl.reconnect = a.reconnect
    directory.on_heard = cl.station_back
    cl.tune(a.call_id, PROFILES[a.quality], directory.name_of(a.call_id.lower()))
    try:
        while True:
            time.sleep(2)
            s = cl.snapshot()
            print(f"\r{s['state']:<10} {s['message'][:24]:<24} buffer {s['buffer_s']:4.1f} s  "
                  f"{s['kbps']:5.1f} kbit/s  dropouts {s['underruns']}  card {s['card_gaps']}   ", end="", flush=True)
            if s["state"] in ("failed", "ended"):
                print()
                break
    except KeyboardInterrupt:
        print()
    cl.hangup()


if __name__ == "__main__":
    main()
