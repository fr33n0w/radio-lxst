# LXMF side of a radio-lxst station: autoresponder, subscribers, on-air messages.

import os
import json
import time
import queue
import threading
from collections import deque

import RNS
import LXMF
from station_stats import lxmf_text

RATE_MAX = 20            # replies per sender in RATE_WINDOW
RATE_WINDOW = 60
NOTIFY_DELAY = 15        # after going on air: announces first, track name known
NOTIFY_MIN_GAP = 1800    # on/off air flapping does not spam subscribers
SEND_SPACING = 2         # seconds between outgoing messages
PATH_WAIT = 20
KEY_WAIT_MAX = 8         # unknown senders fetched at the same time
REPO_URL = "https://github.com/fr33n0w/radio-lxst"
STAMP_COST_MAX = 12     # costlier stamps would stall the audio thread, those replies are skipped


def lxmf_hash(identity):
    return RNS.Destination.hash(identity, "lxmf", "delivery")


def lxmf_name(dest_hash):
    """Display name from the last LXMF announce of an address, '' if unknown."""
    try:
        return LXMF.display_name_from_app_data(RNS.Identity.recall_app_data(dest_hash)) or ""
    except Exception:
        return ""


def who(dest_hash, name=None):
    name = lxmf_name(dest_hash) if name is None else name
    return dest_hash.hex() + (f" ({name})" if name else "")


# what each reply says by default; {placeholders} are filled when it is sent
DEFAULT_REPLIES = {
    "hello": 'Hello, this is "{name}" - Radio-LXST Station - '
             "Available commands: help / info / status / subscribe / unsubscribe / link",
    "help": ("Commands:\n"
             "help - this list\n"
             "info - about the station\n"
             "status - on air, listeners, now playing\n"
             "link - addresses and where to get radio-lxst\n"
             "subscribe - get a message when the station goes on air\n"
             "unsubscribe - stop those messages"),
    "info": ("{name} - Radio-LXST Station {version}\n"
             "Source: {source}\n"
             "Quality: Opus up to {kbps} kbit/s\n"
             "Subscribers: {subscribers}"),
    "status_on": "{name} is on air for {uptime}\nListeners: {listeners}\nNow playing: {now_playing}\n\n{stats}",
    "status_off": "{name} is off air.\n\n{stats}",
    "link": ("Call ID: {call_id}\n"
             "Dial it in Sideband or rnphone (Voice, profile SHQ), or in the radio-lxst client.\n"
             "LXST destination: {lxst}\n"
             "LXMF address (messages): {lxmf}\n"
             "Station announce: {station}\n"
             "Get the radio-lxst station and client: {repo}"),
    "subscribed": "Subscribed: you will get a message when {name} goes on air. Send unsubscribe to stop.",
    "already_subscribed": "You are already subscribed.",
    "unsubscribed": "Unsubscribed, no more messages.",
    "not_subscribed": "You were not subscribed.",
    "on_air": '"{name}" is on air now - Now playing: {now_playing}',
}
# what the GUI lists: key, label
REPLY_LABELS = (
    ("hello", "Any other message"), ("help", "help"), ("info", "info"),
    ("status_on", "status (on air)"), ("status_off", "status (off air)"),
    ("subscribed", "subscribe (new)"), ("already_subscribed", "subscribe (already)"),
    ("unsubscribed", "unsubscribe (done)"), ("not_subscribed", "unsubscribe (was not)"),
    ("on_air", "On-air message to subscribers"),
)
CUSTOM = {k for k, _ in REPLY_LABELS}  # link is not: its addresses and repo stay as they are
PLACEHOLDERS = ("name", "version", "source", "kbps", "subscribers", "listeners", "slots", "uptime",
                "now_playing", "call_id", "lxst", "lxmf", "station", "repo", "stats")
REPLY_MAX = 2000


class _Keep(dict):
    def __missing__(self, key):
        return "{" + key + "}"  # unknown placeholder: left as written


def render(template, values, key=""):
    try:
        return template.format_map(_Keep(values))[:REPLY_MAX]
    except (ValueError, IndexError, AttributeError):
        # a stray { or } in a custom reply: sent as written rather than not at all
        RNS.log(f"Reply '{key}' has a brace problem, sent without placeholders", RNS.LOG_WARNING)
        return template[:REPLY_MAX]


def replies_path(storage):
    return os.path.join(storage, "replies.json")


def load_replies(storage):
    """Custom replies only (missing or empty = default)."""
    try:
        with open(replies_path(storage), encoding="utf-8") as f:
            d = json.load(f)
        return {k: str(v) for k, v in d.items() if k in CUSTOM and str(v).strip()}
    except Exception:
        return {}


