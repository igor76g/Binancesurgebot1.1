"""Поиск сетапа: верхний экстремум (после него есть откатная красная свеча) и нижний экстремум отката.

Окно — закрытые 1м свечи с момента начала всплеска.
  H (верхний экстремум)  — максимум high в окне; после свечи с этим максимумом должна закрыться
                           хотя бы одна красная (close < open) свеча — откат.
  L (нижний экстремум)   — самый низкий low среди свечей ПОСЛЕ свечи с H (низ отката).
Вход — пробой H вверх, стоп — за L, тейк — по rr.
"""
from bisect import bisect_left
from dataclasses import dataclass


@dataclass
class Setup:
    H: float
    L: float
    t_H: int
    red_count: int


def find_setup(t, o, h, l, c, start_ms, min_red=1):
    n = len(c)
    i0 = bisect_left(t, start_ms)
    if n - i0 < 2:
        return None
    iH = i0
    for i in range(i0, n):
        if h[i] >= h[iH]:            # при равных максимумах берём более позднюю свечу
            iH = i
    red = sum(1 for k in range(iH + 1, n) if c[k] < o[k])
    if red < min_red or iH >= n - 1:
        return None
    return Setup(H=h[iH], L=min(l[iH + 1:n]), t_H=t[iH], red_count=red)
