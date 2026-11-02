"""Брокеры: DemoBroker (виртуальные деньги, реальные цены) и LiveBroker (реальные ордера Binance Futures)."""
import math
import time

from exchange import ApiError


def floor_to(x, step):
    return math.floor(x / step + 1e-9) * step


def ceil_to(x, step):
    return math.ceil(x / step - 1e-9) * step


def fmt(x, step):
    """Число в строку с точностью шага цены/количества."""
    s = f"{step:.10f}".rstrip("0")
    dec = len(s.split(".")[1]) if "." in s else 0
    return f"{x:.{dec}f}"


def calc_qty(equity, entry, stop, st, info):
    """Размер позиции по риску. Возвращает (qty, причина_отказа)."""
    dist = entry - stop
    if dist <= 0:
        return None, "стоп выше входа"
    risk = st.risk_fixed_usdt if st.risk_fixed_usdt > 0 else equity * st.risk_percent / 100.0
    qty = risk / dist
    max_notional = equity * st.leverage * 0.9
    if qty * entry > max_notional:
        qty = max_notional / entry
    qty = floor_to(qty, info["step"])
    if qty < info["min_qty"] or qty * entry < info["min_notional"]:
        return None, f"слишком маленький объём ({qty} шт.) при балансе {equity:.2f}"
    return qty, ""


def make_trade(pos, exit_px, reason, t_ms, fees):
    pnl = (exit_px - pos["entry"]) * pos["qty"] - fees
    return {
        "symbol": pos["symbol"], "t_open": pos["t_open"], "t_close": t_ms, "entry": pos["entry"],
        "exit": exit_px, "stop": pos["stop"], "tp": pos["tp"], "qty": pos["qty"],
        "pnl": pnl, "r": pnl / pos["risk"] if pos["risk"] else 0.0, "reason": reason, "mode": pos.get("mode", ""),
    }


class DemoBroker:
    mode = "demo"

    def __init__(self, st):
        self.st = st
        self.balance = st.demo_balance
        self.positions = {}

    def equity(self):
        return self.balance

    def open(self, symbol, ref_price, qty, stop, info, now_ms):
        st = self.st
        entry = ref_price * (1 + st.demo_slippage_pct / 100)
        entry = ceil_to(entry, info["tick"])
        if entry <= stop:
            return None
        tp = floor_to(entry + st.rr * (entry - stop), info["tick"])
        fee = entry * qty * st.taker_fee_pct / 100
        pos = {"symbol": symbol, "qty": qty, "entry": entry, "stop": stop, "tp": tp, "t_open": now_ms,
               "risk": (entry - stop) * qty + fee, "fee_open": fee, "mode": "demo", "tick": info["tick"]}
        self.positions[symbol] = pos
        return pos

    def _close(self, pos, px, reason, now_ms, maker):
        st = self.st
        fee_close = px * pos["qty"] * (st.maker_fee_pct if maker else st.taker_fee_pct) / 100
        tr = make_trade(pos, px, reason, now_ms, pos["fee_open"] + fee_close)
        self.balance += tr["pnl"]
        del self.positions[pos["symbol"]]
        return tr

    def manage(self, prices, now_ms):
        st, closed = self.st, []
        for sym, pos in list(self.positions.items()):
            p = prices.get(sym)
            if p is None:
                continue
            if p <= pos["stop"]:
                px = max(pos["tick"], pos["stop"] * (1 - st.demo_slippage_pct / 100))
                closed.append(self._close(pos, px, "stop", now_ms, False))
            elif p >= pos["tp"]:
                closed.append(self._close(pos, pos["tp"], "take", now_ms, True))
            elif now_ms - pos["t_open"] > st.max_hold_min * 60000:
                closed.append(self._close(pos, p * (1 - st.demo_slippage_pct / 100), "time", now_ms, False))
        return closed

    def close_all(self, prices, now_ms):
        out = []
        for sym, pos in list(self.positions.items()):
            p = prices.get(sym, pos["entry"])
            out.append(self._close(pos, p * (1 - self.st.demo_slippage_pct / 100), "manual", now_ms, False))
        return out


