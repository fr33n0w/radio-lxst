# New version check on GitHub: only tells, never installs.

import re
import threading
import urllib.request

REPO_URL = "https://github.com/fr33n0w/radio-lxst"
VERSION_URL = "https://raw.githubusercontent.com/fr33n0w/radio-lxst/main/radio_lxst.py"
CHECK_EVERY = 12 * 3600


def _parts(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def latest_version(timeout=15):
    """Version published on GitHub, None if it cannot be read."""
    try:
        req = urllib.request.Request(VERSION_URL, headers={"User-Agent": "radio-lxst"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read(200_000).decode("utf-8", "replace")
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
        return m.group(1) if m else None
    except Exception:
        return None


def newer(latest, current):
    try:
        return bool(latest) and _parts(latest) > _parts(current)
    except Exception:
        return False


def watch(current, on_new, every=CHECK_EVERY):
    """Checks now and every `every` s in a thread; on_new(version) once per new version found."""
    def loop():
        told = None
        while True:
            v = latest_version()
            if newer(v, current) and v != told:
                told = v
                try:
                    on_new(v)
                except Exception:
                    pass
            ev.wait(every)
    ev = threading.Event()
    threading.Thread(target=loop, daemon=True).start()
    return ev
