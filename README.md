# critter-bridge

Companion app for **critterOS / Firefly** boards. It bridges your boards to the
internet over HTTPS — something the ESP8266 cannot do reliably — by *pulling*
state from the boards and pushing it out to cloud services, and by accepting
board-originated Telegram pushes from newer firmware.

**Architecture: pull + optional board push.** The bridge polls each board's
existing `/api/dashboard` endpoint on an interval and:

- forwards panel values to **ThingSpeak** (first backend; Adafruit IO / InfluxDB Cloud planned),
- evaluates panel-state **rules** and sends **Telegram** messages on threshold crossings,
- accepts `POST /api/telegram/send` pushes from the board itself and delivers
  them from a background worker,
- exposes a small local web UI for board registry, ThingSpeak mapping, rules, settings and an event log.

Older boards work with no firmware changes: the bridge is still a pure companion
layer. Newer firmware can also push a Telegram trigger directly, using the
Logger server name (`logger_host`, or legacy `server_name`) as the bridge host
on port `8787`. No separate bridge-host setting is needed, and no bot token is
sent from the board in bridge mode.

## Quickstart

```bash
./run.sh          # creates .venv on first run, then starts the app
# open http://localhost:8787
```

Equivalent manual steps:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 run.py
```

Configuration is stored in `config.json` next to the app (created on first
save). The web UI writes it for you; you can also edit it while the app is
running (changes are picked up on the next poll cycle).

## Concepts

| Piece | What it does |
|---|---|
| **Boards** | Registered by IP (or `host`), or discovered automatically from the boards' UDP beacon (see below). Status shows last poll time / last error. A board can set its own poll interval (otherwise the global one is used). The beacon keeps the stored IP fresh, so DHCP address changes self-heal. |
| **ThingSpeak** | Per board: a write API key plus a panel → field (1–8) mapping. Default model is *one board = one channel*, so multiple boards never contend for ThingSpeak's free-tier ~15 s update cadence. |
| **Rules** | A rule ties a panel to an operator (`< <= > >= == !=`) and a threshold; when true, a message is sent to a Telegram chat. Messages support `{device}`, `{panel}`, `{value}` placeholders (temperature is strung through the value rendering). Hysteresis and a per-rule cooldown prevent flappy spam. Rules skip panels of type `telegram`; the board sends those itself. |
| **Telegram** | Set a bot token and a default chat id in Settings. The test button and board-originated `/api/telegram/send` pushes use the default chat unless the request supplies `chat_id`. |
| **Events** | Rolling log (last 200) of polls, ThingSpeak results, Telegram deliveries and errors — visible in the UI and at `GET /api/events`-style `/api/status`. |

## Board push API

Newer critterOS firmware posts Telegram triggers here. The request only
enqueues the message; a background worker calls Telegram so the board never
waits on HTTPS.

```http
POST /api/telegram/send
Content-Type: application/json

{"text":"pressure is high","device":"firefly","panel":"tg1","mac":"AA:BB:CC:DD:EE:FF"}
```

`text` is required. `device`, `mac`, and `panel` are only used for the event
log. The bridge uses `telegram_token` plus either the request's `chat_id` or
the configured default `telegram_chat_id`.

Responses:

| Status | Meaning |
| --- | --- |
| `202` | `{"ok":true,"queued":true}` — accepted and queued |
| `400` | Missing text, bot token, or chat id |
| `503` | Notifier queue is full; retry later |

## Auto-discovery

critterOS boards already broadcast a UDP beacon every `udp_beacon_secs`
(default 10 s) on `udp_port` (default 1234):

```
ID: firefly
IP: http://192.168.2.243
MAC: C8:C9:A3:64:80:C8
Panels:pressure,distance
```

The bridge listens on that port (setting **Beacon port**, `0` disables) and:

- **auto-add on** (default): any beacon from an unregistered board adds it,
  named after its `device_name` (a MAC suffix disambiguates duplicates).
- **auto-add off**: beacons appear under *Discovered* in the UI with an
  *Add* button; nothing is registered without you clicking.
- a beacon from a board you already registered updates its stored IP if it
  changed (DHCP-proof tracking).

Beacons carry identity only — panel state still comes from `/api/dashboard`,
so the polling workflows are untouched. Note that
two boards with the same `device_name` both beacon as `firefly`; the
auto-name disambiguates, but distinct names are still tidier.

## Design notes / assumptions

- **Trusted LAN.** Traffic between the bridge and boards is plaintext HTTP.
  This matches critterOS's documented hobbyist stance: do not expose the board
  or the bridge to networks you don't trust. No tokens are between them.
- **The poll wakes the board's OLED.** `/api/dashboard` counts as a viewer in
  critterOS (`displayService.wake()`), so a polled board intentionally keeps
  its display awake. That is accepted behavior for v1.
- **Rule counters live in memory.** A bridge restart resets re-arm/cooldown
  state; this is fine for hobbyist use.
- **Telegram & ThingSpeak credentials live only here** in bridge mode, never on
  a board. ESP32 direct-cloud mode is the exception: that board stores its own
  credentials.

## Roadmap

- **Next:** more backends behind the same pull model (Adafruit IO, InfluxDB Cloud),
  value gathering per cycle, config file UX polish.
- **Packaging:** single-file executable (PyInstaller) and/or Docker image; a two-line
  systemd unit for Pi users.
- **Later:** more backends, richer rule actions, and config file UX polish.
