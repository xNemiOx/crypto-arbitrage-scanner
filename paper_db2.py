"""
БД для агрессивной стратегии (усреднение в арбитраже).
Отдельная от paper_trades.db.
"""
import sqlite3
import json
import time
from typing import Dict, List, Optional


DB_PATH = "paper_trades_aggressive.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            buy_exchange TEXT NOT NULL,
            sell_exchange TEXT NOT NULL,
            entries TEXT NOT NULL DEFAULT '[]',
            total_invested REAL NOT NULL DEFAULT 0,
            additions_count INTEGER NOT NULL DEFAULT 0,
            max_spread_seen REAL NOT NULL DEFAULT 0,
            last_add_spread REAL NOT NULL DEFAULT 0,
            opened_at INTEGER NOT NULL,
            closed_at INTEGER,
            close_spread REAL,
            gross_profit REAL,
            fees REAL,
            net_profit REAL,
            close_reason TEXT,
            status TEXT NOT NULL DEFAULT 'open'
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx2_status_pair ON trades(status, pair)")
    conn.commit()
    conn.close()
    print(f"✅ БД2 инициализирована: {DB_PATH}")


def has_open_trade(pair: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM trades WHERE pair = ? AND status = 'open'", (pair,))
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def open_trade(pair: str, buy_exchange: str, sell_exchange: str,
               buy_price: float, sell_price: float,
               size_usdt: float, spread_pct: float) -> int:
    """Открывает сделку. Все цены в одной позиции."""
    entry = {
        "price_buy": buy_price,
        "price_sell": sell_price,
        "size_usdt": size_usdt,
        "spread_pct": spread_pct,
        "ts": int(time.time() * 1000),
    }
    ts = int(time.time() * 1000)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades
        (pair, buy_exchange, sell_exchange, entries, total_invested,
         additions_count, max_spread_seen, last_add_spread, opened_at, status)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 'open')
    """, (pair, buy_exchange, sell_exchange, json.dumps([entry]),
          size_usdt, spread_pct, spread_pct, ts))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    print(f"📈 AGG открыта #{trade_id}: {pair} buy@{buy_exchange}={buy_price} sell@{sell_exchange}={sell_price} spread={spread_pct:.2f}%", flush=True)
    return trade_id


def add_to_trade(trade_id: int, buy_price: float, sell_price: float,
                 size_usdt: float, spread_pct: float):
    """Добавляет ещё одну позицию (усреднение)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT entries, total_invested, additions_count FROM trades WHERE id = ?", (trade_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return
    entries = json.loads(row[0])
    entry = {
        "price_buy": buy_price,
        "price_sell": sell_price,
        "size_usdt": size_usdt,
        "spread_pct": spread_pct,
        "ts": int(time.time() * 1000),
    }
    entries.append(entry)
    new_total = row[1] + size_usdt
    new_count = row[2] + 1
    c.execute("""
        UPDATE trades SET entries = ?, total_invested = ?, additions_count = ?,
                          last_add_spread = ?
        WHERE id = ?
    """, (json.dumps(entries), new_total, new_count, spread_pct, trade_id))
    conn.commit()
    conn.close()
    print(f"➕ AGG добавление #{new_count} к #{trade_id}: +${size_usdt} (итого ${new_total}) spread={spread_pct:.2f}%", flush=True)


def update_max_spread(trade_id: int, spread_pct: float):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE trades SET max_spread_seen = MAX(max_spread_seen, ?) WHERE id = ?",
              (spread_pct, trade_id))
    conn.commit()
    conn.close()


def close_trade(trade_id: int, close_buy_bid: float, close_sell_ask: float,
                buy_fee: float, sell_fee: float, close_reason: str = "converged") -> Dict:
    """Закрывает сделку — считает P&L по всем entries."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {}
    cols = [d[0] for d in c.description]
    trade = dict(zip(cols, row))
    entries = json.loads(trade["entries"])

    gross = 0.0
    fees = 0.0
    for e in entries:
        qty_buy = e["size_usdt"] / e["price_buy"]
        qty_sell = e["size_usdt"] / e["price_sell"]
        # Лонг: (close_bid - entry_buy) × qty_buy
        gross += (close_buy_bid - e["price_buy"]) * qty_buy
        # Шорт: (entry_sell - close_ask) × qty_sell
        gross += (e["price_sell"] - close_sell_ask) * qty_sell
        # Комиссии: вход × 2 + выход × 2
        fees += e["size_usdt"] * buy_fee + e["size_usdt"] * sell_fee
        fees += qty_buy * close_buy_bid * buy_fee + qty_sell * close_sell_ask * sell_fee

    net = gross - fees
    ts = int(time.time() * 1000)
    c.execute("""
        UPDATE trades SET close_buy_bid = ?, close_sell_ask = ?, closed_at = ?,
                          close_spread = ?, gross_profit = ?, fees = ?,
                          net_profit = ?, close_reason = ?, status = 'closed'
        WHERE id = ?
    """, (close_buy_bid, close_sell_ask, ts, close_reason, gross, fees, net,
          close_reason, trade_id))
    conn.commit()
    conn.close()

    emoji = "🟢" if net > 0 else "🔴"
    print(f"{emoji} AGG закрыта #{trade_id}: {trade['pair']} "
          f"entries={len(entries)} total=${trade['total_invested']:.0f} "
          f"gross={gross:+.4f} fees={fees:.4f} net={net:+.4f} ({close_reason})", flush=True)
    return {"gross": gross, "fees": fees, "net": net}


def get_open_trades() -> List[Dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at")
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["entries"] = json.loads(r["entries"])
    return rows


def get_stats() -> Dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN net_profit > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN net_profit <= 0 THEN 1 ELSE 0 END),
               COALESCE(SUM(net_profit), 0),
               COALESCE(SUM(gross_profit), 0),
               COALESCE(SUM(fees), 0),
               COALESCE(AVG(net_profit), 0),
               COALESCE(SUM(total_invested), 0)
        FROM trades WHERE status = 'closed'
    """)
    row = c.fetchone()
    stats = {
        "total": row[0] or 0,
        "wins": row[1] or 0,
        "losses": row[2] or 0,
        "total_pnl": round(row[3], 4),
        "total_gross": round(row[4], 4),
        "total_fees": round(row[5], 4),
        "avg_pnl": round(row[6], 4),
        "total_invested_closed": round(row[7], 2),
    }
    stats["winrate"] = round(stats["wins"] / stats["total"] * 100, 1) if stats["total"] else 0
    conn.close()
    return stats


def get_recent_trades(limit: int = 50) -> List[Dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,))
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["entries"] = json.loads(r["entries"])
    return rows


if __name__ == "__main__":
    init_db()
    print()
    print("=== AGG Статистика ===")
    for k, v in get_stats().items():
        print(f"  {k}: {v}")
