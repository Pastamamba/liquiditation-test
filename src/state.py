"""SQLite state store with async writer-queue pattern.

Yksi shared `aiosqlite`-yhteys per processi, WAL mode päällä. Kaikki kirjoitukset
menevät `asyncio.Queue`:n läpi yhdelle kuluttaja-taskille — näin vältetään
race-conditioneja kun monta event-handleriä kirjaa saman fillin/orderin yhtä
aikaa. Lukemat menevät suoraan yhteyden kautta.

Rahasummat tallennetaan TEXT-kenttiin Decimal-stringinä — ei floateja.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import aiosqlite

OrderSide = Literal["bid", "ask"]
OrderStatus = Literal["placed", "filled", "cancelled", "rejected"]
EventLevel = Literal["info", "warning", "error", "kill"]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FillRecord:
    id: str
    timestamp_ms: int
    symbol: str
    side: OrderSide
    price: Decimal
    size: Decimal
    fee: Decimal
    order_id: str
    is_maker: bool


@dataclass(frozen=True)
class OrderRecord:
    id: str
    client_order_id: str | None
    timestamp_ms: int
    symbol: str
    side: OrderSide
    price: Decimal
    size: Decimal
    status: OrderStatus
    cancel_timestamp_ms: int | None = None


@dataclass(frozen=True)
class PnLSnapshot:
    timestamp_ms: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    inventory: Decimal
    capital: Decimal
    spread_pnl: Decimal
    rebate_earned: Decimal


@dataclass(frozen=True)
class EventRecord:
    timestamp_ms: int
    level: EventLevel
    component: str
    message: str
    data: dict[str, Any] | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    size TEXT NOT NULL,
    fee TEXT NOT NULL,
    order_id TEXT NOT NULL,
    is_maker INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_timestamp ON fills(timestamp);
CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_symbol ON fills(symbol);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    client_order_id TEXT,
    timestamp INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    size TEXT NOT NULL,
    status TEXT NOT NULL,
    cancel_timestamp INTEGER
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_timestamp ON orders(timestamp);
CREATE INDEX IF NOT EXISTS idx_orders_client_order_id ON orders(client_order_id);

CREATE TABLE IF NOT EXISTS pnl_snapshots (
    timestamp INTEGER PRIMARY KEY,
    realized_pnl TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    inventory TEXT NOT NULL,
    capital TEXT NOT NULL,
    spread_pnl TEXT NOT NULL,
    rebate_earned TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_level ON events(level);
"""


# Sentineli writer-loopin pysäyttämiseen.
_WriteItem = tuple[str, tuple[Any, ...]]


