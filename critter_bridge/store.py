import json
import logging
import os
import threading
import uuid

log = logging.getLogger(__name__)

VALID_BACKENDS = ("thingspeak", "adafruit", "influx")
_BACKEND_STRKEYS = {
    "thingspeak": ("write_key",),
    "adafruit": ("username", "key"),
    "influx": ("url", "org", "bucket", "token", "measurement"),
}
_BACKEND_MAPKEY = {
    "thingspeak": "fields",
    "adafruit": "feeds",
}


def _clean_map_value(mapkey, value):
    if mapkey == "fields":
        try:
            f = int(value)
        except (TypeError, ValueError):
            return None
        return f if 1 <= f <= 8 else None
    v = str(value or "").strip().strip("/")[:64]  # feed name
    return v or None


def _uid(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:6])


def _clamp_interval(v, floor):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return max(floor, int(v)) if v else None


def _fnum(v, minimum=None):
    """Lenient float coercion; returns None when the value is unusable."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if minimum is not None and v < minimum:
        v = minimum
    return v


class Store:
    """Thread-safe JSON config: settings, boards, rules.

    Loaded at startup, kept in memory, saved atomically on every change.
    """

    def __init__(self, path, defaults):
        self.path = path
        self._defaults = defaults or {}
        self._lock = threading.RLock()
        self._data = dict(defaults or {})
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                log.info("loaded config from %s", self.path)
            except Exception as e:
                log.error("config unreadable (%s); starting fresh: %s", self.path, e)
        # backfill schema keys a config file written by an older version lacks
        merged_settings = dict(self._defaults.get("settings") or {})
        merged_settings.update(self._data.get("settings") or {})
        self._data["settings"] = merged_settings
        self._data.setdefault("boards", [])
        self._data.setdefault("rules", [])
        # v0.2 → v0.3 migration: per-board cloud backend selection.
        # A legacy "thingspeak" dict becomes backend=thingspeak + backend_cfg.
        for b in self._data.get("boards", []):
            if not isinstance(b, dict):
                continue
            legacy = b.pop("thingspeak", None)
            if "backend" not in b:
                b["backend"] = "thingspeak" if (legacy or {}).get("write_key") else ""
                b["backend_cfg"] = legacy or {}
            elif legacy:
                b.setdefault("backend_cfg", legacy)
            b.setdefault("backend_cfg", {})
            b.setdefault("mac", "")

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    # ---- settings ----

    def settings(self):
        with self._lock:
            return dict(self._data.get("settings", {}))

    def update_settings(self, patch):
        with self._lock:
            s = self._data["settings"]
            for key in ("listen_host", "telegram_token", "telegram_chat_id"):
                if key in patch and patch[key] is not None:
                    s[key] = str(patch[key])
            iv = _clamp_interval(patch.get("poll_interval"), 5)
            if iv is not None:
                s["poll_interval"] = iv
            if "auto_add" in patch and patch["auto_add"] is not None:
                s["auto_add"] = bool(patch["auto_add"])
            if "udp_beacon_port" in patch and patch["udp_beacon_port"] is not None:
                try:
                    port = int(patch["udp_beacon_port"])
                except (TypeError, ValueError):
                    port = None
                if port is not None and 0 <= port <= 65535:
                    s["udp_beacon_port"] = port
            p = patch.get("listen_port")
            if p:
                try:
                    s["listen_port"] = int(p)
                except (TypeError, ValueError):
                    pass
            self.save()
            return dict(s)

    # ---- boards ----

    def _find_board(self, board_id):
        for b in self._data.get("boards", []):
            if b.get("id") == board_id:
                return dict(b)
        return None

    def boards(self):
        with self._lock:
            return [dict(b) for b in self._data.get("boards", [])]

    def board(self, board_id):
        with self._lock:
            return self._find_board(board_id)

    def add_board(self, data):
        with self._lock:
            backend = str(data.get("backend") or "").strip()
            if backend and backend not in VALID_BACKENDS:
                raise ValueError("unknown backend %r" % backend)
            board = {
                "id": _uid("b"),
                "name": str(data.get("name") or "").strip(),
                "host": str(data.get("host") or "").strip(),
                "mac": str(data.get("mac") or "").strip().upper(),
                "poll_interval": _clamp_interval(data.get("poll_interval"), 0) or 0,  # 0 = global
                "enabled": bool(data.get("enabled", True)),
                "backend": backend,
                "backend_cfg": {},
            }
            self._push_backend_cfg(board, backend, data.get("backend_cfg"))
            if not board["host"]:
                raise ValueError("host is required")
            self._data["boards"].append(board)
            self.save()
            return dict(board)

    def update_board(self, board_id, patch):
        with self._lock:
            for b in self._data.get("boards", []):
                if b.get("id") != board_id:
                    continue
                if "name" in patch and patch["name"] is not None:
                    b["name"] = str(patch["name"]).strip()
                if "host" in patch and patch["host"] and str(patch["host"]).strip():
                    b["host"] = str(patch["host"]).strip()
                iv = _clamp_interval(patch.get("poll_interval"), 0)
                if iv is not None:
                    b["poll_interval"] = iv  # 0 = use global
                if "enabled" in patch and patch["enabled"] is not None:
                    b["enabled"] = bool(patch["enabled"])
                if "backend" in patch:
                    backend = str(patch["backend"] or "").strip()
                    if backend and backend not in VALID_BACKENDS:
                        raise ValueError("unknown backend %r" % backend)
                    b["backend"] = backend
                b.setdefault("backend_cfg", {})
                self._push_backend_cfg(b, b.get("backend") or "", patch.get("backend_cfg"))
                self.save()
                return dict(b)
            return None

    @staticmethod
    def _push_backend_cfg(b, backend, data):
        """Merge a backend config patch (shallow; per-key validation).
        Keys not in the patch keep their values, so switching backends and
        remapping one panel never wipes sibling configs."""
        if not isinstance(data, dict):
            return
        cur = dict(b.get("backend_cfg") or {})
        for key in _BACKEND_STRKEYS.get(backend, ()):
            if key in data and data[key] is not None:
                cur[key] = str(data[key]).strip()
        mapkey = _BACKEND_MAPKEY.get(backend)
        if mapkey and isinstance(data.get(mapkey), dict):
            clean = {}
            for pid, value in data[mapkey].items():
                pid = str(pid).strip()
                value = _clean_map_value(mapkey, value)
                if pid and value is not None:
                    clean[pid] = value
            cur[mapkey] = clean
        b["backend_cfg"] = cur

    def remove_board(self, board_id):
        with self._lock:
            before = len(self._data["boards"])
            self._data["boards"] = [b for b in self._data["boards"] if b.get("id") != board_id]
            removed = len(self._data["boards"]) != before
            if removed:
                # cascade: rules tied to this board are meaningless without it
                self._data["rules"] = [r for r in self._data["rules"] if r.get("board_id") != board_id]
                self.save()
            return removed

    def upsert_discovered(self, mac, device, ip):
        """File a beacon identity. Returns the action taken:
        'added'   — unknown MAC, auto-registered (auto_add on)
        'linked'  — existing manual board got its MAC recorded
        'tracked' — registered board, host refreshed from the beacon
        'known'   — unknown MAC while auto_add is off (UI can adopt it)
        """
        with self._lock:
            mac = (mac or "").upper()
            boards = self._data["boards"]
            for b in boards:
                if (b.get("mac") or "").upper() == mac:
                    if ip and b.get("host") != ip:
                        b["host"] = ip  # the board knows where it lives now
                        self.save()
                    return "tracked"
            for b in boards:
                if not b.get("mac") and b.get("host") == ip:
                    b["mac"] = mac
                    self.save()
                    return "linked"
            if not self._data["settings"].get("auto_add", True):
                return "known"
            name = (device or "").strip() or "firefly"
            if name in {b.get("name") for b in boards}:
                name = "%s %s" % (name, mac[-5:])
            boards.append({
                "id": _uid("b"),
                "name": name,
                "host": ip,
                "mac": mac,
                "poll_interval": 0,
                "enabled": True,
                "backend": "",
                "backend_cfg": {},
            })
            self.save()
            return "added"

    # ---- rules ----

    def rules(self):
        with self._lock:
            return [dict(r) for r in self._data.get("rules", [])]

    def rule(self, rule_id):
        with self._lock:
            for r in self._data.get("rules", []):
                if r.get("id") == rule_id:
                    return dict(r)
        return None

    def add_rule(self, data):
        with self._lock:
            rule = {
                "id": _uid("r"),
                "board_id": str(data.get("board_id") or ""),
                "panel_id": str(data.get("panel_id") or ""),
                "op": str(data.get("op") or ">").strip(),
                "threshold": float(data.get("threshold") or 0),
                "hysteresis": max(0.0, float(data.get("hysteresis") or 0)),
                "cooldown_s": max(10, int(float(data.get("cooldown_s") or 300))),
                "chat_id": str(data.get("chat_id") or "").strip(),
                "message": str(data.get("message") or "{device}: {panel} = {value}").strip()
                           or "{device}: {panel} = {value}",
                "enabled": bool(data.get("enabled", True)),
            }
            if rule["op"] not in ("<", "<=", ">", ">=", "==", "!="):
                raise ValueError("bad op")
            if not rule["board_id"] or not self.board(rule["board_id"]):
                raise ValueError("rule needs a registered board")
            if not rule["panel_id"]:
                raise ValueError("rule needs a panel")
            self._data["rules"].append(rule)
            self.save()
            return dict(rule)

    def update_rule(self, rule_id, patch):
        with self._lock:
            for r in self._data.get("rules", []):
                if r.get("id") != rule_id:
                    continue
                thresh = _fnum(patch.get("threshold"))
                if thresh is not None:
                    r["threshold"] = thresh
                hyst = _fnum(patch.get("hysteresis"), minimum=0.0)
                if hyst is not None:
                    r["hysteresis"] = hyst
                cooldown = _fnum(patch.get("cooldown_s"))
                if cooldown:
                    r["cooldown_s"] = max(10, int(cooldown))
                for key in ("op", "chat_id", "message", "panel_id"):
                    if patch.get(key) is not None:
                        r[key] = str(patch[key]).strip()
                if r["op"] not in ("<", "<=", ">", ">=", "==", "!="):
                    raise ValueError("bad op")
                if patch.get("enabled") is not None:
                    r["enabled"] = bool(patch["enabled"])
                self.save()
                return dict(r)
            return None

    def remove_rule(self, rule_id):
        with self._lock:
            before = len(self._data["rules"])
            self._data["rules"] = [r for r in self._data["rules"] if r.get("id") != rule_id]
            removed = len(self._data["rules"]) != before
            if removed:
                self.save()
            return removed
