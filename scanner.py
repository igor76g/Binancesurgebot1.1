"""Отбор монет: рост объёма и NATR за последние N минут относительно предыдущих часов."""


def pct_true_ranges(h, l, c):
    """True range каждой свечи (кроме первой) в % от предыдущего закрытия."""
    out = []
    for i in range(1, len(c)):
        pc = c[i - 1]
        tr = max(h[i] - l[i], abs(h[i] - pc), abs(l[i] - pc))
        out.append(tr / pc * 100.0 if pc > 0 else 0.0)
    return out


def _ratio(now, base):
    if base <= 1e-12:
        return float("inf") if now > 1e-12 else 0.0
    return now / base


def surge_metrics(h, l, c, qv, surge_min, baseline_min):
    """Метрики по ЗАКРЫТЫМ свечам. None, если истории недостаточно.

    vol_ratio  — объём (USDT) за последние surge_min минут / средний объём за такие же окна в предыдущие baseline_min минут
    natr_now   — средний true range за последние surge_min минут, % (это NATR с периодом surge_min на 1м свечах)
    natr_ratio — natr_now / средний true range за предыдущие baseline_min минут
    """
    n = len(c)
    if n < surge_min + baseline_min + 1:
        return None
    trp = pct_true_ranges(h, l, c)                      # trp[k] — свеча k+1
    now = trp[-surge_min:]
    base = trp[-(surge_min + baseline_min):-surge_min]
    natr_now = sum(now) / len(now)
    natr_base = sum(base) / len(base)
    vol_now = sum(qv[-surge_min:])
    bq = qv[-(surge_min + baseline_min):-surge_min]
    vol_base = sum(bq) / len(bq) * surge_min
    return {
        "vol_ratio": _ratio(vol_now, vol_base), "natr_ratio": _ratio(natr_now, natr_base),
        "natr_now": natr_now, "natr_base": natr_base, "vol_now": vol_now,
    }


def passes(m, qv24, st):
    return (m is not None and qv24 >= st.min_volume_24h_usdt and m["vol_ratio"] >= st.vol_mult
            and m["natr_ratio"] >= st.natr_mult and m["natr_now"] >= st.min_natr)
