"""Движок: отбор монет раз в N минут, наблюдение за сетапом, вход/выход через брокера."""
import threading
import time

from broker import calc_qty, floor_to
from exchange import ApiError, RateLimit
from scanner import surge_metrics, passes
from strategy import find_setup


def utc_day(ms):
    return int(ms // 86_400_000)


class Engine:
    def __init__(self, st, api, broker, log_fn=print):
        self.st, self.api, self.broker, self.log = st, api, broker, log_fn
        self.info = {}
        self.watch = {}          # symbol -> dict
        self.cooldown = {}       # symbol -> ms до которого не торгуем
        self.trades = []
        self.last_scan = {"t": 0, "checked": 0, "passed": 0, "rows": []}
        self.day = None
        self.day_start_equity = None
        self.day_pnl = 0.0
        self.halted = False
        self._minute_cache = {}

    # ---------- служебное ----------
    def _roll_day(self, now_ms):
        d = utc_day(now_ms)
        if d != self.day:
            self.day, self.day_pnl, self.halted = d, 0.0, False
            try:
                self.day_start_equity = self.broker.equity()
            except ApiError as e:
                self.day_start_equity = self.day_start_equity or self.st.demo_balance
                self.log(f"Не удалось получить баланс: {e}")

    def _record(self, tr):
        self.trades.append(tr)
        self.day_pnl += tr["pnl"]
        self.cooldown[tr["symbol"]] = tr["t_close"] + self.st.cooldown_min * 60000
        self.log(f"Закрыта {tr['symbol']} [{tr['reason']}] вход {tr['entry']} выход {tr['exit']} "
                 f"PnL {tr['pnl']:+.2f} USDT ({tr['r']:+.2f}R)")
        lim = (self.day_start_equity or 0) * self.st.max_daily_loss_percent / 100
        if lim and self.day_pnl <= -lim and not self.halted:
            self.halted = True
            self.log(f"Дневной лимит убытка достигнут ({self.day_pnl:.2f}). Новых входов не будет до 00:00 UTC.")

    def ensure_info(self):
        if not self.info:
            self.info = self.api.exchange_info()
            self.log(f"Загружено {len(self.info)} USDT-перпетуалов")

    # ---------- скан ----------
    def scan(self, now_ms):
        st = self.st
        self.ensure_info()
        self._roll_day(now_ms)
        tick = [t for t in self.api.tickers24h()
                if t["symbol"] in self.info and self.info[t["symbol"]]["trading"] and t["qv"] >= st.min_volume_24h_usdt]
        tick.sort(key=lambda t: -t["qv"])
        tick = tick[:st.max_symbols_scan]
        need = st.surge_minutes + int(st.baseline_hours * 60) + 2
        base_min = int(st.baseline_hours * 60)
        rows, passed = [], 0
        for t in tick:
            try:
                k = self.api.klines(t["symbol"], need)
            except RateLimit as e:
                self.log(str(e))
                time.sleep(min(e.wait, 60))
                break
            except ApiError as e:
                self.log(f"{t['symbol']}: {e}")
                continue
            # формирующуюся свечу отбрасываем
            for key in k:
                k[key] = k[key][:-1]
            m = surge_metrics(k["h"], k["l"], k["c"], k["qv"], st.surge_minutes, base_min)
            if m is None:
                continue
            ok = passes(m, t["qv"], st)
            rows.append({"symbol": t["symbol"], "qv24": t["qv"], **m, "ok": ok})
            if ok:
                passed += 1
                w = self.watch.get(t["symbol"])
                start = k["t"][-st.surge_minutes]
                if w is None:
                    self.watch[t["symbol"]] = {"start": start, "until": now_ms + st.watch_minutes * 60000,
                                               "metrics": m, "setup": None, "k_t": None, "skip_H": None}
                    self.log(f"Отобрана {t['symbol']}: объём x{m['vol_ratio']:.1f}, NATR x{m['natr_ratio']:.1f} "
                             f"(NATR {m['natr_now']:.2f}%)")
                else:
                    w["until"] = now_ms + st.watch_minutes * 60000
                    w["metrics"] = m
        rows.sort(key=lambda r: -r["vol_ratio"])
        self.last_scan = {"t": now_ms, "checked": len(tick), "passed": passed, "rows": rows[:30]}
        self.log(f"Скан: проверено {len(tick)}, прошло {passed}, под наблюдением {len(self.watch)}")

    # ---------- опрос цены и вход ----------
    def _refresh_setup(self, sym, w, now_ms):
        cur_min = now_ms // 60000
        if w.get("k_min") == cur_min:
            return
        k = self.api.klines(sym, 90)
        for key in k:
            k[key] = k[key][:-1]
        w["k_min"] = cur_min
        w["setup"] = find_setup(k["t"], k["o"], k["h"], k["l"], k["c"], w["start"])

    def poll(self, now_ms):
        st = self.st
        self.ensure_info()
        self._roll_day(now_ms)
        for sym in [s for s, w in self.watch.items() if w["until"] < now_ms]:
            del self.watch[sym]
        prices = self.api.prices()
        for tr in self.broker.manage(prices, now_ms):
            self._record(tr)
        if self.halted:
            return
        for sym, w in list(self.watch.items()):
            if sym in self.broker.positions or self.cooldown.get(sym, 0) > now_ms:
                continue
            if len(self.broker.positions) >= st.max_open_positions:
                break
            try:
                self._refresh_setup(sym, w, now_ms)
            except ApiError as e:
                self.log(f"{sym}: {e}")
                continue
            s, p = w["setup"], prices.get(sym)
            if s is None or p is None:
                continue
            info = self.info[sym]
            trigger = s.H + info["tick"]
            stop = floor_to(s.L * (1 - st.stop_pad_pct / 100), info["tick"])
            if p < trigger or w["skip_H"] == s.H:
                continue
            if p > trigger * (1 + st.max_chase_pct / 100):
                w["skip_H"] = s.H
                self.log(f"{sym}: пробой уже ушёл слишком далеко ({p} > {trigger}), пропуск")
                continue
            stop_pct = (p - stop) / p * 100
            if p <= stop or stop_pct < st.min_stop_pct or stop_pct > st.max_stop_pct:
                w["skip_H"] = s.H
                self.log(f"{sym}: стоп {stop_pct:.2f}% вне допустимого диапазона, пропуск")
                continue
            try:
                eq = self.broker.equity()
                qty, why = calc_qty(eq, p, stop, st, info)
                if qty is None:
                    w["skip_H"] = s.H
                    self.log(f"{sym}: пропуск — {why}")
                    continue
                pos = self.broker.open(sym, p, qty, stop, info, now_ms)
            except ApiError as e:
                self.log(f"{sym}: ошибка открытия: {e}")
                w["skip_H"] = s.H
                continue
            if pos is None:
                w["skip_H"] = s.H
                continue
            self.cooldown[sym] = now_ms + st.cooldown_min * 60000
            self.log(f"ВХОД {sym} [{self.broker.mode}]: цена {pos['entry']}, объём {pos['qty']}, "
                     f"стоп {pos['stop']} ({stop_pct:.2f}%), тейк {pos['tp']}, риск {pos['risk']:.2f} USDT")

    def snapshot(self):
        rows = []
        for sym, w in self.watch.items():
            s = w.get("setup")
            rows.append({"symbol": sym, "H": s.H if s else None, "L": s.L if s else None,
                         "vol_ratio": w["metrics"]["vol_ratio"], "natr_ratio": w["metrics"]["natr_ratio"],
                         "until": w["until"]})
        return rows


class Runner(threading.Thread):
    def __init__(self, engine):
        super().__init__(daemon=True)
        self.e = engine
        self.stop_ev = threading.Event()
        self.error = None

    def run(self):
        e, st = self.e, self.e.st
        next_scan = 0
        while not self.stop_ev.is_set():
            now = int(time.time() * 1000)
            try:
                if now >= next_scan:
                    e.scan(now)
                    next_scan = now + int(st.scan_interval_min * 60000)
                e.poll(int(time.time() * 1000))
            except RateLimit as ex:
                e.log(str(ex))
                self.stop_ev.wait(min(ex.wait, 120))
            except Exception as ex:      # сеть и т.п. — не падаем
                e.log(f"Ошибка цикла: {ex}")
                self.stop_ev.wait(5)
            self.stop_ev.wait(st.poll_sec)

    def stop(self):
        self.stop_ev.set()
