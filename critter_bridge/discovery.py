"""UDP beacon listener — auto-discovery of critterOS boards.

Boards broadcast a small beacon every udp_beacon_secs (default 10 s) on
udp_port (default 1234):

    ID: <device_name>
    IP: http://<board-ip>
    MAC: <mac>
    Panels:<id>,<id>

The listener parses these and hands identity to the store:
  * an existing board is linked to the beacon (MAC recorded, host refreshed
    from the beacon so DHCP address changes self-heal),
  * an unknown MAC is auto-registered when the "auto_add" setting is on,
    otherwise it shows up in the UI "Discovered" list to be adopted.

Beacons carry identity, never state, so panel values always come from
`/api/dashboard`. Telegram triggers are the one board-originated push path.
"""

import logging
import socket
import threading
import time

log = logging.getLogger(__name__)

RECV_BUFFER = 1024


def parse_beacon(text, from_ip):
    """Parse 'ID:/MAC:/IP:/Panels:' beacon lines. Returns dict or None."""
    beacon = {"device": "", "mac": "", "ip": from_ip, "panel_ids": []}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("ID:"):
            beacon["device"] = line[3:].strip()
        elif line.startswith("MAC:"):
            mac = line[4:].strip().upper()
            if mac:
                beacon["mac"] = mac
        elif line.startswith("IP:"):
            ip = line[3:].strip().replace("http://", "").strip()
            if ip:
                beacon["ip"] = ip
        elif line.startswith("Panels:"):
            beacon["panel_ids"] = [p.strip() for p in line[7:].split(",") if p.strip()]
    return beacon if beacon["mac"] else None


class Discovery:
    def __init__(self, store, events):
        self.store = store
        self.events = events
        # beacons from boards that are NOT registered (auto_add off)
        self.discovered = {}
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="discovery").start()

    def stop(self):
        self._stop.set()

    def adopt_lookup(self, mac):
        return dict(self.discovered.get(mac, {}))

    def adopt_done(self, mac):
        self.discovered.pop(mac, None)

    # ---------- loop ----------

    def _loop(self):
        sock = None
        bound_port = None
        while not self._stop.is_set():
            try:
                port = int(self.store.settings().get("udp_beacon_port", 1234) or 0)
            except (TypeError, ValueError):
                port = 1234

            # settings changed or discovery disabled: rebind / close
            if port != bound_port and sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
                bound_port = None

            if not port:
                self._stop.wait(1.0)
                continue

            if sock is None:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.settimeout(1.0)
                    sock.bind(("", port))
                    bound_port = port
                    self.events.add("info", "discovery listening on udp/%d" % port)
                except OSError as e:
                    log.warning("beacon listener on udp/%d failed: %s", port, e)
                    if sock:
                        try:
                            sock.close()
                        except OSError:
                            pass
                    sock = None
                    self._stop.wait(5.0)
                    continue

            try:
                data, addr = sock.recvfrom(RECV_BUFFER)
            except socket.timeout:
                continue
            except OSError:
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
                bound_port = None
                continue

            try:
                self._handle(data, addr)
            except Exception:
                log.exception("beacon handling failed")

    def _handle(self, data, addr):
        text = bytes(data).decode("utf-8", "replace")
        if "MAC:" not in text or "ID:" not in text:
            return  # value packets ("FF ..." etc.) are not our concern
        beacon = parse_beacon(text, addr[0])
        if not beacon:
            return
        mac = beacon["mac"]
        result = self.store.upsert_discovered(mac, beacon["device"], beacon["ip"])
        if result == "added":
            self.events.add(
                "info",
                "discovered board: %s (%s at %s)" % (beacon["device"] or "unknown", mac, beacon["ip"]),
            )
            self.discovered.pop(mac, None)
        elif result == "known":
            self.discovered[mac] = {
                "mac": mac,
                "device": beacon["device"],
                "ip": beacon["ip"],
                "panel_ids": beacon["panel_ids"],
                "last_seen": time.time(),
            }
        else:
            # registered board; store handled the host refresh, stay quiet
            self.discovered.pop(mac, None)
