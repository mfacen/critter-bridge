import csv
import io
import logging
import re
import threading

log = logging.getLogger(__name__)

_lock = threading.RLock()


def _safe_ident(name, fallback="t"):
    name = (name or "").strip()
    out = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not out:
        out = fallback
    if out[0].isdigit():
        out = "_" + out
    return out


def _connect(cfg):
    try:
        import pymysql
    except Exception as e:
        raise RuntimeError("pymysql not installed: %s" % e)

    host = (cfg.get("host") or "").strip() or "localhost"
    port = int(str(cfg.get("port") or "3306").strip() or "3306")
    user = (cfg.get("user") or "").strip()
    password = (cfg.get("password") or "").strip()
    db = (cfg.get("database") or cfg.get("db") or "").strip()
    if not user or not db:
        raise ValueError("MariaDB user/database missing")
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db,
        charset="utf8mb4",
        connect_timeout=8,
        autocommit=False,
    )


def _ensure_columns(cur, table, columns):
    # columns: list of (name, is_time)
    cur.execute("CREATE TABLE IF NOT EXISTS `%s` (`Time` INT(11) PRIMARY KEY)" % table)
    cur.execute("SHOW COLUMNS FROM `%s`" % table)
    existing = {row[0] for row in cur.fetchall()}
    for name, is_time in columns:
        if is_time or name == "Time":
            continue
        if name not in existing:
            cur.execute("ALTER TABLE `%s` ADD COLUMN `%s` FLOAT" % (table, name))
            existing.add(name)


def store_csv(table_base, csv_text, cfg):
    """Store logger CSV into MariaDB using schema compatible with legacy fileUpload.php.
    Returns (rows_inserted, table_name)."""
    if not csv_text or not csv_text.strip():
        return 0, _safe_ident(table_base or "device")

    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration:
        return 0, _safe_ident(table_base or "device")
    if not header:
        return 0, _safe_ident(table_base or "device")

    cols = []
    for i, h in enumerate(header):
        h = (h or "").strip()
        if i == 0:
            cols.append(("Time", True))
        else:
            cols.append((_safe_ident(h, "c%d" % i), False))

    # de-dup column names (keep Time unique)
    seen = {}
    uniq = []
    for name, is_time in cols:
        n = seen.get(name, 0)
        if n and not is_time:
            uniq.append(("%s_%d" % (name, n + 1), is_time))
        else:
            uniq.append((name, is_time))
        seen[name] = n + 1
    cols = uniq

    table = _safe_ident(table_base or "device")

    rows = []
    col_names = [c[0] for c in cols]
    for row in reader:
        if not row or all((str(x).strip() == "") for x in row):
            continue
        vals = []
        for i, (name, is_time) in enumerate(cols):
            raw = row[i].strip() if i < len(row) and row[i] is not None else ""
            if is_time:
                try:
                    vals.append(int(float(raw))) if raw != "" else vals.append(None)
                except Exception:
                    vals.append(None)
            else:
                try:
                    vals.append(float(raw)) if raw != "" else vals.append(None)
                except Exception:
                    vals.append(None)
        if vals[0] is None:
            continue
        rows.append(tuple(vals))

    with _lock:
        conn = _connect(cfg)
        try:
            cur = conn.cursor()
            _ensure_columns(cur, table, cols)
            inserted = 0
            if rows:
                placeholders = ", ".join(["%s"] * len(col_names))
                colsql = ", ".join("`%s`" % c for c in col_names)
                sql = "INSERT IGNORE INTO `%s` (%s) VALUES (%s)" % (table, colsql, placeholders)
                cur.executemany(sql, rows)
                inserted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)
            conn.commit()
            return inserted, table
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
