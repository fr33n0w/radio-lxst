#!/usr/bin/env python3
# radio-lxst: internet radio over Reticulum, listeners "call" it with any LXST phone (Sideband, rnphone).

import os
import sys
import time
import types
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

from LXST.Codecs import Opus
from LXST.Codecs.libs.pyogg import opus, OpusEncoder

__version__ = "0.2"

APP_NAME = "lxst"
ASPECT = "telephony"
STATION_ASPECT = ("radiolxst", "station")
FIELD_META = "radiolxst"
META_REFRESH = 30
FIELD_SIGNALLING = 0x00
FIELD_FRAMES = 0x01
CODEC_OPUS = b"\x01"

STATUS_AVAILABLE = 0x03
STATUS_RINGING = 0x04
STATUS_CONNECTING = 0x05
STATUS_ESTABLISHED = 0x06
PREFERRED_PROFILE = 0xFF

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_MS = FRAME_SAMPLES = FRAME_BYTES = MAX_QUEUE_FRAMES = 0


def set_frame_ms(ms):
    # Sideband buffers ~6 frames: longer frames = more jitter tolerance, less bitrate per packet
    global FRAME_MS, FRAME_SAMPLES, FRAME_BYTES, MAX_QUEUE_FRAMES
    FRAME_MS = ms
    FRAME_SAMPLES = SAMPLE_RATE * ms // 1000
    FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2
    MAX_QUEUE_FRAMES = int(3000 / ms)


set_frame_ms(120)
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


class Source:
    """ffmpeg -> 48 kHz stereo PCM frames; stream URL or folder playlist."""

    def __init__(self, url=None, folder=None, shuffle=False):
        self.url, self.folder, self.shuffle = url, folder, shuffle
        self.frames = deque(maxlen=MAX_QUEUE_FRAMES)
        self.now_playing = ""
        self.state = "starting"
        self.running = True
        self.proc = None
        self._skip = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _inputs(self):
        if self.url:
            while True:
                yield self.url, ["-user_agent", UA, "-reconnect", "1", "-reconnect_streamed", "1",
                                 "-reconnect_delay_max", "10"]
        import random
        while True:
            try:
                files = sorted(os.path.join(self.folder, f) for f in os.listdir(self.folder)
                               if f.lower().endswith(AUDIO_EXTS))
            except OSError:
                files = []
            if not files:
                self.state = "no audio files"
                time.sleep(10)
                continue
            if self.shuffle:
                random.shuffle(files)
            for f in files:
                yield f, ["-re"]

    def _loop(self):
        for inp, extra in self._inputs():
            if not self.running:
                return
            self.now_playing = os.path.splitext(os.path.basename(inp))[0] if self.folder else "live stream"
            self.state = "connecting" if self.url else "playing"
            cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *extra, "-i", inp,
                   "-f", "s16le", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "pipe:1"]
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                while self.running and not self._skip:
                    chunk = self.proc.stdout.read(FRAME_BYTES)
                    if len(chunk) < FRAME_BYTES:
                        break
                    self.state = "playing"
                    self.frames.append(chunk)
                self._skip = False
                self.proc.kill()
            except FileNotFoundError:
                self.state = "ffmpeg not found"
                RNS.log("ffmpeg not found in PATH", RNS.LOG_ERROR)
                time.sleep(10)
            except Exception as e:
                RNS.log(f"Source error: {e}", RNS.LOG_ERROR)
            if self.url and self.running:
                self.state = "reconnecting"
                RNS.log("Stream ended, reconnecting in 3 s", RNS.LOG_WARNING)
                time.sleep(3)

    def skip(self):
        self._skip = True

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass


LIVE_MAX_FRAMES = 4  # live input: drop old frames past this, keeps latency low


def _soundcard():
    if sys.platform == "win32":
        from LXST.Platforms.windows import soundcard
        soundcard._com.init_com()  # COM is per thread on Windows
    elif sys.platform == "darwin":
        from LXST.Platforms.darwin import soundcard
    else:
        from LXST.Platforms.linux import soundcard
    return soundcard


def _device_label(dev):
    return ("Loopback: " if getattr(dev, "isloopback", False) else "") + dev.name


