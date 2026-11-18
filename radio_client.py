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

__version__ = "0.3"

STATION_ASPECT = "radiolxst.station"
FIELD_SIGNALLING = 0x00
FIELD_FRAMES = 0x01
FIELD_META = "radiolxst"  # station info inside the call, ignored by plain LXST phones
STATUS_BUSY, STATUS_REJECTED, STATUS_AVAILABLE, STATUS_RINGING = 0x00, 0x01, 0x03, 0x04
STATUS_CONNECTING, STATUS_ESTABLISHED = 0x05, 0x06
PREFERRED_MODE, PREFERRED_PROFILE = 0xF0, 0xFF
MODE_HALF_DUPLEX = 0x02
SAMPLE_RATE = 48000
CHANNELS = 2

PROFILES = {"SHQ": 0x60, "HQ": 0x50, "MQ": 0x40, "LBW": 0x30, "VLBW": 0x20, "ULBW": 0x10}

PATH_WAIT = 20                  # seconds for a path answer
ANSWER_WAIT = 30                # seconds for the station to answer
DIAL_TRIES = 2                  # second try drops the path first: the first one can be stale
RETRY_WAITS = (15, 30, 60, 120)  # pauses before redialing after a drop while playing
NO_AUDIO_S = 20                 # playing but no audio this long = lost (RNS notices a dead link much later)
STEADY_S = 60                   # this much good listening resets the retry count


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


def _com_init():
    # LXST's Windows backend only sets up COM in the thread that imports it first
    if sys.platform == "win32":
        from LXST.Platforms.windows import soundcard
        soundcard._com.init_com()


# LXST's Windows soundcard crashes if two threads enumerate or open devices at once
AUDIO_LOCK = threading.RLock()


class locked_audio:
    """Create, enter and exit an LXST player/recorder under AUDIO_LOCK; play/record stay unlocked."""

    def __init__(self, make):
        self.make = make

    def __enter__(self):
        with AUDIO_LOCK:
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


