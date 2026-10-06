"""
Модуль для работы с базой данных paper-trading.
Хранит историю виртуальных сделок и считает статистику.
"""
import sqlite3
import time
from typing import Optional, Dict, List


DB_PATH = "paper_trades.db"


def init_db():
    """Создаёт таблицу, если её нет."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            buy_exchange TEXT NOT NULL,
            sell_exchange TEXT NOT NULL,
            buy_price REAL NOT NULL,
            sell_price REAL NOT NULL,
            quantity REAL NOT NULL,
            size_usdt REAL NOT NULL,
            spread_pct REAL NOT NULL,
            -- Данные при закрытии
            close_buy_bid REAL,
            close_sell_ask REAL,
            closed_at INTEGER,
            gross_profit REAL,
            fees REAL,
            net_profit REAL,
            close_reason TEXT,
            -- Служебные
            opened_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        )
    """)
    # Индекс для быстрого поиска открытых сделок по паре
    c.execute("CREATE INDEX IF NOT EXISTS idx_status_pair ON trades(status, pair)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_opened_at ON trades(opened_at)")

    # Таблица для отслеживания проблемных пар
    c.execute("""
        CREATE TABLE IF NOT EXISTS pair_blocks (
            pair TEXT PRIMARY KEY,
            consecutive_losses INTEGER NOT NULL DEFAULT 0,
            blocked_until INTEGER NOT NULL DEFAULT 0,
            last_trade_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    print(f"✅ БД инициализирована: {DB_PATH}")


def has_open_trade(pair: str) -> bool:
    """Проверяет, есть ли уже открытая сделка по паре."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM trades WHERE pair = ? AND status = 'open'", (pair,))
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def open_trade(
    pair: str,
    buy_exchange: str,
    sell_exchange: str,
    buy_price: float,
    sell_price: float,
    size_usdt: float,
    spread_pct: float,
) -> int:
    """Регистрирует новую виртуальную сделку. Возвращает id."""
    quantity = size_usdt / buy_price
    ts = int(time.time() * 1000)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades
        (pair, buy_exchange, sell_exchange, buy_price, sell_price,
         quantity, size_usdt, spread_pct, opened_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
    """, (pair, buy_exchange, sell_exchange, buy_price, sell_price,
          quantity, size_usdt, spread_pct, ts))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    print(f"📈 Открыта виртуальная сделка #{trade_id}: {pair} "
          f"buy@{buy_exchange}={buy_price} sell@{sell_exchange}={sell_price} "
          f"spread={spread_pct:.2f}%")
    return trade_id


def close_trade(
    trade_id: int,
    close_buy_bid: float,
    close_sell_ask: float,
    buy_fee: float,
    sell_fee: float,
    close_reason: str = "timeout",
) -> Dict:
    """
    Закрывает сделку, считает P&L.
    close_buy_bid — текущий bid на бирже, где КУПИЛИ (закрываем лонг — продаём)
    close_sell_ask — текущий ask на бирже, где ПРОДАЛИ (закрываем шорт — покупаем)
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {}

    columns = [d[0] for d in c.description]
    trade = dict(zip(columns, row))

    qty = trade["quantity"]
    buy_price = trade["buy_price"]
    sell_price = trade["sell_price"]

    # Лонг: купили по buy_price, продаём по close_buy_bid
    long_pnl = (close_buy_bid - buy_price) * qty
    # Шорт: продали по sell_price, покупаем по close_sell_ask
    short_pnl = (sell_price - close_sell_ask) * qty
    gross = long_pnl + short_pnl

    # Комиссии: вход (buy_fee + sell_fee) + выход (buy_fee + sell_fee)
    fees = qty * buy_price * buy_fee + qty * sell_price * sell_fee
    fees += qty * close_buy_bid * buy_fee + qty * close_sell_ask * sell_fee

    net = gross - fees
    ts = int(time.time() * 1000)

    c.execute("""
        UPDATE trades SET
            close_buy_bid = ?, close_sell_ask = ?,
            closed_at = ?, gross_profit = ?, fees = ?,
            net_profit = ?, close_reason = ?, status = 'closed'
        WHERE id = ?
    """, (close_buy_bid, close_sell_ask, ts, gross, fees, net, close_reason, trade_id))
    conn.commit()
    conn.close()

    emoji = "🟢" if net > 0 else "🔴"
    print(f"{emoji} Закрыта сделка #{trade_id}: {trade['pair']} "
          f"gross={gross:.4f} fees={fees:.4f} net={net:.4f} ({close_reason})", flush=True)

    # Регистрируем результат для авто-блокировки
    register_trade_result(trade["pair"], is_win=(net > 0))

    return {"gross": gross, "fees": fees, "net": net}




def is_pair_blocked(pair: str) -> bool:
    """Проверяет, заблокирована ли пара."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT blocked_until FROM pair_blocks WHERE pair = ?", (pair,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    return int(time.time() * 1000) < row[0]


def is_pair_in_cooldown(pair: str, cooldown_sec: int = 300) -> bool:
    """Проверяет кулдаун после последней сделки по паре (5 мин по умолчанию)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_trade_at FROM pair_blocks WHERE pair = ?", (pair,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    return int(time.time() * 1000) - row[0] < cooldown_sec * 1000


def register_trade_result(pair: str, is_win: bool):
    """Обновляет счётчик убытков. Если 2 подряд — блокирует на 2 часа."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT consecutive_losses FROM pair_blocks WHERE pair = ?", (pair,))
    row = c.fetchone()
    current_losses = row[0] if row else 0
    now_ms = int(time.time() * 1000)

    if is_win:
        # Прибыль — сбрасываем счётчик
        new_losses = 0
        blocked_until = 0
    else:
        new_losses = current_losses + 1
        if new_losses >= 2:
            # 2 убытка подряд — блок на 2 часа
            blocked_until = now_ms + 2 * 3600 * 1000
            print(f"🚫 Пара {pair} ЗАБЛОКИРОВАНА на 2 часа ({new_losses} убытка подряд)", flush=True)

    c.execute("""
        INSERT INTO pair_blocks (pair, consecutive_losses, blocked_until, last_trade_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(pair) DO UPDATE SET
            consecutive_losses = ?,
            blocked_until = ?,
            last_trade_at = ?
    """, (pair, new_losses, blocked_until, now_ms,
          new_losses, blocked_until, now_ms))
    conn.commit()
    conn.close()


def get_open_trades() -> List[Dict]:
    """Все открытые сделки."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at")
    columns = [d[0] for d in c.description]
    rows = [dict(zip(columns, r)) for r in c.fetchall()]
    conn.close()
    return rows


def get_stats() -> Dict:
    """Общая статистика."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN net_profit > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN net_profit <= 0 THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(net_profit), 0) as total_pnl,
            COALESCE(SUM(gross_profit), 0) as total_gross,
            COALESCE(SUM(fees), 0) as total_fees,
            COALESCE(AVG(net_profit), 0) as avg_pnl
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
    }
    if stats["total"] > 0:
        stats["winrate"] = round(stats["wins"] / stats["total"] * 100, 1)
    else:
        stats["winrate"] = 0.0
    conn.close()
    return stats


def get_recent_trades(limit: int = 50) -> List[Dict]:
    """Последние N сделок."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,))
    columns = [d[0] for d in c.description]
    rows = [dict(zip(columns, r)) for r in c.fetchall()]
    conn.close()
    return rows


if __name__ == "__main__":
    init_db()
    print()
    print("=== Статистика ===")
    stats = get_stats()
    for k, v in stats.items():
        print(f"  {k}: {v}")
