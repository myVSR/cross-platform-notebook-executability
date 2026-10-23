"""Design-based estimators for the cross-platform study (notes/analysis-plan.md, sections 3 and 10).

All estimators are domain means: the sample is every drawn resource of a platform (primary + used reserve),
the domain is the subset the outcome is defined for (e.g. analysable resources), and the outcome y is
only read inside the domain.

- Codeberg: simple random sample of the screened-in census, with the finite-population correction
  f = n / N (n = drawn resources, N = screened-in frame).
- GitHub: simple random sample (f = 0).
- Zenodo: two-phase stratified sample. Stratum h has a frame of N_h records, an inspected
  phase-1 subsample of m_h, a pool of |P_h| records with (or possibly with) notebooks, and n_h drawn
  resources, so pi_h = (m_h / N_h)(n_h / |P_h|) and w_h = 1 / pi_h. The mean is the Hajek estimator
  sum(w d y) / sum(w d). Its standard error comes from a bootstrap that resamples phase 2 within strata
  (with replacement) and phase 1 through the pool size, |P_h|* ~ Binomial(m_h, |P_h| / m_h), which
  changes w_h. A stratum whose pool is the whole frame (Z1: m_h = N_h) has no phase-1 variation.
"""
from __future__ import annotations

import math
import zlib
from dataclasses import dataclass, field

import numpy as np
from scipy import stats

Z95 = stats.norm.ppf(0.975)


@dataclass
class Stratum:
    """Phase-1 figures of a Zenodo stratum (draw summary.json)."""
    frame_n: int          # N_h
    inspected: int        # m_h
    pool: int             # |P_h|


@dataclass
class Estimate:
    mean: float
    se: float
    low: float
    high: float
    n_sample: int                 # drawn resources in the sample
    n_domain: int                 # of which in the domain
    df: float = math.inf          # degrees of freedom of the interval (inf = normal)
    n_eff: float | None = None    # Kish effective sample size (weighted designs)
    extra: dict = field(default_factory=dict)

    def as_dict(self):
        out = dict(mean=self.mean, se=self.se, low=self.low, high=self.high, n_sample=self.n_sample,
                   n_domain=self.n_domain, df=None if math.isinf(self.df) else self.df, n_eff=self.n_eff)
        out.update(self.extra)
        return out


def child_rng(seed: int, label: str) -> np.random.Generator:
    """A generator that depends only on the seed and a label, not on the order of the calls."""
    return np.random.default_rng([seed, zlib.crc32(label.encode())])


def srs_mean(y, domain, frame_n: int | None = None) -> Estimate:
    """Domain mean of a simple random sample; with frame_n the finite-population correction is applied."""
    y, domain = np.asarray(y, float), np.asarray(domain, bool)
    n_sample, values = len(domain), y[domain]
    n = len(values)
    if n == 0:
        return Estimate(math.nan, math.nan, math.nan, math.nan, n_sample, 0)
    mean = float(values.mean())
    if n < 2:
        return Estimate(mean, math.nan, math.nan, math.nan, n_sample, n)
    fpc = 1 - n_sample / frame_n if frame_n else 1.0
    se = float(values.std(ddof=1) / math.sqrt(n) * math.sqrt(max(fpc, 0.0)))
    t = stats.t.ppf(0.975, n - 1)
    return Estimate(mean, se, mean - t * se, mean + t * se, n_sample, n, df=n - 1,
                    extra=dict(fpc=fpc))


def zenodo_weights(strata: dict[str, Stratum], drawn: dict[str, int]) -> dict[str, float]:
    """w_h = 1 / pi_h with pi_h = (m_h / N_h)(n_h / |P_h|)."""
    weights = {}
    for h, s in strata.items():
        n_h = drawn.get(h, 0)
        weights[h] = (s.frame_n * s.pool) / (s.inspected * n_h) if n_h else math.nan
    return weights


