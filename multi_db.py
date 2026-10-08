"""
БД для мульти-стратегий.
Одна таблица trades с полем strategy_id.
SQLite в WAL-режиме для параллельных записей.
"""
import sqlite3
import json
import time
from typing import Dict, List, Optional


DB_PATH = "multi_trades.db"


def get_conn():
    """Открывает соединение с WAL-режимом."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """Создаёт таблицу стратегий и trades."""
    conn = get_conn()
    c = conn.cursor()

    # Стратегии
    c.execute("""
        CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            balance REAL NOT NULL DEFAULT 5000,
            initial_balance REAL NOT NULL DEFAULT 5000
        )
    """)

    # Сделки
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id INTEGER NOT NULL,
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
            close_buy_bid REAL,
            close_sell_ask REAL,
            gross_profit REAL,
            fees REAL,
            net_profit REAL,
            close_reason TEXT,
            status TEXT NOT NULL DEFAULT 'open'
        )
    """)

    # Индексы для быстрого поиска
    c.execute("CREATE INDEX IF NOT EXISTS idx_mt_status_strategy ON trades(status, strategy_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mt_strategy_pair ON trades(strategy_id, pair)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mt_opened ON trades(opened_at)")

    conn.commit()
    conn.close()
    print(f"✅ Multi-DB инициализирована: {DB_PATH}")


def register_strategy(sid: int, name: str, initial_balance: float = 5000):
    """Регистрирует стратегию если её нет."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO strategies (id, name, balance, initial_balance)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET name = ?
    """, (sid, name, initial_balance, initial_balance, name))
    conn.commit()
    conn.close()


def get_balance(sid: int) -> float:
    """Возвращает текущий баланс стратегии."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT balance FROM strategies WHERE id = ?", (sid,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0.0


def update_balance(sid: int, delta: float):
    """Обновляет баланс (положительный delta = прибыль, отрицательный = трата)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE strategies SET balance = balance + ? WHERE id = ?", (delta, sid))
    conn.commit()
    conn.close()


def has_open_trade(sid: int, pair: str) -> bool:
    """Есть ли открытая сделка по этой паре у этой стратегии."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM trades WHERE strategy_id = ? AND pair = ? AND status = 'open'",
        (sid, pair)
    )
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def get_open_count(sid: int) -> int:
    """Сколько открытых сделок у стратегии."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM trades WHERE strategy_id = ? AND status = 'open'", (sid,))
    count = c.fetchone()[0]
    conn.close()
    return count


def open_trade(sid: int, pair: str, buy_exchange: str, sell_exchange: str,
               buy_price: float, sell_price: float,
               size_usdt: float, spread_pct: float) -> int:
    """Открывает сделку."""
    entry = {
        "price_buy": buy_price,
        "price_sell": sell_price,
        "size_usdt": size_usdt,
        "spread_pct": spread_pct,
        "ts": int(time.time() * 1000),
    }
    ts = int(time.time() * 1000)
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades
        (strategy_id, pair, buy_exchange, sell_exchange, entries, total_invested,
         additions_count, max_spread_seen, last_add_spread, opened_at, status)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'open')
    """, (sid, pair, buy_exchange, sell_exchange, json.dumps([entry]),
          size_usdt, spread_pct, spread_pct, ts))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def add_to_trade(trade_id: int, buy_price: float, sell_price: float,
                 size_usdt: float, spread_pct: float):
    """Добавление к существующей сделке."""
    conn = get_conn()
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
        UPDATE trades SET entries = ?, total_invested = ?, additions_count = ?, last_add_spread = ?
        WHERE id = ?
    """, (json.dumps(entries), new_total, new_count, spread_pct, trade_id))
    conn.commit()
    conn.close()


def update_max_spread(trade_id: int, spread_pct: float):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE trades SET max_spread_seen = MAX(max_spread_seen, ?) WHERE id = ?",
              (spread_pct, trade_id))
    conn.commit()
    conn.close()


