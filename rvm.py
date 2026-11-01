"""Local, single-machine RVM prototype. Records a simulated purchase value; does not transfer money."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
import hashlib
import re
import unicodedata
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from detection import assess_prediction


DEFAULT_SETTINGS = {"price_per_bottle": 100, "brand_prices": {}}
CANDIDATE_TTL = 60

# Read-only browsing views: one row per transaction, plus one row per brand.
# Aggregate deposits before joining, so multiple brands never multiply totals.
TRANSACTION_VIEWS_SQL = """
    CREATE VIEW IF NOT EXISTS rincian_transaksi AS
    SELECT
        session_id AS transaksi_id,
        MIN(TRIM(label)) AS merek,
        COUNT(*) AS jumlah_botol,
        CASE WHEN MIN(amount_rupiah) = MAX(amount_rupiah)
            THEN MIN(amount_rupiah) ELSE NULL END AS tarif_per_botol_rupiah,
        SUM(amount_rupiah) AS subtotal_rupiah
    FROM deposits
    GROUP BY session_id, TRIM(label) COLLATE NOCASE;

    CREATE VIEW IF NOT EXISTS transaksi AS
    WITH totals AS (
        SELECT session_id, COUNT(*) AS jumlah_botol,
            SUM(amount_rupiah) AS total_rupiah
        FROM deposits GROUP BY session_id
    ), brands AS (
        SELECT transaksi_id, COUNT(*) AS jumlah_jenis,
            GROUP_CONCAT(merek || ' x' || jumlah_botol, '; ') AS rincian_merek
        FROM (SELECT * FROM rincian_transaksi
            ORDER BY transaksi_id, merek COLLATE NOCASE)
        GROUP BY transaksi_id
    )
    SELECT
        s.id AS transaksi_id,
        DATETIME(s.started_at, '+7 hours') AS mulai_wib,
        DATETIME(s.finished_at, '+7 hours') AS selesai_wib,
        CASE s.status WHEN 'completed' THEN 'selesai'
            ELSE 'berlangsung' END AS status,
        COALESCE(t.jumlah_botol, 0) AS jumlah_botol,
        COALESCE(b.jumlah_jenis, 0) AS jumlah_jenis,
        COALESCE(t.total_rupiah, 0) AS total_rupiah,
        COALESCE(b.rincian_merek, 'Belum ada botol') AS rincian_merek
    FROM sessions AS s
    LEFT JOIN totals AS t ON t.session_id = s.id
    LEFT JOIN brands AS b ON b.transaksi_id = s.id;
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize_label(label):
    return label.strip().casefold()


def price_key(label):
    plain = unicodedata.normalize("NFKD", label).casefold()
    compact = re.sub(r"[^a-z0-9]", "", plain)
    return {"aqua": "aqua", "nestlepurelife": "nestle pure life", "purelife": "nestle pure life",
        "leminerale": "le minerale"}.get(compact, compact)


def pricing_config(config):
    return {"price_per_bottle": config["price_per_bottle"],
        "brand_prices": {price_key(label): price for label, price in config.get("brand_prices", {}).items()}}


def bottle_price(config, label):
    return config.get("brand_prices", {}).get(price_key(label), config["price_per_bottle"])


class RVMStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id=1), value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('active','completed')),
                    config TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'scanning',
                    version INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0,
                    last_label TEXT, last_seen REAL, candidate_id TEXT,
                    candidate_label TEXT, candidate_confidence REAL, expires_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_session
                    ON sessions(status) WHERE status='active';
                CREATE TABLE IF NOT EXISTS deposits (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    label TEXT NOT NULL, confidence REAL NOT NULL,
                    amount_rupiah INTEGER NOT NULL CHECK(amount_rupiah>=0), accepted_at TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source='manual_simulation')
                );
                CREATE TABLE IF NOT EXISTS value_ledger (
                    id INTEGER PRIMARY KEY, deposit_id TEXT UNIQUE NOT NULL REFERENCES deposits(id),
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    amount INTEGER NOT NULL CHECK(amount>=0), created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS deposits_session ON deposits(session_id);
                CREATE INDEX IF NOT EXISTS ledger_session ON value_ledger(session_id);
                CREATE TABLE IF NOT EXISTS transaction_changes (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('create','update','delete')),
                    before_json TEXT, after_json TEXT, changed_at TEXT NOT NULL
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(deposits)")}
            if "entry_method" not in columns:
                db.execute("ALTER TABLE deposits ADD COLUMN entry_method TEXT NOT NULL DEFAULT 'camera'")
            db.executescript(TRANSACTION_VIEWS_SQL)
            db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (json.dumps(DEFAULT_SETTINGS),))
            # Retire old acceptance rules while preserving the tariff and all sessions.
            config = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
            db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(pricing_config(config)),))
            for row in db.execute("SELECT * FROM sessions WHERE status='active'").fetchall():
                self._expire(db, row)

    def create_transaction_views(self):
        """Add browsing views to a running database without touching session state."""
        with self.connection() as db:
            db.executescript(TRANSACTION_VIEWS_SQL)

    def settings(self):
        with self.connection() as db:
            return pricing_config(json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0]))

    def update_settings(self, config):
        with self.connection(True) as db:
            previous = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
            config = pricing_config({**previous, **config})
            db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(config),))
        return config

    def _session(self, db, session_id):
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Sesi tidak ditemukan.")
        return row

    def _active(self, db, session_id):
        row = self._session(db, session_id)
        if row["status"] != "active":
            raise HTTPException(409, "Sesi sudah selesai. Mulai sesi baru.")
        return row

    def _clear(self, db, session_id, phase="scanning"):
        db.execute("""UPDATE sessions SET phase=?, version=version+1, streak=0,
            last_label=NULL, last_seen=NULL, candidate_id=NULL, candidate_label=NULL,
            candidate_confidence=NULL, expires_at=NULL WHERE id=?""", (phase, session_id))

    def _expire(self, db, row):
        candidate = {"label": row["candidate_label"], "confidence": row["candidate_confidence"]}
        if row["phase"] == "ready" and (row["expires_at"] <= time.time() or not assess_prediction(candidate)["bottle_detected"]):
            self._clear(db, row["id"])
            return self._session(db, row["id"])
        return row

    def _snapshot(self, db, row, details=True):
        row = self._expire(db, row)
        totals = db.execute("SELECT COUNT(*) bottles, COALESCE(SUM(amount_rupiah),0) total_rupiah FROM deposits WHERE session_id=?", (row["id"],)).fetchone()
        result = {key: row[key] for key in ("id", "started_at", "finished_at", "status", "phase", "streak", "version")}
        result.update(dict(totals))
        result["config"] = json.loads(row["config"])
        result["candidate"] = ({"id": row["candidate_id"], "label": row["candidate_label"],
            "confidence": row["candidate_confidence"], "expires_at": row["expires_at"],
            "price_per_bottle": bottle_price(result["config"], row["candidate_label"])} if row["candidate_id"] else None)
        if details:
            result["deposits"] = [dict(item) for item in db.execute("SELECT * FROM deposits WHERE session_id=? ORDER BY accepted_at DESC, id", (row["id"],))]
        return result

    def current(self):
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM sessions WHERE status='active'").fetchone()
            return self._snapshot(db, row) if row else None

    def start(self):
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM sessions WHERE status='active'").fetchone()
            if row:
                return self._snapshot(db, row)
            config = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
            session_id = str(uuid.uuid4())
            db.execute("INSERT INTO sessions(id,started_at,status,config) VALUES (?,?,'active',?)", (session_id, now(), json.dumps(config)))
            return self._snapshot(db, self._session(db, session_id))

    def get(self, session_id):
        with self.connection(True) as db:
            return self._snapshot(db, self._session(db, session_id))

    def scan_version(self, session_id):
        with self.connection(True) as db:
            row = self._expire(db, self._active(db, session_id))
            if row["phase"] != "scanning":
                raise HTTPException(409, "Konfirmasi botol atau tekan Botol berikutnya terlebih dahulu.")
            return row["version"]

    def observe(self, session_id, version, prediction):
        # Only server-side Roboflow results reach here; clients cannot submit a label/score.
        with self.connection(True) as db:
            row = self._active(db, session_id)
            if row["version"] != version or row["phase"] != "scanning":
                raise HTTPException(409, "Scan sudah kedaluwarsa. Ambil frame baru.")
            label = prediction.get("label", "") if isinstance(prediction, dict) else ""
            confidence = prediction.get("confidence", 0) if isinstance(prediction, dict) else 0
            accepted = assess_prediction(prediction)["bottle_detected"]
            if not accepted:
                self._clear(db, session_id)
            else:
                stamp = time.time()
                db.execute("""UPDATE sessions SET version=version+1, streak=?, last_label=?,
                    last_seen=?, phase=?, candidate_id=?, candidate_label=?, candidate_confidence=?,
                    expires_at=? WHERE id=?""", (1, normalize_label(label), stamp,
                    "ready", str(uuid.uuid4()), label.strip(), confidence,
                    stamp + CANDIDATE_TTL, session_id))
            result = self._snapshot(db, self._session(db, session_id), details=False)
            result["scan_accepted"] = accepted
            return result

    def reset_scan(self, session_id):
        with self.connection(True) as db:
            self._active(db, session_id)
            self._clear(db, session_id)
            return self._snapshot(db, self._session(db, session_id))

    def confirm(self, session_id, candidate_id):
        with self.connection(True) as db:
            row = self._session(db, session_id)
            previous = db.execute("SELECT 1 FROM deposits WHERE id=? AND session_id=?", (candidate_id, session_id)).fetchone()
            if previous:
                return {**self._snapshot(db, row), "already_recorded": True}
            self._active(db, session_id)
            if row["phase"] != "ready" or row["candidate_id"] != candidate_id or row["expires_at"] <= time.time():
                raise HTTPException(409, "Validasi botol tidak tersedia atau kedaluwarsa. Scan ulang.")
            if not assess_prediction({"label": row["candidate_label"], "confidence": row["candidate_confidence"]})["bottle_detected"]:
                raise HTTPException(409, "Botol tidak terdeteksi. Confidence belum memenuhi batas. Scan ulang.")
            amount_rupiah = bottle_price(json.loads(row["config"]), row["candidate_label"])
            stamp = now()
            db.execute("INSERT INTO deposits(id,session_id,label,confidence,amount_rupiah,accepted_at,source) VALUES (?,?,?,?,?,?,?)", (candidate_id, session_id,
                row["candidate_label"], row["candidate_confidence"], amount_rupiah, stamp, "manual_simulation"))
            db.execute("INSERT INTO value_ledger(deposit_id,session_id,amount,created_at) VALUES (?,?,?,?)", (candidate_id, session_id, amount_rupiah, stamp))
            self._clear(db, session_id, "await_next")
            return {**self._snapshot(db, self._session(db, session_id)), "already_recorded": False}

    def finish(self, session_id):
        with self.connection(True) as db:
            row = self._session(db, session_id)
            if row["status"] == "active":
                self._clear(db, session_id, "completed")
                db.execute("UPDATE sessions SET status='completed',finished_at=? WHERE id=?", (now(), session_id))
            return self._snapshot(db, self._session(db, session_id))

    def history(self, limit=20, offset=0):
        with self.connection(True) as db:
            rows = db.execute("SELECT * FROM sessions WHERE status='completed' ORDER BY started_at DESC, id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return {"items": [self._snapshot(db, row, details=False) for row in rows],
                "total": db.execute("SELECT COUNT(*) FROM sessions WHERE status='completed'").fetchone()[0]}

    def summary(self):
        with self.connection() as db:
            return {"bottles": db.execute("SELECT COUNT(*) FROM deposits").fetchone()[0],
                "total_rupiah": db.execute("SELECT COALESCE(SUM(amount),0) FROM value_ledger").fetchone()[0],
                "completed_sessions": db.execute("SELECT COUNT(*) FROM sessions WHERE status='completed'").fetchone()[0]}

    def _editable(self, db, session_id, version):
        row = self._session(db, session_id)
        if row["status"] != "completed":
            raise HTTPException(409, "Selesaikan sesi aktif sebelum mengubah atau menghapus transaksi ini.")
        if row["version"] != version:
            raise HTTPException(409, "Transaksi sudah berubah. Tutup formulir lalu muat ulang data terbaru.")
        return row

    def _audit(self, db, session_id, action, before=None, after=None):
        db.execute("""INSERT INTO transaction_changes(session_id,action,before_json,after_json,changed_at)
            VALUES (?,?,?,?,?)""", (session_id, action,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None, now()))

    def _write_items(self, db, session_id, items):
        # Retain original IDs/confidence for unchanged bottles; only additions are manual.
        buckets = defaultdict(deque)
        for row in db.execute("SELECT * FROM deposits WHERE session_id=? ORDER BY accepted_at,id", (session_id,)):
            buckets[(row["label"], row["amount_rupiah"])].append(row["id"])
        for item in items:
            label, price = item["label"], item["price_per_bottle"]
            for _ in range(item["quantity"]):
                if buckets[(label, price)]:
                    buckets[(label, price)].popleft()
                    continue
                deposit_id, stamp = str(uuid.uuid4()), now()
                db.execute("""INSERT INTO deposits(id,session_id,label,confidence,amount_rupiah,
                    accepted_at,source,entry_method) VALUES (?,?,?,0,?,?,'manual_simulation','manual')""",
                    (deposit_id, session_id, label, price, stamp))
                db.execute("INSERT INTO value_ledger(deposit_id,session_id,amount,created_at) VALUES (?,?,?,?)",
                    (deposit_id, session_id, price, stamp))
        for remaining in buckets.values():
            for deposit_id in remaining:
                db.execute("DELETE FROM value_ledger WHERE deposit_id=?", (deposit_id,))
                db.execute("DELETE FROM deposits WHERE id=?", (deposit_id,))

    def create_transaction(self, items, request_id=None):
        with self.connection(True) as db:
            session_id, stamp = request_id or str(uuid.uuid4()), now()
            signature = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
            existing = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if existing:
                config = json.loads(existing["config"])
                if config.get("manual_request_hash") != signature:
                    raise HTTPException(409, "ID permintaan sudah digunakan. Muat ulang data sebelum melanjutkan.")
                return self._snapshot(db, existing)
            if db.execute("SELECT 1 FROM transaction_changes WHERE session_id=?", (session_id,)).fetchone():
                raise HTTPException(409, "Transaksi untuk permintaan ini sudah dihapus. Buka formulir baru.")
            config = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
            config["entry_mode"] = "manual"
            config["manual_request_hash"] = signature
            db.execute("""INSERT INTO sessions(id,started_at,finished_at,status,phase,config)
                VALUES (?,?,?,'completed','completed',?)""", (session_id, stamp, stamp, json.dumps(config)))
            self._write_items(db, session_id, items)
            result = self._snapshot(db, self._session(db, session_id))
            self._audit(db, session_id, "create", after=result)
            return result

    def update_transaction(self, session_id, version, items):
        with self.connection(True) as db:
            row = self._editable(db, session_id, version)
            before = self._snapshot(db, row)
            self._write_items(db, session_id, items)
            db.execute("UPDATE sessions SET version=version+1 WHERE id=?", (session_id,))
            result = self._snapshot(db, self._session(db, session_id))
            self._audit(db, session_id, "update", before, result)
            return result

    def delete_transaction(self, session_id, version):
        with self.connection(True) as db:
            row = self._editable(db, session_id, version)
            before = self._snapshot(db, row)
            self._audit(db, session_id, "delete", before=before)
            db.execute("DELETE FROM value_ledger WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM deposits WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return {"deleted": True, "id": session_id}
