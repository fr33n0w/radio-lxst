# LXMF side of the radio-lxst client: sends the station commands (help, status, subscribe...) and shows the replies.

import os
import sys
import json
import time
import threading
import subprocess

import RNS
import LXMF

COMMANDS = ("help", "info", "status", "link", "subscribe", "unsubscribe")
KEEP = 60               # lines kept per station
FIRST_SEND_DELAY = 3    # after our first announce: the station needs our key to trust the message
REPLY_WAIT = 90


def subscription(rows):
    """Replies come in order: each one answers the oldest command still waiting. Not by text: replies can be custom."""
    waiting, state = [], None
    for r in rows:
        if r.get("dir") == "out":
            if not str(r.get("state", "")).startswith("failed"):
                waiting.append((r.get("text"), r.get("t") or 0))
        else:
            # a command never answered must not take a later reply
            waiting = [w for w in waiting if (r.get("t") or 0) - w[1] < REPLY_WAIT]
            if not waiting:
                continue
            cmd = waiting.pop(0)[0]
            if cmd in ("subscribe", "unsubscribe"):
                state = cmd == "subscribe"
    return state


def waiting(rows, now=None):
    """Commands sent and not answered yet (the last REPLY_WAIT s)."""
    now = now or time.time()
    left = []
    for r in rows:
        if r.get("dir") == "out":
            if not str(r.get("state", "")).startswith("failed"):
                left.append(r.get("t") or 0)
        elif left:
            left.pop(0)
    return [t for t in left if now - t < REPLY_WAIT]


