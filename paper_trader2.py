"""
Агрессивная стратегия: усреднение в арбитраже.
- Нет стоп-лосса
- Нет таймаута
- Добавление позиций при росте спреда
- Закрытие при сужении спреда
"""
import time
from typing import Dict, Optional

import paper_db2


# ===== Параметры (перезаписываются из main.py) =====
AGG_TRADE_SIZE_USDT = 50.0           # размер одного добавления
AGG_THRESHOLD_PCT = 0.5              # минимальный спред для входа
AGG_ADD_THRESHOLD_PCT = 20           # +20% к спреду → добавляем
AGG_ADD_STEP_PCT = 0.1               # минимальный шаг спреда для добавления (0.1%)
AGG_CLOSE_THRESHOLD_PCT = 0.15       # закрыть при спреде < 0.15%
AGG_MAX_ADDITIONS = 10               # максимум $500 на пару
AGG_MAX_OPEN = 10                    # максимум 30 открытых сделок
AGG_MIN_PROFIT_TO_CLOSE = 0.0        # не закрывать в минус (кроме крайнего случая)


def _get_quote(quotes_store: Dict, exchange: str, pair: str) -> Optional[Dict]:
    key = (exchange, pair)
    if key in quotes_store:
        q = quotes_store[key]
        return {"bid": q.bid, "ask": q.ask, "ts": q.ts}
    return None


def _check_max_age(q: Dict, max_age_ms: int = 300000) -> bool:
    """5 минут — агрессивная стратегия не так чувствительна к свежести."""
    if not q:
        return False
    age = int(time.time() * 1000) - q["ts"]
    return age < max_age_ms


def _current_spread(buy_price: float, sell_price: float) -> float:
    return (sell_price - buy_price) / buy_price * 100


# ==================================================
# ЗАКРЫТИЕ
# ==================================================
def try_close_trades(quotes_store: Dict, exchange_fees: Dict):
    """
    Закрывает сделку когда спред СУЗИЛСЯ до AGG_CLOSE_THRESHOLD_PCT.
    Без стоп-лосса, без таймаута.
    """
    open_trades = paper_db2.get_open_trades()
    if not open_trades:
        return

    for trade in open_trades:
        pair = trade["pair"]
        buy_ex = trade["buy_exchange"]
        sell_ex = trade["sell_exchange"]

        buy_q = _get_quote(quotes_store, buy_ex, pair)
        sell_q = _get_quote(quotes_store, sell_ex, pair)

        if not buy_q or not sell_q:
            continue

        close_buy_bid = buy_q["bid"]    # закроем лонг: продадим по bid
        close_sell_ask = sell_q["ask"]  # закроем шорт: купим по ask

        current_spread = _current_spread(close_sell_ask, close_buy_bid) * 100
        # (spread = (sell - buy) / buy, т.е. текущий спред)
        # Правильно: (close_buy_bid - close_sell_ask) / close_sell_ask? 
        # Точнее: если сейчас можно закрыть с profit при (bid_лонга - ask_шорта)
        # "Сужение спреда" = цена на buy-бирже выросла ИЛИ на sell-бирже упала.
        # Проверяем именно возможность закрытия: если мы закроем сейчас,
        # gross будет >= 0 или чуть больше нуля.

        # Считаем предполагаемый gross
        entries = trade["entries"]
        gross = 0.0
        for e in entries:
            qty_buy = e["size_usdt"] / e["price_buy"]
            qty_sell = e["size_usdt"] / e["price_sell"]
            gross += (close_buy_bid - e["price_buy"]) * qty_buy
            gross += (e["price_sell"] - close_sell_ask) * qty_sell

        # Комиссии
        buy_fee = exchange_fees.get(buy_ex, 0.0005)
        sell_fee = exchange_fees.get(sell_ex, 0.0005)
        fees = 0.0
        for e in entries:
            qty_buy = e["size_usdt"] / e["price_buy"]
            qty_sell = e["size_usdt"] / e["price_sell"]
            fees += e["size_usdt"] * buy_fee + e["size_usdt"] * sell_fee
            fees += qty_buy * close_buy_bid * buy_fee + qty_sell * close_sell_ask * sell_fee

        net = gross - fees

        # Спред между текущими ценами
        spread_now_pct = (close_buy_bid - close_sell_ask) / close_sell_ask * 100 if close_sell_ask > 0 else 0

        # Условие закрытия: спред сузился до порога И net > 0
        if spread_now_pct < AGG_CLOSE_THRESHOLD_PCT and net > 0:
            paper_db2.close_trade(
                trade_id=trade["id"],
                close_buy_bid=close_buy_bid,
                close_sell_ask=close_sell_ask,
                buy_fee=buy_fee,
                sell_fee=sell_fee,
                close_reason="converged",
            )
            continue

        # Крайний случай: спред инвертировался (стало выгодно наоборот) — тоже закрываем
        if spread_now_pct < -0.5:
            paper_db2.close_trade(
                trade_id=trade["id"],
                close_buy_bid=close_buy_bid,
                close_sell_ask=close_sell_ask,
                buy_fee=buy_fee,
                sell_fee=sell_fee,
                close_reason="inverted",
            )
            continue


