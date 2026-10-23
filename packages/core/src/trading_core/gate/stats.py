"""The gate's overfitting and uncertainty statistics (spec 0015 AC-11), pure and seeded.

- ``stationary_bootstrap_lower``: the one sided lower bound of a mean, by the stationary block bootstrap
  (Politis and Romano 1994) with ``random.Random(seed)``.
- ``politis_white_block``: the automatic mean block length for that bootstrap (Politis and White 2004,
  with the Patton, Politis, and White 2009 correction), flat top lag window.
- ``pbo_cscv``: the probability of backtest overfitting by combinatorially symmetric cross validation
  (Bailey, Borwein, López de Prado, and Zhu 2017).
- ``deflated_sharpe``: the deflated Sharpe ratio probability (Bailey and López de Prado 2014).

Every Sharpe here is per day (not annualized). Floats only inside; sums use ``math.fsum``.
"""

import itertools
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import Final

from trading_core.gate.series import mean, sharpe

EULER_GAMMA: Final = 0.5772156649015329
_NORMAL: Final = NormalDist()


def _autocorrelation(values: Sequence[float], lag: int) -> float:
    """Sample autocorrelation at ``lag`` (biased estimator, as Politis and White use)."""
    m = mean(values)
    var = math.fsum((v - m) ** 2 for v in values)
    if var == 0.0:
        return 0.0
    return math.fsum((values[i] - m) * (values[i + lag] - m) for i in range(len(values) - lag)) / var


def _autocovariance(values: Sequence[float], lag: int) -> float:
    m = mean(values)
    return math.fsum((values[i] - m) * (values[i + abs(lag)] - m) for i in range(len(values) - abs(lag))) / len(values)


def _flat_top(t: float) -> float:
    a = abs(t)
    if a <= 0.5:
        return 1.0
    if a <= 1.0:
        return 2.0 * (1.0 - a)
    return 0.0


def politis_white_block(values: Sequence[float], minimum: int = 1) -> int:
    """The optimal mean block length of the stationary bootstrap, rounded up, at least ``minimum``.

    ``m̂`` is the smallest lag after which ``K_N`` autocorrelations in a row stay under ``2√(log10 n / n)``;
    ``M = 2 m̂`` bounds the flat top window. The result is capped at ``min(3√n, n/3)``.
    """
    n = len(values)
    if n < 4:
        return max(minimum, 1)
    k_n = max(5, math.ceil(math.sqrt(math.log10(n))))
    m_max = math.ceil(math.sqrt(n)) + k_n
    bound = 2.0 * math.sqrt(math.log10(n) / n)
    rho = [_autocorrelation(values, k) for k in range(1, min(m_max + k_n, n - 1) + 1)]
    m_hat = None
    for m in range(len(rho) - k_n + 1):
        if all(abs(r) < bound for r in rho[m : m + k_n]):
            m_hat = m
            break
    if m_hat is None:
        m_hat = m_max
    big_m = min(2 * max(m_hat, 1), n - 1)
    g = math.fsum(_flat_top(k / big_m) * abs(k) * _autocovariance(values, k) for k in range(-big_m, big_m + 1))
    g0 = math.fsum(_flat_top(k / big_m) * _autocovariance(values, k) for k in range(-big_m, big_m + 1))
    d_sb = 2.0 * g0**2
    cap = math.ceil(min(3.0 * math.sqrt(n), n / 3.0))
    if d_sb <= 0.0 or g == 0.0:
        return max(minimum, 1)
    b: float = (2.0 * g**2 / d_sb) ** (1.0 / 3.0) * n ** (1.0 / 3.0)
    return max(minimum, min(cap, math.ceil(b)))


@dataclass(frozen=True)
class BootstrapBound:
    """The lower bound, and how it was drawn."""

    lower: float
    block: int
    resamples: int


