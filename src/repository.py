"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE TABLE IF NOT EXISTS fx_rates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    currency TEXT NOT NULL,
                    rate_date TEXT NOT NULL,
                    rate REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(currency, rate_date)
                );
                CREATE TABLE IF NOT EXISTS bills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    claim_number TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount_fc REAL NOT NULL,
                    rate REAL NOT NULL,
                    rate_date TEXT NOT NULL,
                    amount_cny REAL NOT NULL,
                    occupancy_cny REAL NOT NULL,
                    fx_variance_cny REAL NOT NULL,
                    payment_reference TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'settled',
                    snapshot TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_bills_record ON bills(record_id);
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, bill_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_items_bill ON batch_items(bill_id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], bill_seed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            bill_id = None
            if bill_seed is not None:
                cursor = connection.execute(
                    "INSERT INTO bills(record_id,claim_number,currency,amount_fc,rate,rate_date,amount_cny,occupancy_cny,fx_variance_cny,payment_reference,status,snapshot,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record_id,
                        bill_seed["claim_number"],
                        bill_seed["currency"],
                        bill_seed["amount_fc"],
                        bill_seed["rate"],
                        bill_seed["rate_date"],
                        bill_seed["amount_cny"],
                        bill_seed["occupancy_cny"],
                        bill_seed["fx_variance_cny"],
                        bill_seed["payment_reference"],
                        "settled",
                        json.dumps(bill_seed["snapshot"], ensure_ascii=False, sort_keys=True),
                        actor_id,
                        now,
                    ),
                )
                bill_id = int(cursor.lastrowid)
                payload = dict(payload)
                payload["bill_id"] = bill_id
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 汇率 ----

    def upsert_fx_rate(self, currency: str, rate_date: str, rate: float, source: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO fx_rates(currency,rate_date,rate,source,created_by,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(currency, rate_date) DO UPDATE SET rate=excluded.rate, source=excluded.source",
                (currency, rate_date, rate, source, actor_id, now),
            )
            row = connection.execute("SELECT * FROM fx_rates WHERE currency=? AND rate_date=?", (currency, rate_date)).fetchone()
        return dict(row)

    def get_fx_rate(self, currency: str, rate_date: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM fx_rates WHERE currency=? AND rate_date=?", (currency, rate_date)).fetchone()
        return dict(row) if row else None

    def list_fx_rates(self, currency: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if currency:
                rows = connection.execute("SELECT * FROM fx_rates WHERE currency=? ORDER BY rate_date DESC LIMIT ?", (currency, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM fx_rates ORDER BY currency, rate_date DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # ---- 账单 ----

    def get_bill(self, bill_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
        if row is None:
            raise NotFound("账单不存在")
        return self._bill_row(row)

    def get_bill_by_record(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM bills WHERE record_id=?", (record_id,)).fetchone()
        return self._bill_row(row) if row else None

    @staticmethod
    def _bill_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["snapshot"] = json.loads(item["snapshot"])
        return item

    # ---- 批次 ----

    def create_batch(self, reference: str, title: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO batches(reference,title,status,created_by,created_at) VALUES(?,?,?,?,?)",
                    (reference, title, "open", actor_id, now),
                )
                row = connection.execute("SELECT * FROM batches WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次号已存在") from exc
        return dict(row)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return dict(row)

    def list_batches(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def add_batch_item(self, batch_id: int, bill_id: int) -> None:
        with self._connect() as connection:
            if connection.execute("SELECT id FROM batches WHERE id=?", (batch_id,)).fetchone() is None:
                raise NotFound("批次不存在")
            if connection.execute("SELECT id FROM bills WHERE id=?", (bill_id,)).fetchone() is None:
                raise NotFound("账单不存在")
            try:
                connection.execute(
                    "INSERT INTO batch_items(batch_id,bill_id,created_at) VALUES(?,?,?)",
                    (batch_id, bill_id, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("账单已在批次中") from exc

    def batch_items(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT b.*, i.id AS item_id, i.created_at AS batched_at FROM batch_items i "
                "JOIN bills b ON b.id = i.bill_id WHERE i.batch_id=? ORDER BY i.id",
                (batch_id,),
            ).fetchall()
        return [self._bill_row(row) for row in rows]

    def bill_batch_id(self, bill_id: int) -> Optional[int]:
        with self._connect() as connection:
            row = connection.execute("SELECT batch_id FROM batch_items WHERE bill_id=?", (bill_id,)).fetchone()
        return int(row["batch_id"]) if row else None