class Player:
    """Jitter buffer + sound card output. Starts after `prefill` seconds of audio, rebuffers on underrun."""

    def __init__(self, prefill=2.0, device=None, max_extra=4.0):
        self.prefill = prefill
        self.max_extra = max_extra
        self.device = device
        self.volume_db = 0.0
        self.frames = deque()
        self.buffered = 0
        self.lock = threading.Lock()
        self.state = "idle"
        self.underruns = 0
        self.levels = (0.0, 0.0)
        self.running = False
        self._run_id = 0
        self._wake = threading.Event()
        self._thread = None

    @property
    def buffer_s(self):
        return self.buffered / SAMPLE_RATE

    def push(self, frame):
        with self.lock:
            self.frames.append(frame)
            self.buffered += len(frame)
            limit = (self.prefill + self.max_extra) * SAMPLE_RATE
            while self.buffered > limit and len(self.frames) > 1:
                self.buffered -= len(self.frames.popleft())

    def start(self):
        if self.running:
            return
        self.state = "buffering"
        self._run_id += 1
        self.running = True
        if self._thread is None:
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
                self._run(self._run_id)

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

    def _run(self, run_id):
        try:
            from LXST.Sinks import Backend
            with AUDIO_LOCK:
                backend = Backend(preferred_device=self.device)
        except Exception as e:
            RNS.log(f"No audio output: {e}", RNS.LOG_ERROR)
            self.state = "no audio output"
            return
        target = self.prefill
        with locked_audio(lambda: backend.get_player(samples_per_frame=SAMPLE_RATE // 50)) as player:
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
                    continue
                if self.volume_db:
                    f = np.clip(f * 10 ** (self.volume_db / 20), -1.0, 1.0)
                peak = np.abs(f).max(axis=0)
                self.levels = (float(peak[0]), float(peak[-1]))
                player.play(f.astype(np.float32))


class Station:
    def __init__(self, call_id, name="", listeners=None, now_playing="", kbps=None, on_air=True):
        self.call_id, self.name, self.listeners = call_id, name, listeners
        self.now_playing, self.kbps, self.on_air = now_playing, kbps, on_air
        self.heard = time.time()

    @property
    def hops(self):
        dh = RNS.Destination.hash_from_name_and_identity("lxst.telephony", bytes.fromhex(self.call_id))
        return RNS.Transport.hops_to(dh) if RNS.Transport.has_path(dh) else None

    def to_dict(self):
        return {"call_id": self.call_id, "name": self.name, "listeners": self.listeners,
                "now_playing": self.now_playing, "kbps": self.kbps, "on_air": self.on_air, "heard": self.heard}


class Directory:
    """Stations heard through radiolxst.station announces, kept on disk between runs."""

    aspect_filter = STATION_ASPECT

    def __init__(self, path):
        self.path = path
        self.stations = {}
        self.on_change = None
        try:
            with open(path, encoding="utf-8") as f:
                for d in json.load(f):
                    s = Station(d["call_id"], d.get("name", ""), d.get("listeners"), d.get("now_playing", ""),
                                d.get("kbps"), d.get("on_air", True))
                    s.heard = d.get("heard", 0)
                    self.stations[s.call_id] = s
        except Exception:
            pass
        RNS.Transport.register_announce_handler(self)

    def received_announce(self, destination_hash, announced_identity, app_data):
        try:
            cid = announced_identity.hash.hex()
            if not app_data:
                # path response without data: only refresh, never wipe what we know
                if cid in self.stations:
                    self.stations[cid].heard = time.time()
                return
            info = mp.unpackb(app_data)
            if not isinstance(info, dict):
                return
            s = Station(cid, str(info.get("n", ""))[:48], info.get("l"), str(info.get("np", ""))[:64],
                        info.get("kb"), bool(info.get("air", True)))
            self.stations[cid] = s
            RNS.log(f"Heard station {s.name or cid} ({'on air' if s.on_air else 'off air'})", RNS.LOG_NOTICE)
            self.save()
            if callable(self.on_change):
                self.on_change()
        except Exception as e:
            RNS.log(f"Bad station announce: {e}", RNS.LOG_DEBUG)

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump([s.to_dict() for s in list(self.stations.values())], f, indent=1)
        except OSError:
            pass

    def name_of(self, call_id):
        s = self.stations.get(call_id)
        return s.name if s else ""


class Client:
    def __init__(self, identity, prefill=3.0, device=None):
        self.identity = identity
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
        self.last_audio = 0.0
        self._retry_lock = threading.Lock()
        self._lock = threading.RLock()  # tune/hangup vs packets of the old link

    def tune(self, call_id, profile=None, name="", why=""):
        self.hangup()
        with self._lock:
            self._gen += 1
            gen = self._gen
            self.call_id = call_id.strip().lower()
            self.station_name = name
            self.meta = {}
        RNS.log(f"Tuning to {self.call_id}" + (f" ({why})" if why else ""), RNS.LOG_NOTICE)
        if profile is not None:
            self.profile = profile
        self.retries = 0
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
        for i in range(DIAL_TRIES):
            why = self._attempt(gen, dh, fresh=i > 0)
            if why in (None, "ok", "rejected"):
                return
        if self.retries:
            self._schedule_retry(gen, why)
        else:
            self._fail(why)

    def _attempt(self, gen, dh, fresh):
        """One try: path (a fresh one if asked) + call. Returns ok, rejected, a failure reason, or None if cancelled."""
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
            time.sleep(0.2)
        if gen != self._gen:
            return None
        if self.state == "playing":
            return "ok"
        if self.state == "failed":
            return "rejected"
        self.link = None
        try:
            link.teardown()
        except Exception:
            pass
        return "no answer"

    def _signal(self, link, signals):
        RNS.Packet(link, mp.packb({FIELD_SIGNALLING: signals}), create_receipt=False).send()

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
                threading.Thread(target=self._watch, args=(gen, link), daemon=True).start()
                RNS.log(f"Tuned in to {self.station_name or self.call_id}", RNS.LOG_NOTICE)
            elif s in (STATUS_BUSY, STATUS_REJECTED):
                self._fail("station busy or off air")
        meta = msg.get(FIELD_META)
        if isinstance(meta, dict):
            self.meta = meta
            if meta.get("n"):
                self.station_name = str(meta["n"])[:48]
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
        self.state, self.message = "ended", "the station hung up"

    def _watch(self, gen, link):
        while gen == self._gen and self.link is link and self.state == "playing":
            time.sleep(1)
            now = time.time()
            if self.retries and now - self.started > STEADY_S:
                self.retries = 0
            if now - self.last_audio > NO_AUDIO_S and gen == self._gen and self.link is link:
                RNS.log(f"No audio for {NO_AUDIO_S} s", RNS.LOG_NOTICE)
                self._schedule_retry(gen, "no audio from the station", link)
                return

    def _schedule_retry(self, gen, why, dropped=None):
        """After a drop while playing: try again with growing pauses, then give up."""
        with self._retry_lock:
            if gen != self._gen or (dropped is not None and dropped is not self.link):
                return  # cancelled, or this drop is already being handled
            if self.retries >= len(RETRY_WAITS):
                self._fail(f"{why}, gave up after {len(RETRY_WAITS)} retries")
                return
            wait = RETRY_WAITS[self.retries]
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
        RNS.log(f"{why}: retry {self.retries}/{len(RETRY_WAITS)} in {wait} s", RNS.LOG_NOTICE)
        threading.Thread(target=self._retry, args=(gen, wait), daemon=True).start()

    def _retry(self, gen, wait):
        end = time.time() + wait
        while time.time() < end:
            if gen != self._gen:
                return
            self.message = f"retry {self.retries}/{len(RETRY_WAITS)} in {end - time.time():.0f} s"
            time.sleep(0.5)
        if gen != self._gen:
            return
        self.state, self.message = "searching", f"retry {self.retries}/{len(RETRY_WAITS)}"
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

    def snapshot(self):
        el = time.time() - self.started if self.started and self.state == "playing" else 0
        p = self.player
        with self._lock:
            cid, name, meta = self.call_id or "", self.station_name, self.meta
        return {
            "state": self.state, "message": self.message, "call_id": cid,
            "name": name, "listeners": meta.get("l"), "track": str(meta.get("np") or ""),
            "time": el, "kbps": self.bytes * 8 / el / 1000 if el > 1 else 0,
            "buffer_s": p.buffer_s, "player": p.state, "underruns": p.underruns, "levels": p.levels,
            "hops": (RNS.Transport.hops_to(self.link.destination.hash)
                     if self.link and RNS.Transport.has_path(self.link.destination.hash) else None),
        }


def load_identity(path):
    if os.path.isfile(path):
        return RNS.Identity.from_file(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
    ap.add_argument("--rnsconfig", default=None)
    a = ap.parse_args()

    RNS.Reticulum(configdir=a.rnsconfig, loglevel=RNS.LOG_NOTICE)
    directory = Directory(os.path.join(here, "storage", "stations.json"))
    if a.list or not a.call_id:
        if a.list:
            print(f"Listening for stations for {a.list} s...")
            time.sleep(a.list)
        for s in sorted(directory.stations.values(), key=lambda s: -s.heard):
            print(f"  {s.call_id}  {'ON ' if s.on_air else 'off'}  {s.name}  listeners {s.listeners}  "
                  f"{s.now_playing}")
        if not directory.stations:
            print("  no stations heard yet")
        return

    cl = Client(load_identity(a.identity), prefill=a.buffer, device=a.device)
    cl.player.volume_db = a.volume
    cl.tune(a.call_id, PROFILES[a.quality], directory.name_of(a.call_id.lower()))
    try:
        while True:
            time.sleep(2)
            s = cl.snapshot()
            print(f"\r{s['state']:<10} {s['message'][:24]:<24} buffer {s['buffer_s']:4.1f} s  "
                  f"{s['kbps']:5.1f} kbit/s  dropouts {s['underruns']}   ", end="", flush=True)
            if s["state"] in ("failed", "ended"):
                print()
                break
    except KeyboardInterrupt:
        print()
    cl.hangup()


if __name__ == "__main__":
    main()
