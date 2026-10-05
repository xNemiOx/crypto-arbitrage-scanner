"""
Логика paper-trading: открытие/закрытие виртуальных сделок.
Стратегия: одновременный long+short на двух биржах при спреде >= порога.
Закрытие через N секунд по текущим ценам.
"""
import time
from typing import Dict, Tuple, Optional

import paper_db


# Параметры (будут перезаписаны из main.py при импорте)
PAPER_TRADE_SIZE_USDT = 50.0
PAPER_TRADE_THRESHOLD_PCT = 0.7
PAPER_TRADE_HOLD_SECONDS = 150      # 15 минут максимум (аварийный выход)
PAPER_TRADE_MAX_OPEN = 20
PAPER_TRADE_MIN_PROFIT = 0.0
PAPER_TRADE_STOP_LOSS_PCT = -0.3    # стоп-лосс: если gross < -0.5%, закрываем

# Биржи, которые отдают нереальные данные — не используем в paper trading

def _get_quote(quotes_store: Dict, exchange: str, pair: str) -> Optional[Dict]:
    """Достаёт котировку из quotes_store или None."""
    key = (exchange, pair)
    if key in quotes_store:
        q = quotes_store[key]
        return {"bid": q.bid, "ask": q.ask, "ts": q.ts}
    return None


def _check_max_age(q: Dict, max_age_ms: int = 180000) -> bool:
    """Проверяет, что котировка свежая (по умолчанию 120 секунд)."""
    if not q:
        return False
    age = int(time.time() * 1000) - q["ts"]
    return age < max_age_ms


def try_open_trades(spreads_store: Dict, quotes_store: Dict, exchange_fees: Dict):
    """Проходит по всем спредам и открывает сделки там, где это возможно."""
    if not spreads_store:
        return

    open_count = sum(1 for _ in paper_db.get_open_trades())
    if open_count >= PAPER_TRADE_MAX_OPEN:
        return

    # Сортируем по убыванию процента — сначала самые вкусные
    sorted_spreads = sorted(
        spreads_store.items(),
        key=lambda x: x[1].get("spread_pct", 0),
        reverse=True,
    )

    # Отладка: показываем топ-3 спреда
    top3 = sorted_spreads[:3]
    top3_str = " | ".join(f"{p}:{d.get('spread_pct', 0):.3f}%" for p, d in top3)
    print(f"📊 PAPER top-3: {top3_str}", flush=True)

    for pair, data in sorted_spreads:
        if open_count >= PAPER_TRADE_MAX_OPEN:
            break

        spread_pct = data.get("spread_pct", 0)
        if spread_pct < PAPER_TRADE_THRESHOLD_PCT:
            continue

        net_profit = data.get("net_profit_usdt", 0)
        if net_profit <= PAPER_TRADE_MIN_PROFIT:
            continue

        # Отладка: спред прошёл фильтр, смотрим дальше
        print(f"🔍 PAPER: {pair} спред={spread_pct:.4f}% (порог {PAPER_TRADE_THRESHOLD_PCT}%)")

        # Уже есть открытая сделка по этой паре?
        if paper_db.has_open_trade(pair):
            continue

        buy_ex = data.get("min_ask_exchange")
        sell_ex = data.get("max_bid_exchange")
        if not buy_ex or not sell_ex or buy_ex == sell_ex:
            continue

        # Реальные цены на момент открытия
        buy_q = _get_quote(quotes_store, buy_ex, pair)
        sell_q = _get_quote(quotes_store, sell_ex, pair)
        if not buy_q or not sell_q:
            continue

        # Свежие ли данные
        if not _check_max_age(buy_q) or not _check_max_age(sell_q):
            continue

        buy_price = buy_q["ask"]   # покупаем по ask
        sell_price = sell_q["bid"]  # продаём по bid

        # Повторная проверка: реальный спред >= порога
        real_spread = (sell_price - buy_price) / buy_price * 100
        if real_spread < PAPER_TRADE_THRESHOLD_PCT:
            print(f"   ⚠️ {pair}: real_spread={real_spread:.4f}% < порог {PAPER_TRADE_THRESHOLD_PCT}%")
            continue

        # Проверяем, что прибыль покрывает комиссии
        buy_fee = exchange_fees.get(buy_ex, 0.001)
        sell_fee = exchange_fees.get(sell_ex, 0.001)
        est_profit = (sell_price * (1 - sell_fee) - buy_price * (1 + buy_fee)) * (PAPER_TRADE_SIZE_USDT / buy_price)
        if est_profit <= 0:
            continue

        print(f"   → buy {buy_ex}@{buy_price}, sell {sell_ex}@{sell_price}, real_spread={real_spread:.4f}%")
        paper_db.open_trade(
            pair=pair,
            buy_exchange=buy_ex,
            sell_exchange=sell_ex,
            buy_price=buy_price,
            sell_price=sell_price,
            size_usdt=PAPER_TRADE_SIZE_USDT,
            spread_pct=real_spread,
        )
        open_count += 1


