"""
Движок мульти-стратегий.
Проходит по всем 49 стратегиям, для каждой проверяет:
- Нужно ли ЗАКРЫТЬ открытые сделки (converged/close_pct)
- Нужно ли ДОБАВИТЬ к позициям (спред вырос)
- Нужно ли ОТКРЫТЬ новые сделки (спред ≥ entry_pct)
"""
import time
from typing import Dict, Optional

import multi_db
from strategies import STRATEGIES, VIRTUAL_BALANCE, ADD_STEP_PCT, MAX_OPEN_PER_STRATEGY


# Анти-спам: последняя сделка по паре у стратегии (защита от переоткрытия)
# {(sid, pair): last_closed_at_ms}
_last_closed: Dict[tuple, int] = {}

# Минимум секунд между сделками по одной паре у одной стратегии
PAIR_COOLDOWN_SEC = 300  # 5 минут


def _get_quote(quotes_store: Dict, exchange: str, pair: str) -> Optional[Dict]:
    key = (exchange, pair)
    if key in quotes_store:
        q = quotes_store[key]
        return {"bid": q.bid, "ask": q.ask, "ts": q.ts}
    return None


def _check_max_age(q: Dict, max_age_ms: int = 300000) -> bool:
    """Котировка свежая (по умолчанию 5 минут)."""
    if not q:
        return False
    age = int(time.time() * 1000) - q["ts"]
    return age < max_age_ms


def _current_spread(buy_price: float, sell_price: float) -> float:
    if buy_price <= 0:
        return 0.0
    return (sell_price - buy_price) / buy_price * 100


def _cooldown_ok(sid: int, pair: str) -> bool:
    """Прошло ли достаточно времени с последнего закрытия сделки по паре у стратегии."""
    key = (sid, pair)
    last = _last_closed.get(key, 0)
    return (int(time.time() * 1000) - last) > PAIR_COOLDOWN_SEC * 1000


def close_strategy_trades(sid: int, strategy: dict, quotes_store: Dict, exchange_fees: Dict):
    """Закрывает сделки стратегии по её параметрам."""
    open_trades = multi_db.get_open_trades(sid)
    if not open_trades:
        return

    close_pct = strategy["close_pct"]

    for trade in open_trades:
        pair = trade["pair"]
        buy_ex = trade["buy_exchange"]
        sell_ex = trade["sell_exchange"]

        buy_q = _get_quote(quotes_store, buy_ex, pair)
        sell_q = _get_quote(quotes_store, sell_ex, pair)
        if not buy_q or not sell_q:
            continue

        close_buy_bid = buy_q["bid"]
        close_sell_ask = sell_q["ask"]

        # Считаем текущий спред между ценами (в сторону, в которую мы открылись)
        # "Сужение спреда" = цена на buy-бирже выросла ИЛИ на sell-бирже упала
        current_spread_pct = (close_buy_bid - close_sell_ask) / close_sell_ask * 100 if close_sell_ask > 0 else 0

        # Считаем предполагаемый net
        entries = trade["entries"]
        gross = 0.0
        for e in entries:
            qty_buy = e["size_usdt"] / e["price_buy"]
            qty_sell = e["size_usdt"] / e["price_sell"]
            gross += (close_buy_bid - e["price_buy"]) * qty_buy
            gross += (e["price_sell"] - close_sell_ask) * qty_sell

        buy_fee = exchange_fees.get(buy_ex, 0.0005)
        sell_fee = exchange_fees.get(sell_ex, 0.0005)
        fees = 0.0
        for e in entries:
            qty_buy = e["size_usdt"] / e["price_buy"]
            qty_sell = e["size_usdt"] / e["price_sell"]
            fees += e["size_usdt"] * buy_fee + e["size_usdt"] * sell_fee
            fees += qty_buy * close_buy_bid * buy_fee + qty_sell * close_sell_ask * sell_fee

        net = gross - fees

        # Условие закрытия:
        # 1. current_spread_pct <= close_pct (например, < 0.15 или < 0 или < -0.1)
        # 2. net > 0 (прибыль)
        should_close = False
        if current_spread_pct <= close_pct and net > 0:
            should_close = True

        if should_close:
            result = multi_db.close_trade(
                trade_id=trade["id"],
                close_buy_bid=close_buy_bid,
                close_sell_ask=close_sell_ask,
                buy_fee=buy_fee,
                sell_fee=sell_fee,
                close_reason="converged",
            )
            _last_closed[(sid, pair)] = int(time.time() * 1000)
            print(f"🟢 S{sid} закрыта #{trade['id']}: {pair} net={result.get('net', 0):+.4f} ({strategy['name']})", flush=True)


