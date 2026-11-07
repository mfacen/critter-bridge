import os

APP_NAME = "critter-bridge"
APP_VERSION = "0.3.0"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.environ.get("CRITTER_BRIDGE_CONFIG", os.path.join(BASE_DIR, "config.json"))
STATIC_DIR = os.path.join(BASE_DIR, "static")

DEFAULT_SETTINGS = {
    "poll_interval": 15,      # seconds between dashboard polls (per board unless overridden)
    "auto_add": True,         # register boards as soon as their beacon is heard
    "udp_beacon_port": 1234,  # listen for critterOS beacons (udp_port on the boards); 0 = off
    "listen_host": "0.0.0.0",
    "listen_port": 8787,
    "telegram_token": "",     # Telegram bot token; chars sent from here, never from a board
    "telegram_chat_id": "",   # default chat for board-originated pushes and the test button
}

MIN_POLL_INTERVAL = 5       # floor for any poll interval, global or per board
