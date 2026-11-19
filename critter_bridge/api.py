"""Local web API + static UI."""

import logging
import queue

from flask import Flask, jsonify, request, send_from_directory

from . import config
from . import localdb
from . import mariadb_log
from . import telegram

log = logging.getLogger(__name__)


def create_app(store, poller, events, discovery=None, notifier=None):
    app = Flask("critter-bridge")

    # ---- static UI ----

    @app.get("/")
    def index():
        return send_from_directory(config.STATIC_DIR, "index.html")

    @app.get("/app.js")
    def app_js():
        return send_from_directory(config.STATIC_DIR, "app.js")

    @app.get("/style.css")
    def style():
        return send_from_directory(config.STATIC_DIR, "style.css")

    @app.get("/nadias_logo.jpeg")
    def logo():
        return send_from_directory(config.STATIC_DIR, "nadias_logo.jpeg")

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(config.STATIC_DIR, "favicon.ico")

    def err(msg, code=400):
        return jsonify({"error": msg}), code

    # ---- status ----

    @app.get("/api/status")
    def status():
        boards = []
        for b in store.boards():
            entry = dict(b)
            entry["status"] = dict(poller.status.get(b["id"], {}))
            entry["panels"] = list(poller.panels.get(b["id"], []))
            entry["direct_cloud"] = bool(poller.status.get(b["id"], {}).get("direct_cloud"))
            boards.append(entry)
        return jsonify({
            "app": config.APP_NAME,
            "version": config.APP_VERSION,
            "settings": store.settings(),
            "boards": boards,
            "discovered": sorted(discovery.discovered.values(), key=lambda d: d["mac"]) if discovery else [],
            "rules": store.rules(),
            "events": events.as_list(100),
        })

    @app.post("/api/discovery/adopt")
    def adopt_discovered():
        if discovery is None:
            return err("discovery not running", 503)
        mac = (request.get_json(silent=True) or {}).get("mac", "")
        entry = discovery.adopt_lookup(mac)
        if not entry:
            return err("beacon not in discovery cache", 404)
        try:
            board = store.add_board({"name": entry.get("device") or "", "host": entry.get("ip"), "mac": mac})
        except ValueError as e:
            return err(str(e))
        discovery.adopt_done(mac)
        events.add("info", "adopted board: %s (%s at %s)" % (board.get("name") or "unknown", mac, board["host"]))
        return jsonify({"board": board})

    @app.get("/api/boards/<board_id>/values")
    def board_values(board_id):
        return jsonify({
            "values": poller.values.get(board_id, {}),
            "panels": poller.panels.get(board_id, []),
            "status": poller.status.get(board_id, {}),
        })

    # ---- boards ----

    @app.post("/api/boards")
    def add_board():
        try:
            board = store.add_board(request.get_json(silent=True) or {})
        except ValueError as e:
            return err(str(e))
        events.add("info", "board added: %s" % (board.get("name") or board["host"]))
        return jsonify({"board": board})

    @app.post("/api/boards/<board_id>")
    def update_board(board_id):
        try:
            board = store.update_board(board_id, request.get_json(silent=True) or {})
        except (ValueError, TypeError) as e:
            return err(str(e))
        if board is None:
            return err("board not found", 404)
        return jsonify({"board": board})

    @app.delete("/api/boards/<board_id>")
    def remove_board(board_id):
        if not store.remove_board(board_id):
            return err("board not found", 404)
        poller.forget(board_id)
        return jsonify({"ok": True})

    # ---- rules ----

    @app.post("/api/rules")
    def add_rule():
        try:
            rule = store.add_rule(request.get_json(silent=True) or {})
        except (ValueError, TypeError) as e:
            return err(str(e))
        return jsonify({"rule": rule})

    @app.post("/api/rules/<rule_id>")
    def update_rule(rule_id):
        try:
            rule = store.update_rule(rule_id, request.get_json(silent=True) or {})
        except (ValueError, TypeError) as e:
            return err(str(e))
        if rule is None:
            return err("rule not found", 404)
        return jsonify({"rule": rule})

    @app.delete("/api/rules/<rule_id>")
    def remove_rule(rule_id):
        if not store.remove_rule(rule_id):
            return err("rule not found", 404)
        return jsonify({"ok": True})

    # ---- settings ----

    @app.post("/api/settings")
    def update_settings():
        return jsonify({"settings": store.update_settings(request.get_json(silent=True) or {})})

    # ---- board-originated notifications ----

    @app.post("/api/telegram/send")
    def telegram_send():
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or data.get("message") or "").strip()
        if not text:
            return err("text required")

        settings = store.settings()
        if not str(settings.get("telegram_token") or "").strip():
            return err("telegram token not configured")
        chat_id = str(data.get("chat_id") or "").strip()
        if not (chat_id or str(settings.get("telegram_chat_id") or "").strip()):
            return err("telegram chat id not configured")

        who = str(data.get("device") or data.get("mac") or request.remote_addr or "").strip()
        panel = str(data.get("panel") or "").strip()

        if notifier is None:
            ok, detail = telegram.send(
                settings.get("telegram_token", ""),
                chat_id or settings.get("telegram_chat_id", ""),
                text,
            )
            events.add("telegram" if ok else "error", "push %s/%s: %s" % (who or "board", panel or "?", detail))
            return jsonify({"ok": bool(ok), "detail": detail}), (200 if ok else 502)

        try:
            notifier.enqueue(text, chat_id=chat_id, who=who, panel=panel)
        except queue.Full:
            return err("notifier queue full", 503)
        return jsonify({"ok": True, "queued": True}), 202

    # ---- board logger upload (CSV push) ----

    def _board_for_ip(ip):
        ip = (ip or "").strip()
        if not ip:
            return None
        for b in store.boards():
            if (b.get("host") or "").strip() == ip:
                return b
        return None

    def _mariadb_cfg(settings):
        return {
            "host": settings.get("mariadb_host", "localhost"),
            "port": settings.get("mariadb_port", "3306"),
            "user": settings.get("mariadb_user", ""),
            "password": settings.get("mariadb_password", ""),
            "database": settings.get("mariadb_database", "Hardware"),
        }

    def _mariadb_configured(settings):
        c = _mariadb_cfg(settings)
        return bool((c.get("user") or "").strip() and (c.get("database") or "").strip())

    @app.post("/capturas/fileUpload.php")
    def receive_logger_upload():
        ip = request.args.get("ip", "")
        file_name = request.args.get("fileName", "")
        csv_text = request.get_data(cache=False, as_text=True) or ""
        board = _board_for_ip(ip)
        device_name = (board.get("name") if board else "") or ip or "device"
        table_base = file_name.rsplit(".", 1)[0] if file_name else (device_name or "device")

        if not csv_text.strip():
            return "no data", 400

        settings = store.settings()
        db_path = localdb.effective_path(settings.get("local_db_path", ""), config.BASE_DIR)
        backend = (board.get("backend") if board else "") or ""
        maria_ok = _mariadb_configured(settings)
        sqlite_ok = localdb.db_exists(db_path)

        # Destination: explicit backend, else default local store.
        use_mariadb = False
        use_sqlite = False
        if backend == "mariadb":
            use_mariadb = maria_ok
        elif backend == "local":
            use_sqlite = True
        elif backend == "":
            if maria_ok:
                use_mariadb = True
            elif sqlite_ok:
                use_sqlite = True
        else:
            # Cloud backend configured: CSV push forwarding not implemented;
            # fall back to a configured local store to avoid data loss.
            if maria_ok:
                use_mariadb = True
            elif sqlite_ok:
                use_sqlite = True

        if use_mariadb:
            try:
                rows, table = mariadb_log.store_csv(table_base, csv_text, _mariadb_cfg(settings))
                events.add("info", "%s: stored %d row(s) in MariaDB %s (%s)" % (device_name, rows, table, file_name or "csv"))
                return "SUCCESS...", 200
            except Exception as e:
                log.exception("MariaDB log store failed")
                events.add("error", "%s: MariaDB log store failed (%s)" % (device_name, e))
                return "store failed", 500

        if use_sqlite:
            try:
                rows, table = localdb.store_csv(table_base, csv_text, db_path)
                events.add("info", "%s: stored %d row(s) in SQLite %s (%s)" % (device_name, rows, table, file_name or "csv"))
                return "ok", 200
            except Exception as e:
                log.exception("local log store failed")
                events.add("error", "%s: local log store failed (%s)" % (device_name, e))
                return "store failed", 500

        events.add("info", "%s: log received but no local DB/backend configured" % device_name)
        return "ok", 200

    # ---- diagnostics ----

    @app.post("/api/test/telegram")
    def test_telegram():
        data = request.get_json(silent=True) or {}
        ok, detail = telegram.send(
            store.settings().get("telegram_token", ""),
            data.get("chat_id", ""),
            data.get("text") or "critter-bridge test message",
        )
        events.add("telegram" if ok else "error", "telegram test: %s" % detail)
        return jsonify({"ok": bool(ok), "detail": detail}), (200 if ok else 400)

    return app