def open_and_add_strategy(sid: int, strategy: dict, spreads_store: Dict, quotes_store: Dict, exchange_fees: Dict):
    """Открывает новые сделки и добавляет к существующим для стратегии."""

    entry_pct = strategy["entry_pct"]
    add_threshold = strategy["add_threshold_pct"]
    size_usdt = strategy["size_usdt"]
    max_add = strategy["max_additions"]
    allowed = strategy.get("allowed_exchanges", [])

    balance = multi_db.get_balance(sid)
    if balance < size_usdt:
        return

    open_trades = multi_db.get_open_trades(sid)
    open_by_pair = {t["pair"]: t for t in open_trades}

    # === ПЕРЕСЧИТЫВАЕМ СПРЕД ТОЛЬКО ПО ALLOWED БИРЖАМ ===
    filtered_spreads = {}
    for pair in spreads_store.keys():
        # Собираем котировки только с разрешённых бирж
        exchanges_data = []
        for ex in allowed:
            key = (ex, pair)
            if key in quotes_store:
                q = quotes_store[key]
                if q.bid > 0 and q.ask > 0:
                    exchanges_data.append((q.bid, q.ask, ex))
        
        if len(exchanges_data) < 2:
            continue
        
        # Находим max_bid и min_ask среди разрешённых бирж
        max_bid, _, max_bid_ex = max(exchanges_data, key=lambda x: x[0])
        min_ask, _, min_ask_ex = min(exchanges_data, key=lambda x: x[1])
        
        if max_bid <= 0 or min_ask <= 0 or max_bid_ex == min_ask_ex:
            continue
        
        # Проверка выбросов (как в основной логике)
        mid = (max_bid + min_ask) / 2
        spread_usd = max_bid - min_ask
        spread_pct = spread_usd / mid * 100 if mid > 0 else 0
        
        # Фильтр выбросов — спред не больше 5% (защита от битых данных)
        if spread_pct > 5.0 or spread_pct < 0:
            continue
        
        # Оценка прибыли на размер сделки
        buy_fee = exchange_fees.get(min_ask_ex, 0.0005)
        sell_fee = exchange_fees.get(max_bid_ex, 0.0005)
        qty = size_usdt / min_ask
        gross = qty * (max_bid - min_ask)
        fees = qty * min_ask * buy_fee + qty * max_bid * sell_fee
        net_profit = gross - fees
        
        filtered_spreads[pair] = {
            "spread_pct": spread_pct,
            "spread_usd": spread_usd,
            "min_ask": min_ask,
            "max_bid": max_bid,
            "min_ask_exchange": min_ask_ex,
            "max_bid_exchange": max_bid_ex,
            "net_profit_usdt": net_profit,
        }
    
    # Сортируем по убыванию
    sorted_spreads = sorted(
        filtered_spreads.items(),
        key=lambda x: x[1].get("spread_pct", 0),
        reverse=True,
    )
    


    # === 1. Обработка добавлений к существующим ===
    for trade in open_trades:
        pair = trade["pair"]
        if pair not in spreads_store:
            continue

        data = spreads_store[pair]
        current_spread = data.get("spread_pct", 0)

        # Обновляем max
        if current_spread > trade["max_spread_seen"]:
            multi_db.update_max_spread(trade["id"], current_spread)

        last_add = trade["last_add_spread"]
        if last_add <= 0:
            continue

        growth_pct = (current_spread - last_add) / last_add * 100

        should_add = (
            growth_pct >= add_threshold or
            (current_spread - last_add) >= ADD_STEP_PCT
        )
        if not should_add:
            continue
        if trade["additions_count"] >= max_add:
            continue

        # Проверяем, что биржи не сменились
        buy_ex = data.get("min_ask_exchange")
        sell_ex = data.get("max_bid_exchange")
        if not buy_ex or not sell_ex or buy_ex == sell_ex:
            continue
        if buy_ex != trade["buy_exchange"] or sell_ex != trade["sell_exchange"]:
            continue

        # Проверяем баланс
        if multi_db.get_balance(sid) < size_usdt:
            continue

        buy_q = _get_quote(quotes_store, buy_ex, pair)
        sell_q = _get_quote(quotes_store, sell_ex, pair)
        if not buy_q or not sell_q:
            continue

        real_spread = _current_spread(buy_q["ask"], sell_q["bid"])
        if real_spread < entry_pct:
            continue

        multi_db.add_to_trade(
            trade_id=trade["id"],
            buy_price=buy_q["ask"],
            sell_price=sell_q["bid"],
            size_usdt=size_usdt,
            spread_pct=real_spread,
        )
        # Списываем с баланса
        multi_db.update_balance(sid, -size_usdt)
        print(f"➕ S{sid} добавление к #{trade['id']}: {pair} +${size_usdt} spread={real_spread:.3f}%", flush=True)

    # === 2. Открытие новых сделок ===
    for pair, data in sorted_spreads:
        # Лимит открытых на стратегию
        if multi_db.get_open_count(sid) >= MAX_OPEN_PER_STRATEGY:
            break
        if pair in open_by_pair:
            continue

        spread_pct = data.get("spread_pct", 0)
        if spread_pct < entry_pct:
            continue

        net_profit = data.get("net_profit_usdt", 0)
        if net_profit <= 0:
            print(f"⚠️ S{sid} {pair}: net_profit={net_profit} <= 0", flush=True)
            continue

        if not _cooldown_ok(sid, pair):
            print(f"⚠️ S{sid} {pair}: cooldown", flush=True)
            continue

        buy_ex = data.get("min_ask_exchange")
        sell_ex = data.get("max_bid_exchange")
        if not buy_ex or not sell_ex or buy_ex == sell_ex:
            continue

        # === ФИЛЬТР ПО РАЗРЕШЁННЫМ БИРЖАМ ===
        allowed = strategy.get("allowed_exchanges")
        if allowed:
            if buy_ex not in allowed or sell_ex not in allowed:
                print(f"⚠️ S{sid} {pair}: биржа не разрешена ({buy_ex}/{sell_ex})", flush=True)
                continue

            max_per_ex = strategy.get("per_exchange_max_open", 999)
            cnt_buy = multi_db.get_open_count_by_exchange(sid, buy_ex)
            cnt_sell = multi_db.get_open_count_by_exchange(sid, sell_ex)
            if cnt_buy >= max_per_ex:
                print(f"⚠️ S{sid} {pair}: лимит биржи {buy_ex} ({cnt_buy}/{max_per_ex})", flush=True)
                continue
            if cnt_sell >= max_per_ex:
                print(f"⚠️ S{sid} {pair}: лимит биржи {sell_ex} ({cnt_sell}/{max_per_ex})", flush=True)
                continue

        buy_q = _get_quote(quotes_store, buy_ex, pair)
        sell_q = _get_quote(quotes_store, sell_ex, pair)
        if not buy_q or not sell_q:
            print(f"⚠️ S{sid} {pair}: нет котировок ({buy_ex}/{sell_ex})", flush=True)
            continue

        if not _check_max_age(buy_q) or not _check_max_age(sell_q):
            print(f"⚠️ S{sid} {pair}: старые котировки", flush=True)
            continue

        buy_price = buy_q["ask"]
        sell_price = sell_q["bid"]
        real_spread = _current_spread(buy_price, sell_price)
        if real_spread < entry_pct:
            print(f"⚠️ S{sid} {pair}: real_spread={real_spread:.3f}% < {entry_pct}% ({buy_ex}→{sell_ex})", flush=True)
            continue

        # Проверка комиссий
        buy_fee = exchange_fees.get(buy_ex, 0.0005)
        sell_fee = exchange_fees.get(sell_ex, 0.0005)
        est_profit = (sell_price * (1 - sell_fee) - buy_price * (1 + buy_fee)) * (size_usdt / buy_price)
        if est_profit <= 0:
            print(f"⚠️ S{sid} {pair}: est_profit={est_profit:.4f} <= 0", flush=True)
            continue

        # Проверка баланса
        if multi_db.get_balance(sid) < size_usdt:
            break

        trade_id = multi_db.open_trade(
            sid=sid,
            pair=pair,
            buy_exchange=buy_ex,
            sell_exchange=sell_ex,
            buy_price=buy_price,
            sell_price=sell_price,
            size_usdt=size_usdt,
            spread_pct=real_spread,
        )
        # Списываем с баланса
        multi_db.update_balance(sid, -size_usdt)
        print(f"📈 S{sid} открыта #{trade_id}: {pair} {buy_ex}→{sell_ex} spread={real_spread:.3f}% (${size_usdt})", flush=True)


