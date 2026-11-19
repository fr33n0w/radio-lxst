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

__version__ = "0.4"

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
    """Input device labels (mics, line in, virtual cables, loopback of outputs)."""
    from radio_client import com_call
    return com_call(lambda: [_device_label(d) for d in _soundcard().all_microphones(include_loopback=True)]) or []


def default_output_name():
    from radio_client import com_call
    return com_call(lambda: _soundcard().default_speaker().name) or ""


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
        # station directory entry, heard by radio_client
        self.station = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, *STATION_ASPECT)
        # path responses re-announce with the default app data: keep name and info in them too
        self.station.set_default_app_data(self.station_info)
        # live state (on air, listeners, track) on request: GET "status" over a Link
        self.status_hits = []
        self.station.register_request_handler("status", response_generator=self._status_request,
                                              allow=RNS.Destination.ALLOW_ALL)
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
        self.source = source
        self.on_air = True
        self.on_air_since = time.time()
        RNS.log(f"{self.name} on air", RNS.LOG_NOTICE)
        if self.announce_on_start:
            self.announce()
        if self.lxmf_on:
            self.lx.notify_on_air()

    def switch_source(self, source):
        """Change the audio source while on air, listeners stay connected."""
        old = self.source
        self.source = source
        if old:
            old.stop()
        RNS.log(f"Source switched to {source_label(source)}", RNS.LOG_NOTICE)

    def stop(self):
        if self.pending_source:
            self.pending_source.stop()
            self.pending_source = None
        was_on = self.on_air
        self.on_air = False
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

    def now_playing_text(self):
        src = self.source
        return (src.now_playing or "-" if src.folder else "Live Stream") if src else "-"

    def meta(self):
        with self.lock:
            n = sum(1 for l in self.listeners.values() if l.state == "listening")
        src = self.source
        np_ = (src.now_playing if src.folder else "Live streaming") if src else ""
        return {"v": __version__, "n": self.name[:48], "l": n, "s": self.slots, "np": np_[:64],
                "kb": 28 if FRAME_MS >= 120 else 56, "air": self.on_air}

    def is_full(self, but=None):
        """All listener slots taken (ringing callers count too, so two at once cannot both get in)."""
        if not self.slots:
            return False
        with self.lock:
            n = sum(1 for l in self.listeners.values() if l is not but and l.state in ("listening", "ringing"))
        return n >= self.slots

    def station_info(self):
        # the announce says who we are, never what changes: the state is asked with a status request
        return mp.packb({"v": __version__, "n": self.name[:48], "kb": 28 if FRAME_MS >= 120 else 56})

    def _status_request(self, path, data, request_id, link_id, remote_identity, requested_at):
        now = time.time()
        with self.lock:
            hits = [t for t in self.status_hits if now - t < 60]
            if len(hits) >= STATUS_MAX_PER_MIN:
                self.status_hits = hits
                return None  # too many: no answer, the asker sees "no reply"
            hits.append(now)
            self.status_hits = hits
        return self.meta()

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
            with self.lock:
                unnamed = [l for l in self.listeners.values()
                           if l.identity and not l.user and time.time() - l.name_tried >= NAME_RETRY]
            for l in unnamed:
                l.lookup_user()
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
            self._send_meta([link], self.meta())
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
        self._send_meta([lst.link], self.meta())
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

    def _link_closed(self, link):
        with self.lock:
            lst = self.listeners.pop(link.link_id, None)
        if lst and lst.state == "listening":
            RNS.log(f"Listener {lst.label} left after {RNS.prettytime(time.time() - lst.since)}", RNS.LOG_NOTICE)

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
            mon = self.monitor
            if mon is not None:
                mon.push(frame)
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
                "id": l.name, "user": l.user, "state": l.state, "sub": self.is_subscribed(l.identity), "profile": self.profile_label(l) if on else "",
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
                 f"(total calls {s['calls']}, buffer {s['buffer_ms']} ms, {FRAME_MS} ms frames, "
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
    ap.add_argument("--announce-every", type=int, default=60, help=f"minutes between announces (min {ANNOUNCE_MIN_EVERY}), 0 = only at start")
    ap.add_argument("--no-lxmf", action="store_true",
                    help="no LXMF address: no autoresponder, no subscriber messages, one announce less")
    ap.add_argument("--lxmf-every", type=int, default=3, metavar="HOURS",
                    help=f"hours between LXMF address announces (min {LXMF_MIN_EVERY_H})")
    ap.add_argument("--slots", type=int, default=0, help="max listeners at once (limits the upload), 0 = no limit")
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
    radio = Radio(load_identity(a.identity), a.name, storage=os.path.dirname(os.path.abspath(a.identity)),
                  lxmf_on=not a.no_lxmf)
    radio.slots = max(0, a.slots)
    radio.announce_every_min = a.announce_every
    radio.lxmf_every_h = a.lxmf_every
    radio.gain_db = max(-24.0, min(12.0, a.gain))
    radio.limiter_on = not a.no_limiter
    radio.limiter.ceiling_db = min(0.0, a.ceiling)
    radio.start(LineSource(a.input) if a.input else Source(url=a.url, folder=a.folder, shuffle=a.shuffle))

    print(f"\n  {a.name}  " + ("on air" if radio.on_air else "preparing stamps, on air when ready"))
    print(f"  Call this identity from Sideband / rnphone:  {radio.identity_hash}")
    print(f"  LXST destination: {radio.dest.hash.hex()}   LXMF: "
          + (radio.lxmf_hash if radio.lxmf_on else "off"))
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
