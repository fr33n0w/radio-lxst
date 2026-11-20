# Watches the link to the shared RNS instance (rnsd or the app running it) and logs loss and return.

import threading
import time

import RNS


def shared_instance():
    """The interface to the shared RNS instance, None if we run our own."""
    for i in list(RNS.Transport.interfaces):
        if type(i).__name__ == "LocalClientInterface" and getattr(i, "is_connected_to_shared_instance", False):
            return i
    return None


def lost():
    i = shared_instance()
    return i is not None and not i.online


def _hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def start(every=1.0):
    """Background thread: one log line when the shared instance goes away, one when it is back."""
    def loop():
        was, since = False, 0.0
        while True:
            now = lost()
            if now and not was:
                since = time.time()
                RNS.log("Reticulum shared instance lost, waiting to reconnect... (is the rnsd instance running?)",
                        RNS.LOG_WARNING)
            elif was and not now:
                RNS.log(f"Reticulum shared instance back after {_hms(time.time() - since)}, network connection ready!",
                        RNS.LOG_NOTICE)
            was = now
            time.sleep(every)
    threading.Thread(target=loop, daemon=True).start()
