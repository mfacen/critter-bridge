"""Cloud backend registry.

Each backend turns a freshly polled board's panel values into its own API
calls. Backends are selectable per board ("backend" + "backend_cfg" in the
board config); every credential lives on the bridge. All are free tiers:

  thingspeak — one channel per board; panel → field 1–8 mapping. Clean fit
               because the free tier allows one write per channel per ~15 s,
               matching the poll cadence exactly.
  adafruit   — one feed per panel under one account; feeds are auto-created
               on first write. Free tier allows ~30 writes/min per account
               (a burst of mapped feeds lands well inside that at 15 s polls).
  influx     — one line-protocol write per poll with all panels batched;
               points are tagged device/panel, bucketed in your own cloud
               bucket. No per-panel mapping needed.
  layerbase  — PostgreSQL-compatible (Layerbase) database; all numeric panel
               values are inserted into a table (auto-created) per poll.
"""

import logging
import time

import requests

from . import thingspeak as thingspeak_module

log = logging.getLogger(__name__)


def _tag(value, fallback="x"):
    value = str(value or "").strip() or fallback
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in value)


class ThingSpeak:
    name = "thingspeak"

    def send(self, board, values):
        return thingspeak_module.forward(board, values)


class Adafruit:
    name = "adafruit"
    MIN_GAP_S = 2.5  # per-account burst spacing; free tier is ~30 writes/min
    _last_burst = {}  # username -> monotonic

    def send(self, board, values):
        cfg = board.get("backend_cfg") or {}
        user = (cfg.get("username") or "").strip()
        key = (cfg.get("key") or "").strip()
        feeds = cfg.get("feeds") or {}
        if not user or not key:
            return None, "username / AIO key missing"

        pairs = []
        for pid, feed in feeds.items():
            feed = str(feed or "").strip().strip("/")
            if feed and pid in values:
                pairs.append((pid, feed))
        if not pairs:
            return None, "no panels mapped to feeds"

        now = time.monotonic()
        if now - self._last_burst.get(user, 0.0) < self.MIN_GAP_S:
            return None, "deferred (Adafruit cadence)"
        self._last_burst[user] = now

        base = "https://io.adafruit.com/api/v2/%s" % requests.utils.quote(user, safe="")
        headers = {"X-AIO-Key": key}
        ok_n, last_err = 0, ""
        for pid, feed in pairs:
            feed_url = base + "/feeds/%s/data" % requests.utils.quote(feed, safe="")
            try:
                resp = requests.post(feed_url, json={"value": float(values[pid])}, headers=headers, timeout=8)
                if resp.status_code == 404:
                    # feed doesn't exist yet: create it, retry once
                    mk = requests.post(base + "/feeds", json={"name": feed}, headers=headers, timeout=8)
                    if mk.ok:
                        resp = requests.post(feed_url, json={"value": float(values[pid])}, headers=headers, timeout=8)
                if resp.ok:
                    ok_n += 1
                else:
                    last_err = "HTTP %s: %s" % (resp.status_code, (resp.text or "")[:120])
            except (TypeError, ValueError):
                continue
            except Exception as e:
                last_err = "request failed: %s" % e
        if ok_n == len(pairs):
            return True, "%d feed(s) ok" % ok_n
        if ok_n:
            return False, "%d/%d ok; last: %s" % (ok_n, len(pairs), last_err)
        return False, last_err or "no writes"


class Influx:
    name = "influx"

    def send(self, board, values):
        cfg = board.get("backend_cfg") or {}
        url = (cfg.get("url") or "").strip().rstrip("/")
        org = (cfg.get("org") or "").strip()
        bucket = (cfg.get("bucket") or "").strip()
        token = (cfg.get("token") or "").strip()
        if not (url and org and bucket and token):
            return None, "url / org / bucket / token missing"
        write_url = url if url.endswith("/api/v2/write") else url + "/api/v2/write"
        measurement = _tag(cfg.get("measurement") or board.get("name") or "critter")
        device = _tag(board.get("name") or board.get("id") or "board")

        lines = []
        for pid, v in values.items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            lines.append("%s,device=%s,panel=%s value=%s" % (measurement, device, _tag(pid), fv))
        if not lines:
            return None, "no numeric values this poll"

        try:
            resp = requests.post(
                write_url,
                params={"org": org, "bucket": bucket},
                headers={"Authorization": "Token " + token, "Content-Type": "text/plain; charset=utf-8"},
                data="\n".join(lines),
                timeout=8,
            )
        except Exception as e:
            return False, "request failed: %s" % e
        if 200 <= resp.status_code < 300:
            return True, "%d point(s)" % len(lines)
        return False, "HTTP %s: %s" % (resp.status_code, (resp.text or "")[:160])