def close_trade(trade_id: int, close_buy_bid: float, close_sell_ask: float,
                buy_fee: float, sell_fee: float, close_reason: str = "converged") -> Dict:
    """Закрывает сделку, обновляет баланс стратегии."""
    conn = get_conn()
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
        gross += (close_buy_bid - e["price_buy"]) * qty_buy
        gross += (e["price_sell"] - close_sell_ask) * qty_sell
        fees += e["size_usdt"] * buy_fee + e["size_usdt"] * sell_fee
        fees += qty_buy * close_buy_bid * buy_fee + qty_sell * close_sell_ask * sell_fee

    net = gross - fees
    ts = int(time.time() * 1000)
    c.execute("""
        UPDATE trades SET
            close_buy_bid = ?, close_sell_ask = ?, closed_at = ?,
            gross_profit = ?, fees = ?, net_profit = ?,
            close_reason = ?, status = 'closed'
        WHERE id = ?
    """, (close_buy_bid, close_sell_ask, ts, gross, fees, net, close_reason, trade_id))

    # Возвращаем капитал + прибыль на баланс
    c.execute("UPDATE strategies SET balance = balance + ? WHERE id = ?",
              (trade["total_invested"] + net, trade["strategy_id"]))

    conn.commit()
    conn.close()
    return {"gross": gross, "fees": fees, "net": net, "strategy_id": trade["strategy_id"], "pair": trade["pair"]}




def get_open_count_by_exchange(sid: int, exchange: str) -> int:
    """Сколько открытых сделок у стратегии, где данная биржа участвует (buy или sell)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) FROM trades
        WHERE strategy_id = ? AND status = 'open'
        AND (buy_exchange = ? OR sell_exchange = ?)
    """, (sid, exchange, exchange))
    count = c.fetchone()[0]
    conn.close()
    return count

def get_open_trades(sid: Optional[int] = None) -> List[Dict]:
    """Открытые сделки (все или конкретной стратегии)."""
    conn = get_conn()
    c = conn.cursor()
    if sid is not None:
        c.execute("SELECT * FROM trades WHERE status = 'open' AND strategy_id = ? ORDER BY opened_at", (sid,))
    else:
        c.execute("SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at")
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["entries"] = json.loads(r["entries"])
    return rows


def get_strategy_stats(sid: int) -> Dict:
    """Статистика одной стратегии."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT balance, initial_balance, name FROM strategies WHERE id = ?", (sid,))
    srow = c.fetchone()
    if not srow:
        conn.close()
        return {}
    balance, initial, name = srow

    c.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN net_profit > 0 THEN 1 ELSE 0 END),
               COALESCE(SUM(net_profit), 0),
               COALESCE(SUM(gross_profit), 0),
               COALESCE(SUM(fees), 0),
               COALESCE(SUM(total_invested), 0)
        FROM trades WHERE strategy_id = ? AND status = 'closed'
    """, (sid,))
    row = c.fetchone()

    open_count = c.execute("SELECT COUNT(*) FROM trades WHERE strategy_id = ? AND status = 'open'", (sid,)).fetchone()[0]

    # Считаем сумму в открытых сделках
    c.execute("SELECT COALESCE(SUM(total_invested), 0) FROM trades WHERE strategy_id = ? AND status = 'open'", (sid,))
    in_work = c.fetchone()[0]

    equity = balance + in_work
    real_profit = equity - initial

    stats = {
        "strategy_id": sid,
        "name": name,
        "balance": round(balance, 4),
        "in_work": round(in_work, 2),
        "equity": round(equity, 4),
        "initial_balance": initial,
        "profit": round(real_profit, 4),
        "profit_pct": round(real_profit / initial * 100, 4),
        "total": row[0] or 0,
        "wins": row[1] or 0,
        "losses": (row[0] or 0) - (row[1] or 0),
        "total_net": round(row[2], 4),
        "total_gross": round(row[3], 4),
        "total_fees": round(row[4], 4),
        "total_invested": round(row[5], 2),
        "open_trades": open_count,
    }
    stats["winrate"] = round(stats["wins"] / stats["total"] * 100, 1) if stats["total"] else 0
    conn.close()
    return stats


def get_all_strategies_stats() -> List[Dict]:
    """Статистика всех стратегий (отсортирована по profit)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM strategies ORDER BY id")
    ids = [r[0] for r in c.fetchall()]
    conn.close()
    stats = [get_strategy_stats(sid) for sid in ids]
    stats = [s for s in stats if s]
    stats.sort(key=lambda x: x.get("profit", 0), reverse=True)
    return stats


def get_strategy_trades(sid: int, limit: int = 100) -> List[Dict]:
    """Последние сделки стратегии."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE strategy_id = ? ORDER BY opened_at DESC LIMIT ?", (sid, limit))
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["entries"] = json.loads(r["entries"])
    return rows


def reset_strategy(sid: int):
    """Полный сброс стратегии (удаление сделок + баланс 5000)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM trades WHERE strategy_id = ?", (sid,))
    c.execute("UPDATE strategies SET balance = initial_balance WHERE id = ?", (sid,))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print()
    print("Тестовый запуск:")
    register_strategy(1, "TEST_STRATEGY")
    print(f"Баланс стратегии 1: ${get_balance(1)}")