class LiveBroker:
    mode = "live"

    def __init__(self, st, api, log=print):
        self.st = st
        self.api = api
        self.log = log
        self.positions = {}
        self._prepared = set()

    def check_start(self):
        self.api.http.sync_time()
        if self.api.is_hedge_mode():
            raise ApiError("На аккаунте включён Hedge Mode. Переключите на One-way Mode (Настройки фьючерсов Binance).")
        return self.api.balance()

    def equity(self):
        return self.api.balance()

    def _prepare(self, symbol):
        if symbol in self._prepared:
            return
        self.api.set_isolated(symbol)
        self.api.set_leverage(symbol, self.st.leverage)
        self._prepared.add(symbol)

    def _market_close(self, symbol, qty, info):
        return self.api.order({"symbol": symbol, "side": "SELL", "type": "MARKET",
                               "quantity": fmt(qty, info["step"]), "reduceOnly": "true",
                               "newOrderRespType": "RESULT"})

    def open(self, symbol, ref_price, qty, stop, info, now_ms):
        st, api = self.st, self.api
        self._prepare(symbol)
        limit_px = ceil_to(ref_price * (1 + st.max_chase_pct / 100), info["tick"])
        r = api.order({"symbol": symbol, "side": "BUY", "type": "LIMIT", "timeInForce": "IOC",
                       "price": fmt(limit_px, info["tick"]), "quantity": fmt(qty, info["step"]),
                       "newOrderRespType": "RESULT"})
        filled = float(r.get("executedQty", 0))
        if filled <= 0:
            self.log(f"{symbol}: IOC-заявка не исполнилась (цена ушла выше {limit_px})")
            return None
        entry = float(r.get("avgPrice") or ref_price)
        filled = floor_to(filled, info["step"])
        tp = floor_to(entry + st.rr * (entry - stop), info["tick"])
        pos = {"symbol": symbol, "qty": filled, "entry": entry, "stop": stop, "tp": tp, "t_open": now_ms,
               "risk": (entry - stop) * filled, "fee_open": entry * filled * st.taker_fee_pct / 100,
               "mode": "live", "tick": info["tick"], "stop_id": None, "tp_id": None, "info": info}
        qs = fmt(filled, info["step"])
        try:
            a = api.algo_order({"symbol": symbol, "side": "SELL", "type": "STOP_MARKET",
                                "triggerPrice": fmt(stop, info["tick"]), "quantity": qs,
                                "reduceOnly": "true", "workingType": "CONTRACT_PRICE"})
            pos["stop_id"] = a.get("algoId")
        except ApiError as e:
            self.log(f"{symbol}: НЕ УДАЛОСЬ выставить стоп ({e}). Аварийно закрываю позицию по рынку.")
            try:
                self._market_close(symbol, filled, info)
            except ApiError as e2:
                self.log(f"{symbol}: !!! не удалось закрыть позицию: {e2}. ЗАКРОЙТЕ ВРУЧНУЮ !!!")
            return None
        try:
            t = api.order({"symbol": symbol, "side": "SELL", "type": "LIMIT", "timeInForce": "GTC",
                           "price": fmt(tp, info["tick"]), "quantity": qs, "reduceOnly": "true"})
            pos["tp_id"] = t.get("orderId")
        except ApiError as e:
            self.log(f"{symbol}: тейк не выставлен ({e}); позиция защищена только стопом.")
        self.positions[symbol] = pos
        return pos

    def _cancel_all(self, pos):
        sym = pos["symbol"]
        if pos.get("stop_id") is not None:
            try:
                self.api.cancel_algo(pos["stop_id"])
            except ApiError:
                pass
        if pos.get("tp_id") is not None:
            try:
                self.api.cancel_order(sym, pos["tp_id"])
            except ApiError:
                pass

    def manage(self, prices, now_ms):
        st, api, closed = self.st, self.api, []
        for sym, pos in list(self.positions.items()):
            try:
                amt = api.position_qty(sym)
            except ApiError as e:
                self.log(f"{sym}: не удалось запросить позицию: {e}")
                continue
            if abs(amt) < 1e-12:
                reason, px = "stop", pos["stop"]
                if pos.get("tp_id") is not None:
                    try:
                        if api.get_order(sym, pos["tp_id"]).get("status") == "FILLED":
                            reason, px = "take", pos["tp"]
                    except ApiError:
                        pass
                self._cancel_all(pos)
                fee_close = px * pos["qty"] * (st.maker_fee_pct if reason == "take" else st.taker_fee_pct) / 100
                closed.append(make_trade(pos, px, reason, now_ms, pos["fee_open"] + fee_close))
                del self.positions[sym]
            elif now_ms - pos["t_open"] > st.max_hold_min * 60000:
                self._cancel_all(pos)
                try:
                    r = self._market_close(sym, abs(amt), pos["info"])
                    px = float(r.get("avgPrice") or prices.get(sym, pos["entry"]))
                except ApiError as e:
                    self.log(f"{sym}: не удалось закрыть по времени: {e}")
                    continue
                closed.append(make_trade(pos, px, "time", now_ms, pos["fee_open"] + px * pos["qty"] * st.taker_fee_pct / 100))
                del self.positions[sym]
        return closed

    def close_all(self, prices, now_ms):
        out = []
        for sym, pos in list(self.positions.items()):
            self._cancel_all(pos)
            try:
                amt = abs(self.api.position_qty(sym))
                px = pos["entry"]
                if amt > 0:
                    r = self._market_close(sym, amt, pos["info"])
                    px = float(r.get("avgPrice") or prices.get(sym, px))
                out.append(make_trade(pos, px, "manual", now_ms, pos["fee_open"] + px * pos["qty"] * self.st.taker_fee_pct / 100))
                del self.positions[sym]
            except ApiError as e:
                self.log(f"{sym}: не удалось закрыть: {e}")
        return out