class StateStore:
    """Async SQLite-tallennus writer-queue-patternilla.

    Käyttö:
        store = StateStore("./data/mm_bot.db")
        await store.open()
        await store.migrate()
        await store.record_fill(fill)
        await store.flush()      # odota että queue on tyhjä
        fills = await store.get_recent_fills()
        await store.close()

    Tai async context managerina:
        async with StateStore("./data/mm_bot.db") as store:
            await store.record_fill(fill)
    """

    def __init__(self, db_path: Path | str, *, queue_maxsize: int = 1000) -> None:
        self.db_path = Path(db_path)
        self._queue: asyncio.Queue[_WriteItem | None] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._conn: aiosqlite.Connection | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._closed = False

    async def open(self) -> None:
        """Avaa connection, konfiguroi WAL, käynnistä writer-task. Idempotent."""
        if self._conn is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.db_path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.commit()
        self._conn = conn
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name="state-writer"
        )

    async def migrate(self) -> None:
        """Luo taulut ja indeksit jos eivät ole olemassa."""
        if self._conn is None:
            await self.open()
        assert self._conn is not None
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def _writer_loop(self) -> None:
        """Background-task: kuluta queue ja kirjoita DB:hen serial-järjestyksessä."""
        assert self._conn is not None
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                sql, params = item
                await self._conn.execute(sql, params)
                await self._conn.commit()
            except Exception:
                _logger.exception("StateStore writer error: sql=%r", item)
            finally:
                self._queue.task_done()

    async def _enqueue(self, sql: str, params: tuple[Any, ...]) -> None:
        if self._closed:
            raise RuntimeError("StateStore is closed")
        if self._writer_task is None:
            raise RuntimeError("StateStore not open — call open() first")
        await self._queue.put((sql, params))

    async def record_fill(self, fill: FillRecord) -> None:
        """Queue insert (or replace) a fill. Returns when enqueued, not after write."""
        await self._enqueue(
            "INSERT OR REPLACE INTO fills "
            "(id, timestamp, symbol, side, price, size, fee, order_id, is_maker) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fill.id,
                fill.timestamp_ms,
                fill.symbol,
                fill.side,
                str(fill.price),
                str(fill.size),
                str(fill.fee),
                fill.order_id,
                1 if fill.is_maker else 0,
            ),
        )

    async def record_order(self, order: OrderRecord) -> None:
        """Queue an order upsert."""
        await self._enqueue(
            "INSERT OR REPLACE INTO orders "
            "(id, client_order_id, timestamp, symbol, side, price, size, status, "
            "cancel_timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order.id,
                order.client_order_id,
                order.timestamp_ms,
                order.symbol,
                order.side,
                str(order.price),
                str(order.size),
                order.status,
                order.cancel_timestamp_ms,
            ),
        )

    async def record_pnl_snapshot(self, snap: PnLSnapshot) -> None:
        """Queue a PnL snapshot."""
        await self._enqueue(
            "INSERT OR REPLACE INTO pnl_snapshots "
            "(timestamp, realized_pnl, unrealized_pnl, inventory, capital, "
            "spread_pnl, rebate_earned) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                snap.timestamp_ms,
                str(snap.realized_pnl),
                str(snap.unrealized_pnl),
                str(snap.inventory),
                str(snap.capital),
                str(snap.spread_pnl),
                str(snap.rebate_earned),
            ),
        )

    async def record_event(self, event: EventRecord) -> None:
        """Queue an event log row."""
        data_json = json.dumps(event.data) if event.data is not None else None
        await self._enqueue(
            "INSERT INTO events "
            "(timestamp, level, component, message, data_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                event.timestamp_ms,
                event.level,
                event.component,
                event.message,
                data_json,
            ),
        )

    async def flush(self) -> None:
        """Odota että kaikki queueatut kirjoitukset on käsitelty."""
        await self._queue.join()

    async def get_recent_fills(
        self, *, symbol: str | None = None, limit: int = 100
    ) -> list[FillRecord]:
        """Hae viimeisimmät fillit (uusin ensin)."""
        if self._conn is None:
            raise RuntimeError("StateStore not open")
        sql = (
            "SELECT id, timestamp, symbol, side, price, size, fee, order_id, is_maker "
            "FROM fills"
        )
        params: list[Any] = []
        if symbol is not None:
            sql += " WHERE symbol = ?"
            params.append(symbol)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            FillRecord(
                id=row[0],
                timestamp_ms=row[1],
                symbol=row[2],
                side=row[3],
                price=Decimal(row[4]),
                size=Decimal(row[5]),
                fee=Decimal(row[6]),
                order_id=row[7],
                is_maker=bool(row[8]),
            )
            for row in rows
        ]

    async def get_open_orders_db(
        self, *, symbol: str | None = None
    ) -> list[OrderRecord]:
        """Hae orderit joiden status == 'placed' (ei filled/cancelled/rejected)."""
        if self._conn is None:
            raise RuntimeError("StateStore not open")
        sql = (
            "SELECT id, client_order_id, timestamp, symbol, side, price, size, "
            "status, cancel_timestamp FROM orders WHERE status = 'placed'"
        )
        params: list[Any] = []
        if symbol is not None:
            sql += " AND symbol = ?"
            params.append(symbol)
        sql += " ORDER BY timestamp DESC"
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            OrderRecord(
                id=row[0],
                client_order_id=row[1],
                timestamp_ms=row[2],
                symbol=row[3],
                side=row[4],
                price=Decimal(row[5]),
                size=Decimal(row[6]),
                status=row[7],
                cancel_timestamp_ms=row[8],
            )
            for row in rows
        ]

    async def get_pnl_history(
        self, *, since_ms: int | None = None, limit: int = 1000
    ) -> list[PnLSnapshot]:
        """Hae PnL-snapshotit (uusin ensin)."""
        if self._conn is None:
            raise RuntimeError("StateStore not open")
        sql = (
            "SELECT timestamp, realized_pnl, unrealized_pnl, inventory, capital, "
            "spread_pnl, rebate_earned FROM pnl_snapshots"
        )
        params: list[Any] = []
        if since_ms is not None:
            sql += " WHERE timestamp >= ?"
            params.append(since_ms)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            PnLSnapshot(
                timestamp_ms=row[0],
                realized_pnl=Decimal(row[1]),
                unrealized_pnl=Decimal(row[2]),
                inventory=Decimal(row[3]),
                capital=Decimal(row[4]),
                spread_pnl=Decimal(row[5]),
                rebate_earned=Decimal(row[6]),
            )
            for row in rows
        ]

    async def get_recent_events(
        self,
        *,
        level: EventLevel | None = None,
        since_ms: int | None = None,
        limit: int = 200,
    ) -> list[EventRecord]:
        """Hae event-logirivejä (uusin ensin)."""
        if self._conn is None:
            raise RuntimeError("StateStore not open")
        sql = (
            "SELECT timestamp, level, component, message, data_json FROM events"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if level is not None:
            clauses.append("level = ?")
            params.append(level)
        if since_ms is not None:
            clauses.append("timestamp >= ?")
            params.append(since_ms)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            EventRecord(
                timestamp_ms=row[0],
                level=row[1],
                component=row[2],
                message=row[3],
                data=json.loads(row[4]) if row[4] is not None else None,
            )
            for row in rows
        ]

    async def close(self) -> None:
        """Sulje yhteys siististi: flush queue, pysäytä writer, close conn."""
        if self._closed:
            return
        self._closed = True
        await self.flush()
        await self._queue.put(None)
        if self._writer_task is not None:
            await self._writer_task
            self._writer_task = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> StateStore:
        await self.open()
        await self.migrate()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
