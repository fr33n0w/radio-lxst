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

__version__ = "0.2"

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


class _Sink:
    channels = CHANNELS
    samplerate = SAMPLE_RATE


def _com_init():
    # LXST's Windows backend only sets up COM in the thread that imports it first
    if sys.platform == "win32":
        from LXST.Platforms.windows import soundcard
        soundcard._com.init_com()


def list_devices():
    """Output device names, read in a worker thread so the GUI thread never touches COM."""
    out = []

    def job():
        try:
            _com_init()
            from LXST.Sinks import Backend
            out.extend(s.name for s in Backend().all_speakers())
        except Exception as e:
            RNS.log(f"Could not list audio devices: {e}", RNS.LOG_WARNING)

    t = threading.Thread(target=job, daemon=True)
    t.start()
    t.join(5)
    return out


class Player:
    """Jitter buffer + sound card output. Starts after `prefill` seconds of audio, rebuffers on underrun."""

    def __init__(self, prefill=2.0, device=None):
        self.prefill = prefill
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

    @property
    def buffer_s(self):
        return self.buffered / SAMPLE_RATE

    def push(self, frame):
        with self.lock:
            self.frames.append(frame)
            self.buffered += len(frame)
            # cap latency at prefill + 4 s
            limit = (self.prefill + 4) * SAMPLE_RATE
            while self.buffered > limit and len(self.frames) > 1:
                self.buffered -= len(self.frames.popleft())

    def start(self):
        if self.running:
            return
        self.running = True
        self.state = "buffering"
        self._run_id += 1
        threading.Thread(target=self._run, args=(self._run_id,), daemon=True).start()

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
            _com_init()
            from LXST.Sinks import Backend
            backend = Backend(preferred_device=self.device)
        except Exception as e:
            RNS.log(f"No audio output: {e}", RNS.LOG_ERROR)
            self.state = "no audio output"
            return
        target = self.prefill
        with backend.get_player(samples_per_frame=SAMPLE_RATE // 50) as player:
            # an older thread still inside play() exits here instead of sharing the queue
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
    def __init__(self, identity, prefill=2.0, device=None):
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

    def tune(self, call_id, profile=None, name=""):
        self.hangup()
        self._gen += 1
        gen = self._gen
        self.call_id = call_id.strip().lower()
        self.station_name = name
        self.meta = {}
        if profile is not None:
            self.profile = profile
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
        if not RNS.Transport.has_path(dh):
            RNS.Transport.request_path(dh)
            t = time.time()
            while not RNS.Transport.has_path(dh) and time.time() - t < 20:
                if gen != self._gen:
                    return
                time.sleep(0.2)
        remote = RNS.Identity.recall(dh)
        if gen != self._gen:
            return
        if not remote:
            self._fail("no path to the station")
            return
        self.state, self.message = "connecting", f"{RNS.Transport.hops_to(dh)} hops"
        dest = RNS.Destination(remote, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxst", "telephony")
        self.decoder = Opus()
        self.decoder.sink = _Sink()
        self.bytes = self.frames = 0
        link = RNS.Link(dest, closed_callback=lambda l: self._closed(l, gen))
        link.set_packet_callback(lambda data, packet: self._packet(link, data, gen))
        self.link = link
        t = time.time()
        while gen == self._gen and self.state in ("connecting", "ringing") and time.time() - t < 30:
            time.sleep(0.2)
        if gen == self._gen and self.state in ("connecting", "ringing"):
            self._fail("no answer")

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
        for s in msg.get(FIELD_SIGNALLING, []) if isinstance(msg.get(FIELD_SIGNALLING), list) else []:
            if s == STATUS_AVAILABLE:
                link.identify(self.identity)
            elif s == STATUS_RINGING:
                self.state, self.message = "ringing", ""
                self._signal(link, [PREFERRED_PROFILE + self.profile, PREFERRED_MODE + MODE_HALF_DUPLEX])
            elif s == STATUS_ESTABLISHED:
                self.state, self.message = "playing", ""
                self.started = time.time()
                self.player.start()
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
                    self.frames += 1
                    self.bytes += len(data)
                except Exception as e:
                    RNS.log(f"Decode error: {e}", RNS.LOG_DEBUG)

    def _closed(self, link, gen):
        if gen != self._gen:
            return
        if self.state in ("playing", "connecting", "ringing"):
            RNS.log("Station closed the link", RNS.LOG_NOTICE)
            self.state, self.message = "ended", "the station hung up"
        self.player.stop()

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
        return {
            "state": self.state, "message": self.message, "call_id": self.call_id or "",
            "name": self.station_name, "listeners": self.meta.get("l"), "track": str(self.meta.get("np") or ""),
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
    ap.add_argument("--buffer", type=float, default=2.0, help="seconds of audio buffered before playing")
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
