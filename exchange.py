"""Клиент USDT-M фьючерсов Binance. Только стандартная библиотека."""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

# Прямое подключение, без системного прокси Windows (его часто выставляет VPN). Свой прокси: SURGE_PROXY=http://127.0.0.1:порт
import os
_proxy = os.environ.get("SURGE_PROXY")
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": _proxy, "https": _proxy} if _proxy else {}))

MAINNET = "https://fapi.binance.com"
TESTNET = "https://testnet.binancefuture.com"


class ApiError(Exception):
    def __init__(self, msg, code=None, status=None):
        super().__init__(msg)
        self.code = code
        self.status = status


class RateLimit(ApiError):
    def __init__(self, wait):
        super().__init__(f"Лимит запросов Binance, пауза {wait} с", None, 429)
        self.wait = wait


class Http:
    def __init__(self, base, key="", secret="", timeout=10):
        self.base = base
        self.key = key
        self.secret = secret.encode() if secret else b""
        self.timeout = timeout
        self.offset = 0
        self.used_weight = 0

    def sync_time(self):
        t = self.request("GET", "/fapi/v1/time")["serverTime"]
        self.offset = t - int(time.time() * 1000)

    def request(self, method, path, params=None, signed=False, _retry=True):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            if not self.key or not self.secret:
                raise ApiError("Не заданы API-ключи")
            params["timestamp"] = int(time.time() * 1000) + self.offset
            params["recvWindow"] = 5000
        query = urllib.parse.urlencode(params)
        if signed:
            query += "&signature=" + hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()
        url = self.base + path + (("?" + query) if query else "")
        headers = {"User-Agent": "SurgeBot/1.0"}
        if self.key:
            headers["X-MBX-APIKEY"] = self.key
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with _opener.open(req, timeout=self.timeout) as r:
                w = r.headers.get("X-MBX-USED-WEIGHT-1M")
                if w and w.isdigit():
                    self.used_weight = int(w)
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            code, msg = None, body[:200]
            try:
                j = json.loads(body)
                code, msg = j.get("code"), j.get("msg", msg)
            except ValueError:
                pass
            if code == -1021 and signed and _retry:
                self.sync_time()
                return self.request(method, path, params, signed, _retry=False)
            if e.code in (418, 429):
                raise RateLimit(int(e.headers.get("Retry-After", "30") or 30))
            raise ApiError(f"HTTP {e.code} code={code}: {msg}", code, e.code)
        except urllib.error.URLError as e:
            raise ApiError(f"Ошибка сети: {e.reason}")
        except TimeoutError:
            raise ApiError("Таймаут запроса")


class FuturesApi:
    """Публичные и приватные методы, нужные боту."""

    def __init__(self, base=MAINNET, key="", secret=""):
        self.http = Http(base, key, secret)

    # ---------- публичные ----------
    def exchange_info(self):
        data = self.http.request("GET", "/fapi/v1/exchangeInfo")
        out = {}
        for s in data["symbols"]:
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT":
                continue
            f = {x["filterType"]: x for x in s["filters"]}
            out[s["symbol"]] = {
                "tick": float(f["PRICE_FILTER"]["tickSize"]),
                "step": float(f["LOT_SIZE"]["stepSize"]),
                "min_qty": float(f["LOT_SIZE"]["minQty"]),
                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5)),
                "trading": s.get("status") == "TRADING",
            }
        return out

    def tickers24h(self):
        rows = self.http.request("GET", "/fapi/v1/ticker/24hr")
        return [{"symbol": r["symbol"], "qv": float(r["quoteVolume"]), "last": float(r["lastPrice"])} for r in rows]

    def prices(self):
        return {r["symbol"]: float(r["price"]) for r in self.http.request("GET", "/fapi/v1/ticker/price")}

    def klines(self, symbol, limit):
        """1m свечи; последняя строка — формирующаяся. Возвращает dict списков."""
        rows = self.http.request("GET", "/fapi/v1/klines", {"symbol": symbol, "interval": "1m", "limit": limit})
        return {
            "t": [int(r[0]) for r in rows], "o": [float(r[1]) for r in rows], "h": [float(r[2]) for r in rows],
            "l": [float(r[3]) for r in rows], "c": [float(r[4]) for r in rows], "qv": [float(r[7]) for r in rows],
        }

    # ---------- приватные ----------
    def balance(self):
        last = None
        for path in ("/fapi/v3/balance", "/fapi/v2/balance"):
            try:
                for b in self.http.request("GET", path, signed=True):
                    if b["asset"] == "USDT":
                        return float(b["balance"]) + float(b.get("crossUnPnl", 0))
            except ApiError as e:
                last = e
        raise last or ApiError("Баланс USDT не найден")

    def position_qty(self, symbol):
        last = None
        for path in ("/fapi/v3/positionRisk", "/fapi/v2/positionRisk"):
            try:
                rows = self.http.request("GET", path, {"symbol": symbol}, signed=True)
                return sum(float(r["positionAmt"]) for r in rows)
            except ApiError as e:
                last = e
        raise last

    def is_hedge_mode(self):
        return bool(self.http.request("GET", "/fapi/v1/positionSide/dual", signed=True).get("dualSidePosition"))

    def set_isolated(self, symbol):
        try:
            self.http.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except ApiError as e:
            if e.code != -4046:      # уже изолированная
                raise

    def set_leverage(self, symbol, lev):
        self.http.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev}, signed=True)

    def order(self, params):
        return self.http.request("POST", "/fapi/v1/order", params, signed=True)

    def algo_order(self, params):
        """Условные ордера (STOP_MARKET и др.) — /fapi/v1/algoOrder, параметр triggerPrice."""
        return self.http.request("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", **params}, signed=True)

    def get_order(self, symbol, order_id):
        return self.http.request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    def cancel_order(self, symbol, order_id):
        return self.http.request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    def cancel_algo(self, algo_id):
        return self.http.request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}, signed=True)

    def open_orders(self, symbol):
        return self.http.request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    def open_algo_orders(self, symbol):
        return self.http.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