def save_replies(storage, replies):
    d = {k: v for k, v in replies.items() if k in CUSTOM and v.strip() and v != DEFAULT_REPLIES[k]}
    tmp = replies_path(storage) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, replies_path(storage))
    return d


class StationLXMF:
    def __init__(self, radio, storage):
        self.radio = radio
        self.path = os.path.join(storage, "subscribers.json")
        self.storage = storage
        self.replies = load_replies(storage)
        self.router = LXMF.LXMRouter(identity=radio.identity, storagepath=os.path.join(storage, "lxmf"))
        self.dest = self.router.register_delivery_identity(radio.identity, display_name=radio.name)
        self.router.register_delivery_callback(self._received)
        self.enabled = True
        self.lock = threading.Lock()
        self.subs = self._load()
        self.recent = {}
        self.key_wait = set()  # senders whose key is being fetched
        self.last_notify = 0
        self.outq = queue.Queue()
        threading.Thread(target=self._send_loop, daemon=True).start()

    # on/off without restart: off = address unregistered, nothing answered or announced
    def set_enabled(self, on):
        if on == self.enabled:
            return
        self.enabled = on
        if on:
            try:
                RNS.Transport.register_destination(self.dest)
            except KeyError:
                pass
        else:
            RNS.Transport.deregister_destination(self.dest)
        RNS.log(f"LXMF autoresponder {'on' if on else 'off'}", RNS.LOG_NOTICE)

    def announce(self):
        self.dest.display_name = self.radio.name
        self.router.announce(self.dest.hash)

    # subscribers
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return {k: v for k, v in json.load(f).items() if len(k) == 32}
        except Exception:
            return {}

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.subs, f, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            RNS.log(f"Could not save subscribers: {e}", RNS.LOG_ERROR)

    # incoming
    def _received(self, m):
        if not self.enabled:
            return
        src = m.source_hash
        if not m.signature_validated:
            if m.unverified_reason == LXMF.LXMessage.SOURCE_UNKNOWN and m.packed:
                with self.lock:
                    busy = src in self.key_wait or len(self.key_wait) >= KEY_WAIT_MAX
                    if not busy:
                        self.key_wait.add(src)
                if not busy:
                    RNS.log(f"LXMF from {src.hex()}: sender key unknown, asking the network", RNS.LOG_NOTICE)
                    threading.Thread(target=self._fetch_key, args=(m,), daemon=True).start()
                    return
            RNS.log(f"LXMF from {src.hex()} not verified, not answered", RNS.LOG_NOTICE)
            return
        self._handle(m)

    def _fetch_key(self, m):
        # the path answer carries the sender's announce, so its key: then check the signature again
        src = m.source_hash
        try:
            RNS.Transport.request_path(src)
            t = time.time()
            while RNS.Identity.recall(src) is None and time.time() - t < PATH_WAIT:
                time.sleep(1)
            m2 = LXMF.LXMessage.unpack_from_bytes(m.packed, m.method)
            if not m2.signature_validated:
                RNS.log(f"LXMF from {src.hex()} not verified (sender key not found), not answered", RNS.LOG_NOTICE)
                return
            self._handle(m2)
        except Exception as e:
            RNS.log(f"LXMF from {src.hex()}: key check failed: {e}", RNS.LOG_ERROR)
        finally:
            with self.lock:
                self.key_wait.discard(src)

    def _handle(self, m):
        src = m.source_hash
        name = lxmf_name(src)
        text = (m.content_as_string() or "").strip()
        RNS.log(f"LXMF from {who(src, name)}: {text[:60]!r}", RNS.LOG_NOTICE)
        now = time.time()
        with self.lock:
            hits = self.recent.setdefault(src, deque())
            while hits and now - hits[0] > RATE_WINDOW:
                hits.popleft()
            if len(hits) >= RATE_MAX:
                RNS.log(f"LXMF from {src.hex()}: over {RATE_MAX} messages/min, not answered", RNS.LOG_NOTICE)
                return
            hits.append(now)
        self.radio.stats.inc("lxmf_msgs")
        cmd = text.split()[0].lower().strip("/!.") if text else ""
        self.send(src, self._answer(cmd, src, name), cmd if cmd in self.COMMANDS else "hello")

    COMMANDS = ("help", "info", "status", "link", "subscribe", "unsubscribe")

    def _answer(self, cmd, src, name):
        if cmd == "link":
            return render(DEFAULT_REPLIES["link"], self.values(), "link")  # fixed: addresses + repo
        if cmd in ("help", "info"):
            return self.reply(cmd)
        if cmd == "status":
            return self.reply("status_on" if self.radio.meta()["air"] else "status_off")
        if cmd == "subscribe":
            k = src.hex()
            with self.lock:
                new = k not in self.subs
                self.subs[k] = {"name": name, "since": int(self.subs.get(k, {}).get("since", time.time()))}
                self._save()
                n = len(self.subs)
            RNS.log(f"Subscribed {who(src, name)}" if new else f"Already subscribed: {who(src, name)}"
                    , RNS.LOG_NOTICE)
            if new:
                self.radio.stats.inc("subscribed")
                RNS.log(f"Subscribers: {n}", RNS.LOG_NOTICE)
            return self.reply("subscribed" if new else "already_subscribed")
        if cmd == "unsubscribe":
            with self.lock:
                was = self.subs.pop(src.hex(), None) is not None
                if was:
                    self._save()
                n = len(self.subs)
            if was:
                self.radio.stats.inc("unsubscribed")
                RNS.log(f"Unsubscribed {who(src, name)}, subscribers: {n}", RNS.LOG_NOTICE)
            return self.reply("unsubscribed" if was else "not_subscribed")
        return self.reply("hello")

    # replies: templates from storage/replies.json, the defaults where empty
    def values(self):
        r = self.radio
        m = r.meta()
        st = RNS.Destination.hash_from_name_and_identity("radiolxst.station", bytes.fromhex(r.identity_hash))
        return {
            "name": r.name, "version": m["v"], "source": r.source_kind() or "none", "kbps": m["kb"],
            "subscribers": len(self.subs), "listeners": m["l"], "slots": m.get("s") or "no limit",
            "uptime": RNS.prettytime(time.time() - r.on_air_since) if r.on_air and r.on_air_since else "-",
            "now_playing": r.now_playing_text(), "call_id": r.identity_hash, "lxst": r.dest.hash.hex(),
            "lxmf": self.dest.hash.hex(), "station": st.hex(), "repo": REPO_URL,
            "stats": lxmf_text(r.stats.view()),
        }

    def reply(self, key, values=None):
        return render(self.replies.get(key) or DEFAULT_REPLIES[key], values or self.values(), key)

    def reload_replies(self):
        self.replies = load_replies(self.storage)

    # outgoing
    def notify_on_air(self):
        threading.Thread(target=self._notify, daemon=True).start()

    def _notify(self):
        time.sleep(NOTIFY_DELAY)
        r = self.radio
        if not self.enabled or not r.on_air:
            return
        with self.lock:
            subs = list(self.subs)
        if not subs:
            return
        if time.time() - self.last_notify < NOTIFY_MIN_GAP:
            RNS.log(f"On-air message to subscribers skipped, last one under "
                    f"{NOTIFY_MIN_GAP // 60} min ago", RNS.LOG_NOTICE)
            return
        self.last_notify = time.time()
        text = self.reply("on_air")
        RNS.log(f"On-air message to {len(subs)} subscriber(s)", RNS.LOG_NOTICE)
        for k in subs:
            self.send(bytes.fromhex(k), text, "on-air")

    def send(self, dest_hash, text, what):
        self.outq.put((dest_hash, text, what))

    def _send_loop(self):
        while True:
            dest_hash, text, what = self.outq.get()
            try:
                self._send(dest_hash, text, what)
            except Exception as e:
                RNS.log(f"LXMF {what} to {dest_hash.hex()} failed: {e}", RNS.LOG_ERROR)
            time.sleep(SEND_SPACING)

    def _send(self, dest_hash, text, what):
        if not self.enabled:
            return
        ident = RNS.Identity.recall(dest_hash)
        if ident is None:
            RNS.Transport.request_path(dest_hash)
            t = time.time()
            while ident is None and time.time() - t < PATH_WAIT:
                time.sleep(1)
                ident = RNS.Identity.recall(dest_hash)
        if ident is None:
            RNS.log(f"LXMF {what} to {dest_hash.hex()} failed: address unknown, no path", RNS.LOG_NOTICE)
            return
        cost = self.router.get_outbound_stamp_cost(dest_hash)
        if cost and cost > STAMP_COST_MAX:
            RNS.log(f"LXMF {what} to {who(dest_hash)} skipped: asks a stamp cost {cost}", RNS.LOG_NOTICE)
            return
        out = RNS.Destination(ident, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
        m = LXMF.LXMessage(out, self.dest, text, desired_method=LXMF.LXMessage.OPPORTUNISTIC)
        label = who(dest_hash)
        m.register_delivery_callback(lambda msg: RNS.log(f"LXMF {what} delivered to {label}", RNS.LOG_NOTICE))
        m.register_failed_callback(lambda msg: RNS.log(f"LXMF {what} to {label} failed", RNS.LOG_NOTICE))
        self.router.handle_outbound(m)
        RNS.log(f"LXMF {what} sent to {label}", RNS.LOG_NOTICE)