def run(spreads_store: Dict, quotes_store: Dict, exchange_fees: Dict):
    """Одна итерация по всем стратегиям — открытие/добавление."""
    for sid, strategy in STRATEGIES.items():
        try:
            open_and_add_strategy(sid, strategy, spreads_store, quotes_store, exchange_fees)
        except Exception as e:
            print(f"❌ S{sid} open error: {e}", flush=True)


def close_loop(quotes_store: Dict, exchange_fees: Dict):
    """Закрытие сделок по всем стратегиям — вызывается часто."""
    for sid, strategy in STRATEGIES.items():
        try:
            close_strategy_trades(sid, strategy, quotes_store, exchange_fees)
        except Exception as e:
            print(f"❌ S{sid} close error: {e}", flush=True)


def init_strategies():
    """Регистрирует все стратегии в БД."""
    for sid, strategy in STRATEGIES.items():
        balance = strategy.get("initial_balance", VIRTUAL_BALANCE)
        multi_db.register_strategy(sid, strategy["name"], balance)
    print(f"✅ Зарегистрировано {len(STRATEGIES)} стратегий")


if __name__ == "__main__":
    from strategies import STRATEGIES
    multi_db.init_db()
    init_strategies()
    print()
    print("Проверка:")
    for sid in [1, 25, 49]:
        s = multi_db.get_strategy_stats(sid)
        print(f"  S{sid}: {s.get('name')} balance=${s.get('balance')}")
