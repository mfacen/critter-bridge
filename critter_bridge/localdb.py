import csv
import io
import logging
import os
import re
import sqlite3
import threading

log = logging.getLogger(__name__)

_lock = threading.RLock()


def effective_path(configured, base_dir):
    path = (configured or "").strip()
    if path:
        return os.path.expanduser(path)
    return os.path.join(base_dir, "critter.db")


def db_exists(path):
    try:
        return bool(path) and os.path.exists(path)
    except Exception:
        return False


def _safe_ident(name, fallback="t"):
    name = (name or "").strip()
    out = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not out:
        out = fallback
    if out[0].isdigit():
        out = "_" + out
    return out


def _connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return conn


def _ensure_table(conn, table, columns):
    cols_sql = ", ".join('"%s" TEXT' % c for c in columns)
    conn.execute('CREATE TABLE IF NOT EXISTS "%s" (%s)' % (table, cols_sql))
    cur = conn.execute('PRAGMA table_info("%s")' % table)
    existing = {row[1] for row in cur.fetchall()}
    for col in columns:
        if col not in existing:
            conn.execute('ALTER TABLE "%s" ADD COLUMN "%s" TEXT' % (table, col))
            existing.add(col)


def store_csv(device_name, csv_text, db_path):
    """Parse board logger CSV and store rows in a per-device wide table.
    Returns (rows_inserted, table_name) or raises."""
    if not csv_text or not csv_text.strip():
        return 0, _safe_ident(device_name or "device")

    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration:
        return 0, _safe_ident(device_name or "device")

    if not header:
        return 0, _safe_ident(device_name or "device")

    # Normalize header: first column is timestamp
    cols = []
    for i, h in enumerate(header):
        h = (h or "").strip()
        if i == 0:
            cols.append("ts")
        else:
            cols.append(_safe_ident(h, "c%d" % i))

    # Avoid duplicate column names
    seen = {}
    uniq_cols = []
    for c in cols:
        n = seen.get(c, 0)
        if n:
            uniq_cols.append("%s_%d" % (c, n + 1))
        else:
            uniq_cols.append(c)
        seen[c] = n + 1
    cols = uniq_cols

    table = _safe_ident(device_name or "device")
    rows = []
    for row in reader:
        if not row or all((str(x).strip() == "") for x in row):
            continue
        vals = []
        for i in range(len(cols)):
            vals.append((row[i].strip() if i < len(row) and row[i] is not None else ""))
        if not vals[0]:
            continue
        rows.append(vals)

    with _lock:
        conn = _connect(db_path)
        try:
            _ensure_table(conn, table, cols)
            if rows:
                placeholders = ", ".join(["?"] * len(cols))
                colsql = ", ".join('"%s"' % c for c in cols)
                conn.executemany(
                    'INSERT INTO "%s" (%s) VALUES (%s)' % (table, colsql, placeholders),
                    rows,
                )
            conn.commit()
        finally:
            conn.close()

    return len(rows), table
