"""Настройки приложения и их хранение (%APPDATA%\\SurgeBot\\settings.json)."""
import json
import os
from dataclasses import dataclass, fields, asdict

import sys


def _app_dir():
    if os.environ.get("ANDROID_PRIVATE"):          # приватная папка приложения на Android
        return os.path.join(os.environ["ANDROID_PRIVATE"], "SurgeBot")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "SurgeBot")
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "SurgeBot")


APP_DIR = _app_dir()
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")


@dataclass
class Settings:
    # режим: demo — виртуальные деньги на реальных ценах, live — настоящие ордера
    mode: str = "demo"
    testnet: bool = False            # только для live: тестовая сеть Binance Futures
    api_key: str = ""
    api_secret: str = ""
    remember_keys: bool = False      # хранить ключи в файле настроек открытым текстом

    # --- отбор монет (проверка каждые scan_interval_min минут) ---
    min_volume_24h_usdt: float = 15_000_000.0   # оборот за 24 часа, USDT
    vol_mult: float = 4.0                       # объём за окно выросло минимум в N раз
    natr_mult: float = 4.0                      # NATR за окно вырос минимум в N раз
    min_natr: float = 0.7                       # NATR за окно не менее, %
    scan_interval_min: float = 5.0
    surge_minutes: int = 15                     # окно «последние N минут»
    baseline_hours: float = 6.0                 # с чем сравниваем: средние предыдущие N часов
    watch_minutes: int = 60                     # сколько держим отобранную монету под наблюдением
    max_symbols_scan: int = 300

    # --- вход и выход ---
    poll_sec: float = 3.0                       # период проверки цены
    stop_pad_pct: float = 0.05                  # стоп ниже нижнего экстремума на столько %
    min_stop_pct: float = 0.3                   # стоп ближе — сделку пропускаем
    max_stop_pct: float = 10.0                  # стоп дальше — пропускаем
    max_chase_pct: float = 0.5                  # не входим, если цена выше уровня входа более чем на %
    rr: float = 2.0                             # тейк 1 к 2
    cooldown_min: int = 30                      # пауза по монете после сделки
    max_hold_min: int = 240                     # закрыть по рынку, если сделка висит дольше

    # --- риск ---
    risk_percent: float = 1.0                   # риск на сделку, % от баланса
    risk_fixed_usdt: float = 0.0                # если > 0, риск в USDT вместо процентов
    leverage: int = 5
    max_open_positions: int = 3
    max_daily_loss_percent: float = 5.0         # после такого дневного убытка новых входов нет (до 00:00 UTC)

    # --- демо ---
    demo_balance: float = 1000.0
    demo_slippage_pct: float = 0.05
    taker_fee_pct: float = 0.05
    maker_fee_pct: float = 0.02


def validate(s: Settings) -> Settings:
    s.mode = s.mode if s.mode in ("demo", "live") else "demo"
    s.vol_mult = max(1.0, float(s.vol_mult))
    s.natr_mult = max(1.0, float(s.natr_mult))
    s.min_natr = max(0.0, float(s.min_natr))
    s.min_volume_24h_usdt = max(0.0, float(s.min_volume_24h_usdt))
    s.scan_interval_min = max(1.0, float(s.scan_interval_min))
    s.surge_minutes = max(3, int(s.surge_minutes))
    s.baseline_hours = max(1.0, min(12.0, float(s.baseline_hours)))
    s.watch_minutes = max(5, int(s.watch_minutes))
    s.poll_sec = max(2.0, float(s.poll_sec))
    s.rr = max(0.5, float(s.rr))
    s.min_stop_pct = max(0.05, float(s.min_stop_pct))
    s.max_stop_pct = max(s.min_stop_pct, float(s.max_stop_pct))
    s.risk_percent = max(0.05, min(5.0, float(s.risk_percent)))
    s.risk_fixed_usdt = max(0.0, float(s.risk_fixed_usdt))
    s.leverage = max(1, min(20, int(s.leverage)))
    s.max_open_positions = max(1, int(s.max_open_positions))
    s.max_daily_loss_percent = max(0.5, float(s.max_daily_loss_percent))
    s.demo_balance = max(10.0, float(s.demo_balance))
    return s


def load_settings(path: str = SETTINGS_PATH) -> Settings:
    s = Settings()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        names = {f.name for f in fields(Settings)}
        for k, v in data.items():
            if k in names:
                setattr(s, k, v)
    except (OSError, ValueError):
        pass
    try:
        return validate(s)
    except (TypeError, ValueError):
        return validate(Settings())


def save_settings(s: Settings, path: str = SETTINGS_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    d = asdict(s)
    if not s.remember_keys:
        d["api_key"] = ""
        d["api_secret"] = ""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
