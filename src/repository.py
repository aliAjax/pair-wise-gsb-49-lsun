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
                CREATE TABLE IF NOT EXISTS fx_rates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    currency TEXT NOT NULL,
                    rate_date TEXT NOT NULL,
                    rate TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(currency, rate_date)
                );
                CREATE TABLE IF NOT EXISTS settlement_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    currency TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES settlement_batches(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    currency TEXT NOT NULL,
                    foreign_amount REAL NOT NULL,
                    cny_amount REAL NOT NULL,
                    rate_date TEXT NOT NULL,
                    rate TEXT NOT NULL,
                    bill_snapshot TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    UNIQUE(batch_id, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_fx_lookup ON fx_rates(currency, rate_date);
                CREATE INDEX IF NOT EXISTS idx_batch_items_record ON batch_items(record_id);
                CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items(batch_id);
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

    def list_records(self, state: Optional[str] = None, limit: int = 100, event_id: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = []
        params: List[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if event_id:
            clauses.append("json_extract(payload, '$.event_id')=? OR json_extract(payload, '$.claim_event_id')=?")
            params.extend([event_id, event_id])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT * FROM records" + where + " ORDER BY id DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(sql, (*params, limit)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
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

    # ---- 汇率资料 ----
    def upsert_fx_rate(self, currency: str, rate_date: str, rate: str, source: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO fx_rates(currency,rate_date,rate,source,created_by,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(currency,rate_date) DO UPDATE SET rate=excluded.rate, source=excluded.source, updated_at=excluded.updated_at
                """,
                (currency, rate_date, rate, source, actor_id, now, now),
            )
            row = connection.execute("SELECT * FROM fx_rates WHERE currency=? AND rate_date=?", (currency, rate_date)).fetchone()
        return dict(row)

    def get_fx_rate(self, currency: str, rate_date: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM fx_rates WHERE currency=? AND rate_date<=? ORDER BY rate_date DESC, id DESC LIMIT 1",
                (currency, rate_date),
            ).fetchone()
        return dict(row) if row else None

    def list_fx_rates(self, currency: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            if currency:
                rows = connection.execute(
                    "SELECT * FROM fx_rates WHERE currency=? ORDER BY rate_date DESC, id DESC LIMIT ?", (currency, limit)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM fx_rates ORDER BY rate_date DESC, currency LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    # ---- 结算批次 ----
    def create_batch(self, reference: str, currency: str, note: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO settlement_batches(reference,currency,note,created_by,created_at) VALUES(?,?,?,?,?)",
                    (reference, currency, note, actor_id, now),
                )
                batch_id = int(cursor.lastrowid)
                row = connection.execute("SELECT * FROM settlement_batches WHERE id=?", (batch_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次reference已存在") from exc
        return dict(row)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM settlement_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return dict(row)

    def list_batches(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM settlement_batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def add_batch_item(self, batch_id: int, record_id: int) -> Dict[str, Any]:
        record = self.get(record_id)
        if record["state"] != "settled":
            raise Conflict("只有已结算案件才能加入批次")
        batch = self.get_batch(batch_id)
        p = record["payload"]
        ccy = p.get("loss_currency", "CNY")
        if ccy != batch["currency"]:
            raise Conflict("案件币种%s与批次币种%s不一致" % (ccy, batch["currency"]))
        bill = p.get("bill") or {}
        snapshot = json.dumps(bill, ensure_ascii=False, sort_keys=True)
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO batch_items(batch_id,record_id,currency,foreign_amount,cny_amount,rate_date,rate,bill_snapshot,added_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        batch_id, record_id, ccy,
                        bill.get("payment_amount_foreign", p.get("payment_amount_foreign", 0)),
                        bill.get("payment_amount_cny", p.get("payment_amount_cny", 0)),
                        (p.get("settlement_fx") or {}).get("rate_date", ""),
                        str((p.get("settlement_fx") or {}).get("rate", "1")),
                        snapshot, now,
                    ),
                )
                item_id = int(cursor.lastrowid)
                row = connection.execute("SELECT * FROM batch_items WHERE id=?", (item_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("该案件已在此批次中") from exc
        item = dict(row)
        item["bill_snapshot"] = json.loads(item["bill_snapshot"])
        return item

    def list_batch_items(self, batch_id: int) -> List[Dict[str, Any]]:
        self.get_batch(batch_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT i.*, r.reference AS record_reference
                FROM batch_items i JOIN records r ON r.id = i.record_id
                WHERE i.batch_id=? ORDER BY i.id
                """,
                (batch_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["bill_snapshot"] = json.loads(item["bill_snapshot"])
            result.append(item)
        return result

    def batches_for_record(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT b.*, i.foreign_amount, i.cny_amount, i.rate_date, i.rate
                FROM batch_items i JOIN settlement_batches b ON b.id = i.batch_id
                WHERE i.record_id=? ORDER BY b.id
                """,
                (record_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