def _stamp_job(identity):
    T = RNS.Transport
    dh = RNS.Destination.hash(identity, "lxmf", "delivery")
    name_hash = RNS.Identity.full_hash("lxmf.delivery".encode("utf-8"))[:RNS.Identity.NAME_HASH_LENGTH // 8]
    return dh, dh + identity.get_public_key() + name_hash, min(T.STAMP_COST_CAP, float(T.DESTINATION_VALUE))


def load_ready(identity):
    """True when no announce stamp is needed or ours is on disk already."""
    if not getattr(RNS.Transport, "STAMP_DESTINATIONS", False):
        return True
    from RNS.Stamps import load_destination_stamp
    dh, material, value = _stamp_job(identity)
    return load_destination_stamp(dh, material, value) is not None


def ensure_stamp(identity):
    """Announce stamp of our LXMF address (RNS 1.6.1+), made in a child process the first time.
    RNS would make it inside this process on the first path request for us: one core busy holding
    the GIL (the window freezes) and the call's path requests queued behind it for minutes."""
    if load_ready(identity):
        return True
    from RNS.Stamps import store_destination_stamp
    dh, material, value = _stamp_job(identity)
    RNS.log("Making the announce stamp of the client LXMF address (first start only, a few minutes)",
            RNS.LOG_NOTICE)
    t = time.time()
    flags = 0x00004000 | 0x08000000 if sys.platform == "win32" else 0  # below normal priority, no console
    try:
        out = subprocess.run([sys.executable, os.path.abspath(__file__), "--stamp", material.hex(), str(value)],
                             capture_output=True, text=True, creationflags=flags, timeout=3600).stdout.split()
        stamp, got = bytes.fromhex(out[-2]), float(out[-1])
    except Exception as e:
        RNS.log(f"Announce stamp not made: {e}", RNS.LOG_ERROR)
        return False
    store_destination_stamp(dh, stamp, got)
    RNS.log(f"Announce stamp ready in {time.time() - t:.0f} s", RNS.LOG_NOTICE)
    return True


class Commands:
    """Our LXMF address (the client identity) talking to the stations' autoresponders."""

    def __init__(self, identity, storage, name="", directory=None):
        self.identity = identity
        self.directory = directory
        self.path = os.path.join(storage, "commands.json")
        self.lock = threading.Lock()
        self.lines = self._load()
        self.revision = 0
        self.on_change = None
        self.on_notice = None  # (call_id, text): a message that answers no command, like the on-air one
        self.announced = 0
        self.router = LXMF.LXMRouter(identity=identity, storagepath=os.path.join(storage, "lxmf_client"))
        self.dest = self.router.register_delivery_identity(identity, display_name=name or None)
        self.router.register_delivery_callback(self._received)

    @property
    def address(self):
        return self.dest.hash.hex()

    def set_name(self, name):
        self.dest.display_name = name or None

    def announce(self):
        self.router.announce(self.dest.hash)
        self.announced = time.time()
        RNS.log(f"LXMF address {self.address} announced", RNS.LOG_NOTICE)

    # transcript: {call_id: [{"t", "dir", "text", "state"}]}
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return {k: v[-KEEP:] for k, v in d.items() if isinstance(v, list) and len(k) == 32}
        except Exception:
            return {}

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.lines, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _add(self, call_id, direction, text, state=""):
        line = {"t": time.time(), "dir": direction, "text": text, "state": state}
        with self.lock:
            rows = self.lines.setdefault(call_id, [])
            rows.append(line)
            del rows[:-KEEP]
            self._save()
            self.revision += 1
        self._changed()
        return line

    def _set_state(self, call_id, line, state):
        with self.lock:
            line["state"] = state
            self._save()
            self.revision += 1
        self._changed()

    def _changed(self):
        if callable(self.on_change):
            try:
                self.on_change()
            except Exception:
                pass

    def transcript(self, call_id):
        with self.lock:
            return [dict(x) for x in self.lines.get(call_id, [])]

    def subscribed(self, call_id):
        """True/False from the last subscribe/unsubscribe the station answered, None if never asked."""
        return subscription(self.transcript(call_id))

    def clear(self, call_id):
        with self.lock:
            self.lines.pop(call_id, None)
            self._save()
            self.revision += 1
        self._changed()

    # out
    def send(self, call_id, cmd):
        cmd = cmd.strip().lower()
        if cmd not in COMMANDS:
            raise ValueError(f"unknown command {cmd!r}")
        call_id = call_id.strip().lower()
        line = self._add(call_id, "out", cmd, "sending")
        threading.Thread(target=self._send, args=(call_id, cmd, line), daemon=True).start()
        return line

    def _send(self, call_id, cmd, line):
        if not self.announced:
            self.announce()
            time.sleep(FIRST_SEND_DELAY)
        try:
            ident = RNS.Identity.recall(bytes.fromhex(call_id), from_identity_hash=True)
        except Exception:
            ident = None
        if ident is None:
            # not heard yet: its LXMF address answers a path request with its key
            lx = RNS.Destination.hash_from_name_and_identity("lxmf.delivery", bytes.fromhex(call_id))
            RNS.Transport.request_path(lx)
            t = time.time()
            while ident is None and time.time() - t < 20:
                time.sleep(1)
                ident = RNS.Identity.recall(lx)
        if ident is None:
            self._set_state(call_id, line, "failed: station unknown, no path")
            return
        out = RNS.Destination(ident, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
        m = LXMF.LXMessage(out, self.dest, cmd, desired_method=LXMF.LXMessage.OPPORTUNISTIC)
        m.register_delivery_callback(lambda msg: self._set_state(call_id, line, "delivered"))
        m.register_failed_callback(lambda msg: self._set_state(call_id, line, "failed"))
        self.router.handle_outbound(m)
        self._set_state(call_id, line, "sent")
        RNS.log(f"Command {cmd!r} sent to {call_id}", RNS.LOG_NOTICE)

    # in
    def _received(self, m):
        try:
            ident = RNS.Identity.recall(m.source_hash)
            call_id = ident.hash.hex() if ident else ""
            known = call_id and (call_id in self.lines or
                                 (self.directory is not None and call_id in self.directory.stations))
            if not known:
                RNS.log(f"LXMF from {m.source_hash.hex()} ignored: not a station we know", RNS.LOG_INFO)
                return
            if not m.signature_validated:
                RNS.log(f"LXMF from {call_id} not verified, ignored", RNS.LOG_NOTICE)
                return
            text = (m.content_as_string() or "").strip()
            notice = not waiting(self.transcript(call_id))  # not the answer to a command: the on-air message
            self._add(call_id, "in", text[:2000])
            RNS.log(f"{'Message' if notice else 'Reply'} from {call_id}: {text[:60]!r}", RNS.LOG_NOTICE)
            if notice and callable(self.on_notice):
                self.on_notice(call_id, text)
        except Exception as e:
            RNS.log(f"Bad LXMF message: {e}", RNS.LOG_DEBUG)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--stamp":
    # child of ensure_stamp(): the stamp work, away from the GUI process
    from RNS.Stamps import generate_stamp
    s, v = generate_stamp(bytes.fromhex(sys.argv[2]), stamp_cost=float(sys.argv[3]), expand_rounds=0,
                          size=RNS.Identity.STAMPLENGTH)
    print(s.hex(), v)
