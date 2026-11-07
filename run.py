#!/usr/bin/env python3
"""critter-bridge entrypoint: python3 run.py"""

import logging
import sys

from critter_bridge import config
from critter_bridge.api import create_app
from critter_bridge.discovery import Discovery
from critter_bridge.events import Events
from critter_bridge.notifier import Notifier
from critter_bridge.poller import Poller
from critter_bridge.store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)


def main():
    defaults = {
        "settings": dict(config.DEFAULT_SETTINGS),
        "boards": [],
        "rules": [],
    }
    store = Store(config.CONFIG_PATH, defaults)
    events = Events()
    discovery = Discovery(store, events)
    discovery.start()
    poller = Poller(store, events)
    poller.start()
    notifier = Notifier(store, events)
    notifier.start()
    app = create_app(store, poller, events, discovery, notifier)

    settings = store.settings()
    host = settings.get("listen_host", "0.0.0.0")
    port = int(settings.get("listen_port", 8787))
    print("critter-bridge %s  (config: %s)" % (config.APP_VERSION, config.CONFIG_PATH))
    print("dashboard: http://%s:%d" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port))

    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    sys.exit(main())