def try_close_trades(quotes_store: Dict, exchange_fees: Dict):
    """
    Закрывает сделки когда:
    1. Спред сошёлся (gross profit > 0 после комиссий) — целевой сценарий
    2. Стоп-лосс: gross упал ниже -0.5%
    3. Timeout: прошло больше HOLD_SECONDS (аварийный выход)
    """
    open_trades = paper_db.get_open_trades()
    if not open_trades:
        return

    now_ms = int(time.time() * 1000)

    for trade in open_trades:
        opened_at = trade["opened_at"]
        elapsed_s = (now_ms - opened_at) / 1000.0

        pair = trade["pair"]
        buy_ex = trade["buy_exchange"]
        sell_ex = trade["sell_exchange"]

        buy_q = _get_quote(quotes_store, buy_ex, pair)
        sell_q = _get_quote(quotes_store, sell_ex, pair)

        # Если данных нет — только по таймауту закрываем
        if not buy_q or not sell_q:
            if elapsed_s >= PAPER_TRADE_HOLD_SECONDS:
                paper_db.close_trade(
                    trade_id=trade["id"],
                    close_buy_bid=trade["buy_price"],
                    close_sell_ask=trade["sell_price"],
                    buy_fee=exchange_fees.get(buy_ex, 0.0005),
                    sell_fee=exchange_fees.get(sell_ex, 0.0005),
                    close_reason="no_data",
                )
            continue

        close_buy_bid = buy_q["bid"]   # закроем лонг: продадим по bid
        close_sell_ask = sell_q["ask"]  # закроем шорт: купим по ask

        qty = trade["quantity"]
        buy_price = trade["buy_price"]
        sell_price = trade["sell_price"]

        # Текущий gross (без комиссий)
        long_pnl = (close_buy_bid - buy_price) * qty
        short_pnl = (sell_price - close_sell_ask) * qty
        current_gross = long_pnl + short_pnl

        # Оценка комиссий
        buy_fee = exchange_fees.get(buy_ex, 0.0005)
        sell_fee = exchange_fees.get(sell_ex, 0.0005)
        est_fees = (qty * buy_price * buy_fee + qty * sell_price * sell_fee +
                    qty * close_buy_bid * buy_fee + qty * close_sell_ask * sell_fee)
        est_net = current_gross - est_fees

        # ПРАВИЛО 1: прибыль есть (net > 0) — закрываем сразу
        if est_net > 0:
            paper_db.close_trade(
                trade_id=trade["id"],
                close_buy_bid=close_buy_bid,
                close_sell_ask=close_sell_ask,
                buy_fee=buy_fee,
                sell_fee=sell_fee,
                close_reason="profit",
            )
            print(f"   ✅ Закрыто с прибылью: {pair} net≈{est_net:+.4f}")
            continue

        # ПРАВИЛО 2: стоп-лосс (gross упал ниже -0.5%)
        gross_pct = current_gross / (buy_price * qty) * 100
        if gross_pct < PAPER_TRADE_STOP_LOSS_PCT:
            paper_db.close_trade(
                trade_id=trade["id"],
                close_buy_bid=close_buy_bid,
                close_sell_ask=close_sell_ask,
                buy_fee=buy_fee,
                sell_fee=sell_fee,
                close_reason="stop_loss",
            )
            print(f"   🛑 Стоп-лосс: {pair} gross={gross_pct:.2f}%")
            continue

        # ПРАВИЛО 3: таймаут (15 минут)
        if elapsed_s >= PAPER_TRADE_HOLD_SECONDS:
            paper_db.close_trade(
                trade_id=trade["id"],
                close_buy_bid=close_buy_bid,
                close_sell_ask=close_sell_ask,
                buy_fee=buy_fee,
                sell_fee=sell_fee,
                close_reason="timeout",
            )
            print(f"   ⏰ Таймаут: {pair} ({elapsed_s:.0f} сек)")
            continue


def run(spreads_store: Dict, quotes_store: Dict, exchange_fees: Dict):
    """Одна итерация paper trading — только ОТКРЫТИЕ новых сделок.
    Закрытие теперь в отдельном цикле close_positions_loop в main.py.
    """
    try:
        try_open_trades(spreads_store, quotes_store, exchange_fees)
    except Exception as e:
        print(f"❌ Paper trader error: {e}", flush=True)