def list_inputs():
    """Input device labels (mics, line in, virtual cables, loopback of outputs), read in a worker thread."""
    out = []

    def job():
        try:
            out.extend(_device_label(d) for d in _soundcard().all_microphones(include_loopback=True))
        except Exception as e:
            RNS.log(f"Could not list input devices: {e}", RNS.LOG_WARNING)

    t = threading.Thread(target=job, daemon=True)
    t.start()
    t.join(5)
    return out


class LineSource:
    """Live sound card input (mic, line in, VB-Cable, loopback) -> 48 kHz stereo PCM frames."""

    url = folder = None

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
            dev = None
            try:
                dev = self._open()
            except Exception as e:
                RNS.log(f"Input error: {e}", RNS.LOG_ERROR)
            if dev is None:
                self.state = "input device not found"
                RNS.log(f"Input device not found: {self.device}", RNS.LOG_ERROR)
                time.sleep(5)
                continue
            try:
                ch = max(1, int(dev.channels))
                with dev.recorder(samplerate=SAMPLE_RATE, channels=min(ch, CHANNELS),
                                  blocksize=FRAME_SAMPLES // 4) as rec:
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
        self.caller_profile = None
        self.profile = DEFAULT_PROFILE
        self.state = "connecting"
        self.since = time.time()
        self.sent_bytes = 0
        self.failed = 0

    @property
    def name(self):
        return self.identity.hash.hex() if self.identity else "?"


class Radio:
    def __init__(self, identity, name="radio-lxst"):
        self.identity, self.name = identity, name
        self.source = None
        self.on_air = False
        self.announce_on_start = True
        self.announce_every_min = 15
        self.last_announce = 0
        self.listeners = {}
        self.lock = threading.Lock()
        self.encoders = {}
        self.total_calls = 0
        self.peak_listeners = 0
        self.total_bytes = 0
        self.max_late = 0.0
        self.levels = (0.0, 0.0)
        self.on_air_since = None
        self.gain_db = 0.0
        self.limiter_on = True
        self.limiter = Limiter()

        self.dest = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, ASPECT)
        self.dest.set_link_established_callback(self._link_established)
        # LXMF address with the station name, so it shows up in Sideband's announce list
        self.lxmf = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, "lxmf", "delivery")
        # station directory entry, heard by radio_client
        self.station = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, *STATION_ASPECT)
        # path responses re-announce with the default app data: keep name and info in them too
        self.station.set_default_app_data(self.station_info)
        self.lxmf.set_default_app_data(lambda: mp.packb([self.name.encode("utf-8"), None]))
        threading.Thread(target=self._announce_loop, daemon=True).start()
        threading.Thread(target=self._meta_loop, daemon=True).start()
        threading.Thread(target=self._send_loop, daemon=True).start()

    @property
    def identity_hash(self):
        return self.identity.hash.hex()

    def start(self, source):
        if self.on_air:
            self.stop()
        self.encoders = {}
        self.source = source
        self.on_air = True
        self.on_air_since = time.time()
        RNS.log(f"{self.name} on air", RNS.LOG_NOTICE)
        if self.announce_on_start:
            self.announce()

    def switch_source(self, source):
        """Change the audio source while on air, listeners stay connected."""
        old = self.source
        self.source = source
        if old:
            old.stop()
        RNS.log(f"Source switched to {source_label(source)}", RNS.LOG_NOTICE)

    def stop(self):
        was_on = self.on_air
        self.on_air = False
        self.on_air_since = None
        if was_on:
            # tell the directory we are off air
            try:
                self.station.announce(app_data=self.station_info())
            except Exception:
                pass
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
        self.dest.announce()
        self.lxmf.announce(app_data=mp.packb([self.name.encode("utf-8"), None]))
        self.station.announce(app_data=self.station_info())
        self.last_announce = time.time()
        RNS.log(f"Announced {self.name}", RNS.LOG_NOTICE)

    def meta(self):
        with self.lock:
            n = sum(1 for l in self.listeners.values() if l.state == "listening")
        src = self.source
        np_ = src.now_playing if src and src.folder else ""
        return {"v": __version__, "n": self.name[:48], "l": n, "np": np_[:64],
                "kb": 28 if FRAME_MS >= 120 else 56, "air": self.on_air}

    def station_info(self):
        return mp.packb(self.meta())

    # in-call info: LXST only reads keys 0x00/0x01, other phones ignore this one
    def _send_meta(self, links, meta):
        data = mp.packb({FIELD_META: meta})
        for link in links:
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
            m = self.meta()
            if m != last or time.time() - last_t >= META_REFRESH:
                with self.lock:
                    links = [l.link for l in self.listeners.values() if l.state == "listening"]
                if links:
                    self._send_meta(links, m)
                last, last_t = m, time.time()

    def _announce_loop(self):
        while True:
            time.sleep(5)
            every = self.announce_every_min
            if self.on_air and every and time.time() - self.last_announce >= every * 60:
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
        self._send_meta([lst.link], self.meta())
        RNS.log(f"Listener {lst.name} tuned in ({self.profile_label(lst)})", RNS.LOG_NOTICE)

    def _packet(self, lst, data):
        # caller audio is ignored, only signalling matters
        try:
            msg = mp.unpackb(data)
        except Exception:
            return
        if not isinstance(msg, dict) or FIELD_SIGNALLING not in msg:
            return
        sigs = msg[FIELD_SIGNALLING]
        for s in (sigs if isinstance(sigs, list) else [sigs]):
            if isinstance(s, int) and s >= PREFERRED_PROFILE:
                lst.caller_profile = s - PREFERRED_PROFILE
                if lst.state == "listening":
                    lst.profile = PROFILE_MAP.get(lst.caller_profile, DEFAULT_PROFILE)
                    RNS.log(f"Listener {lst.name} switched to {self.profile_label(lst)}", RNS.LOG_NOTICE)

    def _link_closed(self, link):
        with self.lock:
            lst = self.listeners.pop(link.link_id, None)
        if lst and lst.state == "listening":
            RNS.log(f"Listener {lst.name} left after {RNS.prettytime(time.time() - lst.since)}", RNS.LOG_NOTICE)

    @staticmethod
    def profile_label(lst):
        cp = CALLER_PROFILE_NAMES.get(lst.caller_profile, "default")
        return f"{cp} -> Opus {Opus.profile_name(lst.profile)}"

    def _encoder(self, profile):
        enc = self.encoders.get(profile)
        if enc is None:
            enc = RadioOpus(profile=profile)
            enc.source = _Src()
            self.encoders[profile] = enc
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
            try:
                pcm = src.frames.popleft()
            except IndexError:
                self.levels = (0.0, 0.0)
                continue
            frame = np.frombuffer(pcm, dtype=np.int16).reshape(FRAME_SAMPLES, CHANNELS) / 32768.0
            if self.gain_db:
                frame = frame * 10 ** (self.gain_db / 20)
            if self.limiter_on:
                frame = self.limiter.process(frame)
            else:
                self.limiter.reduction_db = 0.0
                frame = np.clip(frame, -1.0, 1.0)
            peak = np.abs(frame).max(axis=0)
            self.levels = (float(peak[0]), float(peak[1]))
            with self.lock:
                active = [l for l in self.listeners.values() if l.state == "listening"]
            if not active:
                continue
            packets = {}
            for lst in active:
                if lst.profile not in packets:
                    try:
                        enc = self._encoder(lst.profile).encode(frame)
                        packets[lst.profile] = mp.packb({FIELD_FRAMES: CODEC_OPUS + bytes(enc)})
                    except Exception as e:
                        RNS.log(f"Encode error: {e}", RNS.LOG_ERROR)
                        packets[lst.profile] = None
                data = packets[lst.profile]
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
                "id": l.name, "state": l.state, "profile": self.profile_label(l) if on else "",
                "time": now - l.since if on else 0,
                "kbps": l.sent_bytes * 8 / max(1, now - l.since) / 1000 if on else 0,
                "lost": l.failed,
            })
        src = self.source
        return {
            "on_air": self.on_air, "uptime": now - self.on_air_since if self.on_air_since else 0,
            "listeners": sum(1 for r in rows if r["state"] == "listening"), "rows": rows,
            "peak": self.peak_listeners, "calls": self.total_calls, "bytes": self.total_bytes,
            "buffer_ms": len(src.frames) * FRAME_MS if src else 0,
            "source_state": src.state if src else "off", "now_playing": src.now_playing if src else "",
            "levels": self.levels, "late_ms": self.max_late * 1000,
            "gain_db": self.gain_db, "limiter_on": self.limiter_on,
            "reduction_db": self.limiter.reduction_db if self.limiter_on and self.on_air else 0.0,
            "next_announce": (self.last_announce + self.announce_every_min * 60 - now)
            if self.on_air and self.announce_every_min else None,
        }

    def status_text(self):
        s = self.snapshot()
        lines = [f"[{time.strftime('%H:%M:%S')}] {self.name}  listeners {s['listeners']}  "
                 f"(total calls {s['calls']}, buffer {s['buffer_ms']} ms, {FRAME_MS} ms frames, "
                 f"send late max {s['late_ms']:.0f} ms)"]
        self.max_late = 0.0
        for r in s["rows"]:
            dur = RNS.prettytime(r["time"]) if r["state"] == "listening" else r["state"]
            lines.append(f"   {r['id']}  {r['profile']}  {dur}  {r['kbps']:.1f} kbit/s"
                         + (f"  lost {r['lost']}" if r["lost"] else ""))
        return "\n".join(lines)