# ==================================================
# ОТКРЫТИЕ / ДОБАВЛЕНИЕ
# ==================================================
def try_open_or_add_trades(spreads_store: Dict, quotes_store: Dict, exchange_fees: Dict):
    """Проходит по спредам и либо открывает новую сделку, либо добавляет к существующей."""
    if not spreads_store:
        return

    # Отладка: топ-3 спреда для AGG
    sorted_top = sorted(spreads_store.items(), key=lambda x: x[1].get("spread_pct", 0), reverse=True)[:3]
    top3 = " | ".join(f"{p}:{d.get('spread_pct', 0):.3f}%" for p, d in sorted_top)
    print(f"📊 AGG top-3: {top3}", flush=True)

    open_trades = paper_db2.get_open_trades()
    open_by_pair = {t["pair"]: t for t in open_trades}

    if len(open_trades) >= AGG_MAX_OPEN:
        return

    # Сортируем по спреду (сначала самые высокие)
    sorted_spreads = sorted(
        spreads_store.items(),
        key=lambda x: x[1].get("spread_pct", 0),
        reverse=True,
    )

    # Первая часть — обрабатываем существующие открытые сделки (добавления)
    for trade in open_trades:
        pair = trade["pair"]
        if pair not in spreads_store:
            continue

        data = spreads_store[pair]
        current_spread = data.get("spread_pct", 0)

        # Обновляем max_spread_seen
        if current_spread > trade["max_spread_seen"]:
            paper_db2.update_max_spread(trade["id"], current_spread)

        # Проверка на добавление: спред вырос на ADD_THRESHOLD% от последнего
        last_add = trade["last_add_spread"]
        if last_add <= 0:
            continue

        growth_pct = (current_spread - last_add) / last_add * 100

        # Добавляем если:
        # 1. Спред вырос на +AGG_ADD_THRESHOLD_PCT% ИЛИ на AGG_ADD_STEP_PCT абсолютно
        # 2. Не превысили лимит добавлений
        should_add = (
            growth_pct >= AGG_ADD_THRESHOLD_PCT or
            (current_spread - last_add) >= AGG_ADD_STEP_PCT
        )

        if not should_add:
            continue
        if trade["additions_count"] >= AGG_MAX_ADDITIONS:
            continue

        buy_ex = data.get("min_ask_exchange")
        sell_ex = data.get("max_bid_exchange")
        if not buy_ex or not sell_ex or buy_ex == sell_ex:
            continue

        # Проверяем, что биржи совпадают с исходными
        if buy_ex != trade["buy_exchange"] or sell_ex != trade["sell_exchange"]:
            # Если биржи сменились — пропускаем (чтобы не накапливать в другом направлении)
            continue

        buy_q = _get_quote(quotes_store, buy_ex, pair)
        sell_q = _get_quote(quotes_store, sell_ex, pair)
        if not buy_q or not sell_q:
            continue

        real_spread = _current_spread(buy_q["ask"], sell_q["bid"])

        paper_db2.add_to_trade(
            trade_id=trade["id"],
            buy_price=buy_q["ask"],
            sell_price=sell_q["bid"],
            size_usdt=AGG_TRADE_SIZE_USDT,
            spread_pct=real_spread,
        )

    # Вторая часть — открываем НОВЫЕ сделки по парам без открытых
    for pair, data in sorted_spreads:
        if len(paper_db2.get_open_trades()) >= AGG_MAX_OPEN:
            break

        if pair in open_by_pair:
            continue

        spread_pct = data.get("spread_pct", 0)
        if spread_pct < AGG_THRESHOLD_PCT:
            continue

        net_profit = data.get("net_profit_usdt", 0)
        if net_profit <= 0:
            print(f"   ⚠️ AGG {pair}: net_profit={net_profit:.4f} <= 0", flush=True)
            continue

        buy_ex = data.get("min_ask_exchange")
        sell_ex = data.get("max_bid_exchange")
        if not buy_ex or not sell_ex or buy_ex == sell_ex:
            print(f"   ⚠️ AGG {pair}: buy_ex={buy_ex} sell_ex={sell_ex}", flush=True)
            continue

        buy_q = _get_quote(quotes_store, buy_ex, pair)
        sell_q = _get_quote(quotes_store, sell_ex, pair)
        if not buy_q or not sell_q:
            continue

        if not _check_max_age(buy_q) or not _check_max_age(sell_q):
            continue

        buy_price = buy_q["ask"]
        sell_price = sell_q["bid"]
        real_spread = _current_spread(buy_price, sell_price)

        if real_spread < AGG_THRESHOLD_PCT:
            continue

        # Проверка комиссий
        buy_fee = exchange_fees.get(buy_ex, 0.0005)
        sell_fee = exchange_fees.get(sell_ex, 0.0005)
        est_profit = (sell_price * (1 - sell_fee) - buy_price * (1 + buy_fee)) * (AGG_TRADE_SIZE_USDT / buy_price)
        if est_profit <= 0:
            continue

        paper_db2.open_trade(
            pair=pair,
            buy_exchange=buy_ex,
            sell_exchange=sell_ex,
            buy_price=buy_price,
            sell_price=sell_price,
            size_usdt=AGG_TRADE_SIZE_USDT,
            spread_pct=real_spread,
        )


def run(spreads_store: Dict, quotes_store: Dict, exchange_fees: Dict):
    """Одна итерация агрессивной стратегии — только ОТКРЫТИЕ/ДОБАВЛЕНИЕ."""
    try:
        try_open_or_add_trades(spreads_store, quotes_store, exchange_fees)
    except Exception as e:
        print(f"❌ AGG trader error: {e}", flush=True)


def close_loop(quotes_store: Dict, exchange_fees: Dict):
    """Закрытие — вызывается из отдельного цикла каждые 10 сек."""
    try:
        try_close_trades(quotes_store, exchange_fees)
    except Exception as e:
        print(f"❌ AGG close error: {e}", flush=True)