def two_phase_mean(y, domain, stratum, strata: dict[str, Stratum], rng: np.random.Generator,
                   bootstrap: int = 2000) -> Estimate:
    """Hajek domain mean of the two-phase stratified Zenodo sample with a two-phase bootstrap SE."""
    y, domain, stratum = np.asarray(y, float), np.asarray(domain, bool), np.asarray(stratum, object)
    labels = [h for h in strata if (stratum == h).any()]
    drawn = {h: int((stratum == h).sum()) for h in labels}
    weights = zenodo_weights({h: strata[h] for h in labels}, drawn)
    w = np.array([weights.get(h, math.nan) for h in stratum])
    dy = np.where(domain, np.nan_to_num(y), 0.0)
    total_w = float((w * domain).sum())
    n_domain = int(domain.sum())
    if n_domain == 0:
        return Estimate(math.nan, math.nan, math.nan, math.nan, len(y), 0)
    mean = float((w * dy).sum() / total_w)
    wd = w[domain]
    n_eff = float(wd.sum() ** 2 / (wd ** 2).sum())

    numerator, denominator = np.zeros(bootstrap), np.zeros(bootstrap)
    for h in labels:
        idx = np.flatnonzero(stratum == h)
        s, n_h = strata[h], len(idx)
        pick = idx[rng.integers(0, n_h, size=(bootstrap, n_h))]
        sum_dy = dy[pick].sum(axis=1)
        sum_d = domain[pick].sum(axis=1)
        if s.inspected < s.frame_n:                      # phase 1 is a subsample: the pool size varies
            pool = rng.binomial(s.inspected, s.pool / s.inspected, size=bootstrap)
        else:
            pool = np.full(bootstrap, s.pool)
        w_h = s.frame_n * pool / (s.inspected * n_h)
        numerator += w_h * sum_dy
        denominator += w_h * sum_d
    with np.errstate(invalid='ignore', divide='ignore'):
        replicates = numerator / denominator
    replicates = replicates[np.isfinite(replicates)]
    se = float(replicates.std(ddof=1)) if len(replicates) > 1 else math.nan
    return Estimate(mean, se, mean - Z95 * se, mean + Z95 * se, len(y), n_domain, n_eff=n_eff,
                    extra=dict(boot_low=float(np.percentile(replicates, 2.5)),
                               boot_high=float(np.percentile(replicates, 97.5)),
                               bootstrap=bootstrap, bootstrap_valid=int(len(replicates)),
                               weights={h: weights[h] for h in labels}, drawn=drawn,
                               estimated_domain_size=total_w))


@dataclass
class Difference:
    a: str
    b: str
    diff: float
    se: float
    low: float
    high: float
    df: float
    p: float
    p_holm: float | None = None

    def as_dict(self):
        return dict(a=self.a, b=self.b, diff=self.diff, se=self.se, low=self.low, high=self.high,
                    df=None if math.isinf(self.df) else self.df, p=self.p, p_holm=self.p_holm)


def difference(a_label: str, a: Estimate, b_label: str, b: Estimate) -> Difference:
    """a - b for independent samples: variances add. Welch-Satterthwaite df if both have finite df
    (Codeberg - GitHub), otherwise normal (any comparison with the bootstrap-based Zenodo estimate)."""
    diff = a.mean - b.mean
    se = math.sqrt(a.se ** 2 + b.se ** 2)
    if math.isfinite(a.df) and math.isfinite(b.df) and se > 0:
        df = se ** 4 / (a.se ** 4 / a.df + b.se ** 4 / b.df)
        crit, p = stats.t.ppf(0.975, df), 2 * stats.t.sf(abs(diff) / se, df)
    else:
        df = math.inf
        crit, p = Z95, (2 * stats.norm.sf(abs(diff) / se) if se > 0 else math.nan)
    return Difference(a_label, b_label, diff, se, diff - crit * se, diff + crit * se, df, float(p))


def holm(p_values):
    """Holm step-down adjusted p-values, in the input order."""
    p = np.asarray(p_values, float)
    order = np.argsort(p)
    m = len(p)
    adjusted = np.empty(m)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * p[i])
        adjusted[i] = min(1.0, running)
    return adjusted.tolist()


def wilson(successes: int, n: int):
    """95% Wilson interval of a proportion."""
    if n == 0:
        return math.nan, math.nan
    phat, z2 = successes / n, Z95 ** 2
    centre = (phat + z2 / (2 * n)) / (1 + z2 / n)
    half = Z95 * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return centre - half, centre + half
