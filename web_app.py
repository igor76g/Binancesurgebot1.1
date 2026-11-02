"""SurgeBot с веб-интерфейсом: работает на Android (Termux), Windows, macOS, Linux.
Запуск: python web_app.py  ->  откройте в браузере http://127.0.0.1:8765
Сервер слушает только локальный адрес устройства."""
import json
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from broker import DemoBroker, LiveBroker
from engine import Engine, Runner
from exchange import ApiError, FuturesApi, MAINNET, TESTNET
from settings import Settings, load_settings, save_settings, validate
from dataclasses import asdict

PORT = 8765
SCHEMA = [
    ("Отбор монет", [
        ("min_volume_24h_usdt", "Мин. оборот за 24 ч, USDT"), ("vol_mult", "Рост объёма, раз"), ("natr_mult", "Рост NATR, раз"),
        ("min_natr", "Мин. NATR, %"), ("surge_minutes", "Окно, минут"), ("baseline_hours", "База сравнения, часов"),
        ("scan_interval_min", "Проверять каждые, мин"), ("watch_minutes", "Держать в наблюдении, мин")]),
    ("Вход и выход", [
        ("rr", "Тейк: R (2 = 1:2)"), ("stop_pad_pct", "Отступ стопа, %"), ("min_stop_pct", "Мин. стоп, %"), ("max_stop_pct", "Макс. стоп, %"),
        ("max_chase_pct", "Макс. проскальзывание входа, %"), ("cooldown_min", "Пауза по монете, мин"),
        ("max_hold_min", "Макс. время в сделке, мин"), ("poll_sec", "Проверка цены, сек")]),
    ("Риск", [
        ("risk_percent", "Риск на сделку, % баланса"), ("risk_fixed_usdt", "Или фикс. риск, USDT (0 = выкл.)"), ("leverage", "Плечо"),
        ("max_open_positions", "Макс. сделок одновременно"), ("max_daily_loss_percent", "Дневной лимит убытка, %")]),
    ("Демо", [
        ("demo_balance", "Виртуальный баланс, USDT"), ("demo_slippage_pct", "Проскальзывание, %"),
        ("taker_fee_pct", "Комиссия тейкера, %"), ("maker_fee_pct", "Комиссия мейкера, %")]),
]


class Controller:
    def __init__(self, make_api=None):
        self.st = load_settings()
        self.logs = deque(maxlen=300)
        self.engine = self.runner = None
        self.make_api = make_api or (lambda base, k, s: FuturesApi(base, k, s))
        self.lock = threading.Lock()

    def log(self, m):
        self.logs.append(f"{datetime.now():%H:%M:%S}  {m}")

    def settings_json(self):
        d = asdict(self.st)
        d["api_key"] = "***" if self.st.api_key else ""
        d["api_secret"] = "***" if self.st.api_secret else ""
        return {"values": d, "schema": [{"group": g, "fields": [{"key": k, "label": l} for k, l in f]} for g, f in SCHEMA]}

    def apply(self, data):
        s = Settings(**asdict(self.st))
        for _, fields in SCHEMA:
            for k, _l in fields:
                if k in data:
                    cur = getattr(s, k)
                    setattr(s, k, type(cur)(float(str(data[k]).replace(",", "."))) if isinstance(cur, int) else float(str(data[k]).replace(",", ".")))
        s.mode = data.get("mode", s.mode)
        s.testnet = bool(data.get("testnet", s.testnet))
        s.remember_keys = bool(data.get("remember_keys", s.remember_keys))
        for k in ("api_key", "api_secret"):
            v = str(data.get(k, "")).strip()
            if v and v != "***":
                setattr(s, k, v)
        self.st = validate(s)
        save_settings(self.st)
        self.log("Настройки сохранены" + (" (применятся при следующем старте)" if self.runner else ""))

    def start(self):
        with self.lock:
            if self.runner:
                return "уже запущен"
            st = self.st
            try:
                if st.mode == "live":
                    if not st.api_key or not st.api_secret:
                        return "Введите API Key и Secret"
                    api = self.make_api(TESTNET if st.testnet else MAINNET, st.api_key, st.api_secret)
                    broker = LiveBroker(st, api, self.log)
                    self.log(f"Подключено. Баланс USDT: {broker.check_start():.2f}")
                else:
                    api = self.make_api(MAINNET, "", "")
                    broker = DemoBroker(st)
            except ApiError as e:
                return str(e)
            self.engine = Engine(st, api, broker, self.log)
            self.runner = Runner(self.engine)
            self.runner.start()
            self.log(f"Старт, режим: {st.mode}")
            return ""

    def stop(self, close_positions=False):
        with self.lock:
            if not self.runner:
                return
            self.runner.stop()
            self.runner.join(timeout=3)
            e = self.engine
            if close_positions and e.broker.positions:
                try:
                    for tr in e.broker.close_all(e.api.prices(), int(time.time() * 1000)):
                        e._record(tr)
                except ApiError as ex:
                    self.log(f"Ошибка закрытия: {ex}")
            self.runner = None
            self.log("Остановлен")

    def state(self):
        e = self.engine
        base = {"running": bool(self.runner), "logs": list(self.logs)[-100:]}
        if not e:
            return base
        eq = e.broker.equity() if e.broker.mode == "demo" else (e.day_start_equity or 0) + e.day_pnl
        base.update({
            "mode": e.broker.mode, "equity": round(eq, 2), "day_pnl": round(e.day_pnl, 2), "halted": e.halted,
            "watch": [{**w, "until": datetime.fromtimestamp(w["until"] / 1000).strftime("%H:%M")} for w in e.snapshot()],
            "positions": [{k: p[k] for k in ("symbol", "entry", "stop", "tp", "qty")} | {"risk": round(p["risk"], 2)}
                          for p in list(e.broker.positions.values())],
            "scan": [{**r, "qv24": round(r["qv24"] / 1e6)} for r in e.last_scan["rows"][:15]],
            "trades": [{**t, "time": datetime.fromtimestamp(t["t_close"] / 1000).strftime("%d.%m %H:%M"), "pnl": round(t["pnl"], 2), "r": round(t["r"], 2)}
                       for t in reversed(e.trades[-100:])],
        })
        return base




def make_handler(ctl, page):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/state":
                self._send(200, ctl.state())
            elif self.path == "/settings":
                self._send(200, ctl.settings_json())
            else:
                self._send(200, page, "text/html")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                data = {}
            try:
                if self.path == "/settings":
                    ctl.apply(data)
                    self._send(200, {"ok": True})
                elif self.path == "/start":
                    err = ctl.start()
                    self._send(200, {"ok": not err, "error": err})
                elif self.path == "/stop":
                    ctl.stop(bool(data.get("close")))
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "not found"})
            except (ValueError, TypeError) as e:
                self._send(400, {"ok": False, "error": f"Неверное значение: {e}"})
    return H


def serve(ctl, port=PORT, page=None):
    import os
    page = page or open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html"), encoding="utf-8").read()
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(ctl, page))
    return srv


if __name__ == "__main__":
    ctl = Controller()
    srv = serve(ctl)
    print(f"SurgeBot: откройте в браузере http://127.0.0.1:{PORT}  (Ctrl+C - выход)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        ctl.stop()
