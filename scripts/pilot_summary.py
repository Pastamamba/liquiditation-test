"""Pilot post-mortem CLI -- kaivaa metriikat mm_bot.db:stä raporttia varten.

Käyttö:
    python scripts/pilot_summary.py --db data/mm_bot_testnet_001.db

Mitä tämä OSAA poimia DB:stä:
    - Fillit (kpl bid/ask, volyymi, total fees)
    - Orderit (placed/filled/cancelled, mean/median/p95 lifetime)
    - PnL-snapshotit (latest = ajon viimeinen, realized/unrealized/spread/fees)
    - Eventit (info/warning/error/kill), top-N error-viestit

Mitä tämä EI näe (in-memory metriikoissa, ei tallenneta DB:hen):
    - Quote-latenssin percentiilit (p50/p95/p99) -- ne pidetään
      `MetricsCollector._latency_samples`-dequessä, ja tyhjenevät prosessin
      sammutuksessa. Kaappaa ne Telegram /status -komennolla ENNEN kuin
      sammutat botin.
    - Adverse selection bps -keskiarvo -- sama (`_adverse_history`-deque).
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import Counter
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path


def _q(samples: list[float], p: float) -> float:
    if not samples:
        return 0.0
    samples = sorted(samples)
    k = (len(samples) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(samples) - 1)
    return samples[f] + (samples[c] - samples[f]) * (k - f) if f != c else samples[f]


def _fmt_dec(s: str | None, *, places: int = 4) -> str:
    if s is None:
        return "--"
    try:
        return f"{Decimal(s):.{places}f}"
    except Exception:
        return s


def _fmt_ts_ms(ts_ms: int | None) -> str:
    if ts_ms is None:
        return "--"
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.UTC).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def summarize_fills(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "SELECT side, price, size, fee, is_maker FROM fills ORDER BY timestamp ASC"
    )
    rows = cur.fetchall()
    print("=== Fills ===")
    print(f"Total: {len(rows)}")
    if not rows:
        print("(ei yhtään filliä)\n")
        return
    bids = [r for r in rows if r[0] == "bid"]
    asks = [r for r in rows if r[0] == "ask"]
    bid_vol = sum(Decimal(r[2]) for r in bids)
    ask_vol = sum(Decimal(r[2]) for r in asks)
    total_fee = sum(Decimal(r[3]) for r in rows)
    bid_notional = sum(Decimal(r[1]) * Decimal(r[2]) for r in bids)
    ask_notional = sum(Decimal(r[1]) * Decimal(r[2]) for r in asks)
    maker_count = sum(1 for r in rows if int(r[4]) == 1)
    print(f"  bid: {len(bids):4d}  size_sum={bid_vol} ETH  notional={bid_notional:.2f} USDC")
    print(f"  ask: {len(asks):4d}  size_sum={ask_vol} ETH  notional={ask_notional:.2f} USDC")
    print(f"  fees_sum:      {total_fee:.6f} USDC")
    print(f"  Maker fillejä: {maker_count}/{len(rows)}")
    print()


def summarize_orders(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "SELECT id, status, timestamp, cancel_timestamp FROM orders"
    )
    rows = cur.fetchall()
    print("=== Orders ===")
    print(f"Total: {len(rows)}")
    if not rows:
        print("(ei ordereita)\n")
        return
    statuses = Counter(r[1] for r in rows)
    for status, count in sorted(statuses.items()):
        print(f"  {status:10s} {count}")

    # Lifetime: cancelled = cancel_timestamp - timestamp
    # filled  = ensimmäisen matchaavan fillin timestamp - placement timestamp
    fill_times: dict[str, int] = {}
    fcur = conn.execute("SELECT order_id, MIN(timestamp) FROM fills GROUP BY order_id")
    for oid, ts in fcur.fetchall():
        fill_times[oid] = int(ts)

    lifetimes_s: list[float] = []
    for oid, status, placed, cancel_ts in rows:
        if status == "cancelled" and cancel_ts is not None:
            lifetimes_s.append((int(cancel_ts) - int(placed)) / 1000.0)
        elif status == "filled":
            ft = fill_times.get(oid)
            if ft is not None:
                lifetimes_s.append((ft - int(placed)) / 1000.0)
    if lifetimes_s:
        print("  Lifetime (s) -- placed -> filled / cancelled:")
        print(f"    mean   = {statistics.fmean(lifetimes_s):.2f}")
        print(f"    median = {statistics.median(lifetimes_s):.2f}")
        print(f"    p95    = {_q(lifetimes_s, 95):.2f}")
        print(f"    n      = {len(lifetimes_s)}")
    cancel_count = statuses.get("cancelled", 0)
    placed_count = len(rows)
    if placed_count > 0:
        print(f"  Cancel/place suhde: {cancel_count / placed_count:.3f}")
    print()


def summarize_pnl(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "SELECT timestamp, realized_pnl, unrealized_pnl, inventory, capital, "
        "spread_pnl, rebate_earned FROM pnl_snapshots "
        "ORDER BY timestamp DESC LIMIT 1"
    )
    row = cur.fetchone()
    print("=== PnL (latest snapshot) ===")
    if row is None:
        print("(ei snapshotteja)\n")
        return
    ts, realized, unrealized, inv, cap, spread, rebate = row
    print(f"  At:               {_fmt_ts_ms(int(ts))}")
    print(f"  Capital:          {_fmt_dec(cap, places=2)} USDC")
    print(f"  Inventory:        {_fmt_dec(inv, places=6)} (kohde-asset)")
    print(f"  Realized PnL:     {_fmt_dec(realized, places=4)} USDC")
    print(f"  Unrealized PnL:   {_fmt_dec(unrealized, places=4)} USDC")
    print(f"  Spread PnL gross: {_fmt_dec(spread, places=4)} USDC")
    print(f"  Rebates earned:   {_fmt_dec(rebate, places=4)} USDC")
    fees_q = conn.execute("SELECT COALESCE(SUM(CAST(fee AS REAL)), 0) FROM fills")
    total_fees_row = fees_q.fetchone()
    total_fees = float(total_fees_row[0]) if total_fees_row else 0.0
    print(f"  Total fees:       {total_fees:.4f} USDC")
    try:
        net = (
            Decimal(realized)
            - Decimal(str(total_fees))
            + Decimal(rebate)
            + Decimal(unrealized)
        )
        print(f"  Net (R-F+Reb+U):  {net:.4f} USDC")
        if Decimal(cap) > 0:
            pct = (net / Decimal(cap)) * Decimal("100")
            print(f"  Net % capital:    {pct:.3f} %")
    except Exception:
        pass
    print()


def summarize_events(conn: sqlite3.Connection) -> None:
    cur = conn.execute("SELECT level, COUNT(*) FROM events GROUP BY level")
    by_level = dict(cur.fetchall())
    print("=== Events ===")
    if not by_level:
        print("(ei eventtejä)\n")
        return
    for level in ("info", "warning", "error", "kill"):
        n = by_level.get(level, 0)
        marker = ""
        if level in ("error", "kill") and n > 0:
            marker = "  !!"
        elif level == "warning" and n > 0:
            marker = "  ."
        print(f"  {level:8s} {n:5d}{marker}")
    err_cur = conn.execute(
        "SELECT timestamp, component, message FROM events "
        "WHERE level IN ('error', 'kill') "
        "ORDER BY timestamp DESC LIMIT 10"
    )
    err_rows = err_cur.fetchall()
    if err_rows:
        print("\n  Latest 10 error/kill events:")
        for ts, component, msg in err_rows:
            print(f"    [{_fmt_ts_ms(int(ts))}] {component}: {msg}")
    print()


def summarize_run_window(conn: sqlite3.Connection) -> None:
    print("=== Run window ===")
    pieces: list[tuple[str, int | None]] = []
    for label, sql in (
        ("first fill",       "SELECT MIN(timestamp) FROM fills"),
        ("last fill",        "SELECT MAX(timestamp) FROM fills"),
        ("first order",      "SELECT MIN(timestamp) FROM orders"),
        ("last order event", "SELECT MAX(COALESCE(cancel_timestamp, timestamp)) FROM orders"),
        ("first event",      "SELECT MIN(timestamp) FROM events"),
        ("last event",       "SELECT MAX(timestamp) FROM events"),
    ):
        row = conn.execute(sql).fetchone()
        pieces.append((label, int(row[0]) if row and row[0] is not None else None))
    for label, ts in pieces:
        print(f"  {label:18s} {_fmt_ts_ms(ts)}")

    starts = [ts for label, ts in pieces if "first" in label and ts is not None]
    ends = [ts for label, ts in pieces if ("last" in label) and ts is not None]
    if starts and ends:
        elapsed_s = (max(ends) - min(starts)) / 1000.0
        h, rem = divmod(int(elapsed_s), 3600)
        m, s = divmod(rem, 60)
        print(f"  approx duration:   {h:02d}:{m:02d}:{s:02d}")
    print()


def _print_in_memory_caveat() -> None:
    print("=== EI saatavilla DB:stä (kaappaa ennen sammutusta) ===")
    print("  Quote-latenssi p50/p95/p99 -- `MetricsCollector._latency_samples`")
    print("  Adverse selection bps ka.  -- `MetricsCollector._adverse_history`")
    print("  -> Pyydä Telegram /status botilta ENNEN sammutusta ja kopioi raporttiin.")
    print()


def run(db_path: Path) -> int:
    if not db_path.exists():
        print(f"ERROR: db ei löydy: {db_path}", file=sys.stderr)
        return 2
    print(f"Pilot summary -- db={db_path}\n")
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        summarize_run_window(conn)
        summarize_fills(conn)
        summarize_orders(conn)
        summarize_pnl(conn)
        summarize_events(conn)
    _print_in_memory_caveat()
    return 0


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--db",
        type=Path,
        default=Path("data/mm_bot_testnet_001.db"),
        help="Polku SQLite-tiedostoon (default: %(default)s)",
    )
    return p.parse_args(list(argv) if argv is not None else None)


def main() -> int:
    args = _parse_args()
    return run(args.db)


if __name__ == "__main__":
    raise SystemExit(main())