def load_identity(path):
    if os.path.isfile(path):
        return RNS.Identity.from_file(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ident = RNS.Identity()
    ident.to_file(path)
    return ident


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Internet radio over Reticulum LXST")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="existing stream (http/https, Icecast, Shoutcast...)")
    g.add_argument("--folder", help="folder of audio files, played in a loop")
    g.add_argument("--input", metavar="DEVICE", help="live sound card input: mic, line in, VB-Cable, 'Loopback: ...' (name or part of it)")
    g.add_argument("--list-inputs", action="store_true", help="list the input devices and exit")
    ap.add_argument("--shuffle", action="store_true", help="shuffle the folder playlist")
    ap.add_argument("--name", default="radio-lxst", help="station name in the announce")
    ap.add_argument("--identity", default=os.path.join(here, "storage", "identity"))
    ap.add_argument("--rnsconfig", default=None, help="Reticulum config dir")
    ap.add_argument("--frame-ms", type=int, choices=[60, 120], default=120,
                    help="120 = steadier on slow paths (max ~28 kbit/s), 60 = up to 56 kbit/s")
    ap.add_argument("--announce-every", type=int, default=15, help="minutes between announces, 0 = only at start")
    ap.add_argument("--gain", type=float, default=0.0, help="output volume in dB (-24..+12)")
    ap.add_argument("--no-limiter", action="store_true", help="disable the peak limiter")
    ap.add_argument("--ceiling", type=float, default=-2.0, help="limiter ceiling in dBFS")
    ap.add_argument("--status", type=int, default=15, help="seconds between status lines")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    a = ap.parse_args()
    if a.list_inputs:
        print("\n".join(list_inputs()) or "no input devices found")
        return
    set_frame_ms(a.frame_ms)

    RNS.Reticulum(configdir=a.rnsconfig, loglevel=RNS.LOG_NOTICE + a.verbose)
    radio = Radio(load_identity(a.identity), a.name)
    radio.announce_every_min = a.announce_every
    radio.gain_db = max(-24.0, min(12.0, a.gain))
    radio.limiter_on = not a.no_limiter
    radio.limiter.ceiling_db = min(0.0, a.ceiling)
    radio.start(LineSource(a.input) if a.input else Source(url=a.url, folder=a.folder, shuffle=a.shuffle))

    print(f"\n  {a.name}  on air")
    print(f"  Call this identity from Sideband / rnphone:  {radio.identity_hash}")
    print(f"  LXST destination: {radio.dest.hash.hex()}   LXMF: {radio.lxmf.hash.hex()}")
    print(f"  Source: {a.url or a.folder or a.input}\n")
    try:
        while True:
            time.sleep(a.status)
            print(radio.status_text(), flush=True)
    except KeyboardInterrupt:
        pass
    radio.stop()
    time.sleep(0.5)


if __name__ == "__main__":
    main()
