"""
Конфиг 8 финальных стратегий.
- Баланс: $1750 на стратегию
- Только 5 бирж: gateio, bitget, bingx, okx, binance
- $350 на биржу = лимит по размеру ставки
- БЕЗ добавлений
"""

ALLOWED_EXCHANGES = ["gateio", "bitget", "bingx", "okx", "binance"]

STRATEGIES = {
    # === СТАРЫЕ 4 (с $50/$70) ===
    1: {
        "name": "E0.4_X0.15_S50",
        "entry_pct": 0.4,
        "close_pct": 0.15,
        "add_threshold_pct": 9999,
        "size_usdt": 50,
        "max_additions": 0,
        "initial_balance": 1750,
        "allowed_exchanges": ALLOWED_EXCHANGES,
        "per_exchange_balance": 350,
        "per_exchange_max_open": 7,
    },
    2: {
        "name": "E0.4_X0.15_S70",
        "entry_pct": 0.4,
        "close_pct": 0.15,
        "add_threshold_pct": 9999,
        "size_usdt": 70,
        "max_additions": 0,
        "initial_balance": 1750,
        "allowed_exchanges": ALLOWED_EXCHANGES,
        "per_exchange_balance": 350,
        "per_exchange_max_open": 5,
    },
    3: {
        "name": "E0.5_X0.15_S50",
        "entry_pct": 0.5,
        "close_pct": 0.15,
        "add_threshold_pct": 9999,
        "size_usdt": 50,
        "max_additions": 0,
        "initial_balance": 1750,
        "allowed_exchanges": ALLOWED_EXCHANGES,
        "per_exchange_balance": 350,
        "per_exchange_max_open": 7,
    },
    4: {
        "name": "E0.5_X0.15_S70",
        "entry_pct": 0.5,
        "close_pct": 0.15,
        "add_threshold_pct": 9999,
        "size_usdt": 70,
        "max_additions": 0,
        "initial_balance": 1750,
        "allowed_exchanges": ALLOWED_EXCHANGES,
        "per_exchange_balance": 350,
        "per_exchange_max_open": 5,
    },
    # === НОВЫЕ 4 (с $100/$150) ===
    5: {
        "name": "E0.4_X0.15_S150",
        "entry_pct": 0.4,
        "close_pct": 0.15,
        "add_threshold_pct": 9999,
        "size_usdt": 150,
        "max_additions": 0,
        "initial_balance": 1750,
        "allowed_exchanges": ALLOWED_EXCHANGES,
        "per_exchange_balance": 350,
        "per_exchange_max_open": 2,   # 350 / 150 = 2
    },
    6: {
        "name": "E0.4_X0.15_S100",
        "entry_pct": 0.4,
        "close_pct": 0.15,
        "add_threshold_pct": 9999,
        "size_usdt": 100,
        "max_additions": 0,
        "initial_balance": 1750,
        "allowed_exchanges": ALLOWED_EXCHANGES,
        "per_exchange_balance": 350,
        "per_exchange_max_open": 3,   # 350 / 100 = 3
    },
    7: {
        "name": "E0.5_X0.15_S150",
        "entry_pct": 0.5,
        "close_pct": 0.15,
        "add_threshold_pct": 9999,
        "size_usdt": 150,
        "max_additions": 0,
        "initial_balance": 1750,
        "allowed_exchanges": ALLOWED_EXCHANGES,
        "per_exchange_balance": 350,
        "per_exchange_max_open": 2,
    },
    8: {
        "name": "E0.5_X0.15_S100",
        "entry_pct": 0.5,
        "close_pct": 0.15,
        "add_threshold_pct": 9999,
        "size_usdt": 100,
        "max_additions": 0,
        "initial_balance": 1750,
        "allowed_exchanges": ALLOWED_EXCHANGES,
        "per_exchange_balance": 350,
        "per_exchange_max_open": 3,
    },
}

VIRTUAL_BALANCE = 1750
ADD_STEP_PCT = 0.1
MAX_OPEN_PER_STRATEGY = 35


if __name__ == "__main__":
    print(f"Всего стратегий: {len(STRATEGIES)}")
    print()
    for sid, s in STRATEGIES.items():
        print(f"  S{sid}: {s['name']:<22} | entry {s['entry_pct']}% | close {s['close_pct']}% | ${s['size_usdt']}/сделку | макс/биржа {s['per_exchange_max_open']}")