class Layerbase:
    name = "layerbase"

    @staticmethod
    def _safe_table(table):
        table = (table or "").strip() or "panel_values"
        out = "".join(c if (c.isalnum() or c == "_") else "_" for c in table)
        return out or "panel_values"

    @staticmethod
    def _sql_str(value):
        # Basic SQL string literal escaping for SQLite-compatible query API.
        return "'" + str(value).replace("'", "''") + "'"

    def _send_http(self, board, values, cfg):
        api_url = (cfg.get("api_url") or "").strip()
        api_key = (cfg.get("api_key") or "").strip()
        table = self._safe_table(cfg.get("table"))
        if not api_url or not api_key:
            return None, "api_url / api_key missing"

        device = str(board.get("name") or board.get("id") or "board")
        rows = []
        for pid, v in values.items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            rows.append("(%s,%s,%s)" % (self._sql_str(device), self._sql_str(pid), repr(fv)))
        if not rows:
            return None, "no numeric values this poll"

        headers = {
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        }
        create_sql = (
            "CREATE TABLE IF NOT EXISTS %s ("
            "device TEXT NOT NULL,"
            "panel TEXT NOT NULL,"
            "value REAL NOT NULL,"
            "ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")" % table
        )
        insert_sql = "INSERT INTO %s (device,panel,value) VALUES %s" % (table, ",".join(rows))
        try:
            resp = requests.post(api_url, json={"query": create_sql}, headers=headers, timeout=12)
            if not resp.ok:
                body = (resp.text or "")
                # Some SQLite-compatible engines may reject IF NOT EXISTS; tolerate existing table.
                if resp.status_code == 400 and "already exists" in body.lower():
                    pass
                else:
                    return False, "HTTP %s: %s" % (resp.status_code, body[:160])
            resp = requests.post(api_url, json={"query": insert_sql}, headers=headers, timeout=12)
            if not resp.ok:
                return False, "HTTP %s: %s" % (resp.status_code, (resp.text or "")[:160])
            data = {}
            try:
                data = resp.json() or {}
            except Exception:
                data = {}
            count = data.get("rowCount")
            return True, "%s point(s)" % (count if count is not None else len(rows))
        except Exception as e:
            return False, "request failed: %s" % e

    def send(self, board, values):
        cfg = board.get("backend_cfg") or {}
        if (cfg.get("api_key") or "").strip() or (cfg.get("api_url") or "").strip():
            return self._send_http(board, values, cfg)

        try:
            import psycopg2
        except Exception as e:
            return False, "psycopg2 not installed: %s" % e

        host = (cfg.get("host") or "").strip()
        port = str(cfg.get("port") or "5432").strip() or "5432"
        user = (cfg.get("user") or "").strip()
        password = (cfg.get("password") or "").strip()
        dbname = (cfg.get("dbname") or "").strip()
        table = self._safe_table(cfg.get("table"))
        if not (host and user and dbname and password):
            return None, "host / user / password / dbname missing (or set api_url / api_key for HTTP API)"

        device = str(board.get("name") or board.get("id") or "board")
        rows = []
        for pid, v in values.items():
            try:
                rows.append((device, str(pid), float(v)))
            except (TypeError, ValueError):
                continue
        if not rows:
            return None, "no numeric values this poll"

        conn = None
        cur = None
        try:
            conn = psycopg2.connect(
                host=host,
                port=int(port),
                user=user,
                password=password,
                dbname=dbname,
                sslmode="require",
                connect_timeout=8,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5,
            )
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS %s ("
                "device TEXT NOT NULL,"
                "panel TEXT NOT NULL,"
                "value REAL NOT NULL,"
                "ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ")" % table
            )
            cur.executemany(
                "INSERT INTO %s (device, panel, value) VALUES (%%s, %%s, %%s)" % table,
                rows,
            )
            conn.commit()
            return True, "%d point(s)" % len(rows)
        except Exception as e:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return False, "request failed: %s" % e
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


REGISTRY = {cls.name: cls() for cls in (ThingSpeak, Adafruit, Influx, Layerbase)}


def forward(board, values):
    """Dispatch a poll's values to the board's configured backend.

    Returns (ok, detail): True sent, False failed, None skipped
    (nothing selected / missing cfg / cadence-deferred).
    """
    backend = (board.get("backend") or "").strip()
    if not backend:
        return None, "no backend selected"
    impl = REGISTRY.get(backend)
    if impl is None:
        return False, "unknown backend %r" % backend
    return impl.send(board, values)