def stationary_bootstrap_lower(
    values: Sequence[float], block: int, resamples: int, confidence: float, seed: int
) -> BootstrapBound:
    """The one sided ``confidence`` lower bound of the mean of ``values``.

    Each resample walks ``len(values)`` steps over the series (wrapping), starting a new block at a
    random index with probability ``1/block``. The bound is the ``1 - confidence`` quantile of the
    resampled means (the order statistic at ``floor((1 - confidence) * resamples)``).
    """
    n = len(values)
    if n == 0:
        raise ValueError("a bootstrap needs at least one value")
    rng = random.Random(seed)
    # Bound once: a long scalping holdout runs this inner loop tens of millions of times. The draws
    # stay exactly those of the plain loop, so a seed always gives the same bound.
    draw, pick, fsum = rng.random, rng.randrange, math.fsum
    p = 1.0 / block
    means: list[float] = []
    picked = [0.0] * n
    for _ in range(resamples):
        index = pick(n)
        for step in range(n):
            picked[step] = values[index]
            index = pick(n) if draw() < p else (index + 1) % n
        means.append(fsum(picked) / n)
    means.sort()
    position = min(resamples - 1, math.floor((1.0 - confidence) * resamples))
    return BootstrapBound(means[position], block, resamples)


def _ranked(sharpes: Sequence[float | None]) -> list[int]:
    """Column indexes from worst to best: undefined first, then by Sharpe; a tie ranks the lower index higher."""
    return sorted(range(len(sharpes)), key=lambda i: (sharpes[i] is not None, sharpes[i] or 0.0, -i))


def pbo_cscv(matrix: Sequence[Sequence[float]], blocks: int) -> float:
    """The probability of backtest overfitting over a days by trials return matrix.

    The rows split into ``blocks`` contiguous, near equal blocks; every way to pick half of them is one
    in sample set, its complement the out of sample set. For each split the in sample winner's out of
    sample relative rank ``ω = rank / (N + 1)`` gives ``λ = ln(ω / (1 - ω))``; PBO is the share of splits
    with ``λ ≤ 0``. Raises ``ValueError`` with fewer rows than blocks or an odd block count.
    """
    rows = len(matrix)
    if blocks % 2 or rows < blocks:
        raise ValueError(f"CSCV needs an even block count and at least {blocks} rows, got {rows}")
    trials = len(matrix[0])
    parts = [list(range(i * rows // blocks, (i + 1) * rows // blocks)) for i in range(blocks)]
    overfit = total = 0
    for chosen in itertools.combinations(range(blocks), blocks // 2):
        inside = [r for b in chosen for r in parts[b]]
        outside = [r for b in range(blocks) if b not in chosen for r in parts[b]]
        is_sharpe = [sharpe([matrix[r][j] for r in inside]) for j in range(trials)]
        oos_sharpe = [sharpe([matrix[r][j] for r in outside]) for j in range(trials)]
        winner = _ranked(is_sharpe)[-1]
        rank = _ranked(oos_sharpe).index(winner) + 1
        omega = rank / (trials + 1)
        total += 1
        if math.log(omega / (1.0 - omega)) <= 0.0:
            overfit += 1
    return overfit / total


def moments(values: Sequence[float]) -> tuple[float, float]:
    """Skewness and raw (not excess) kurtosis from the population moments; (0, 3) without variance."""
    m = mean(values)
    m2 = math.fsum((v - m) ** 2 for v in values) / len(values)
    if m2 == 0.0:
        return 0.0, 3.0
    m3 = math.fsum((v - m) ** 3 for v in values) / len(values)
    m4 = math.fsum((v - m) ** 4 for v in values) / len(values)
    return m3 / m2**1.5, m4 / m2**2


def expected_max_sharpe(trials: int, variance: float) -> float:
    """The expected maximum per day Sharpe of ``trials`` unskilled trials with that Sharpe variance."""
    if trials <= 1 or variance <= 0.0:
        return 0.0
    return math.sqrt(variance) * (
        (1.0 - EULER_GAMMA) * _NORMAL.inv_cdf(1.0 - 1.0 / trials)
        + EULER_GAMMA * _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    )


def deflated_sharpe(
    sharpe_per_day: float, days: int, skew: float, kurtosis: float, trials: int, trial_variance: float
) -> float | None:
    """The deflated Sharpe probability ``Phi((SR - SR0) sqrt(T - 1) / sqrt(1 - skew SR + (kurt - 1) / 4 SR^2))``.

    ``SR₀`` is ``expected_max_sharpe``. None when ``days < 2`` or the variance term is not positive.
    """
    if days < 2:
        return None
    denominator = 1.0 - skew * sharpe_per_day + (kurtosis - 1.0) / 4.0 * sharpe_per_day**2
    if denominator <= 0.0:
        return None
    sr0 = expected_max_sharpe(trials, trial_variance)
    return _NORMAL.cdf((sharpe_per_day - sr0) * math.sqrt(days - 1) / math.sqrt(denominator))
