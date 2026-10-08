"""
Конфиг 4 финальных стратегий с лимитами по биржам.
- Баланс: $1750 на стратегию
- Только 5 бирж: gateio, bitget, bingx, okx, binance
- $350 на биржу = 7 сделок по $50 или 5 сделок по $70
- БЕЗ добавлений
"""

ALLOWED_EXCHANGES = ["gateio", "bitget", "bingx", "okx", "binance"]

STRATEGIES = {
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
        "per_exchange_max_open": 7,   # 350 / 50 = 7
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
        "per_exchange_max_open": 5,   # 350 / 70 = 5
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
}

VIRTUAL_BALANCE = 1750
ADD_STEP_PCT = 0.1
MAX_OPEN_PER_STRATEGY = 35   # 5 бирж × 7 сделок = максимум 35 на стратегию


if __name__ == "__main__":
    print(f"Всего стратегий: {len(STRATEGIES)}")
    print()
    for sid, s in STRATEGIES.items():
        print(f"  #{sid}: {s['name']} | balance ${s['initial_balance']} | max/биржа {s['per_exchange_max_open']} | ${s['per_exchange_balance']}/биржа")
