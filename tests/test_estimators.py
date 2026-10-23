#!/usr/bin/env python3
"""Offline tests for analysis/paper/estimators.py (design-based estimators of the study analysis).
Run: python3 tests/test_estimators.py"""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'analysis' / 'paper'))
import estimators as est  # noqa: E402


class SrsTest(unittest.TestCase):
    def test_domain_mean_and_fpc(self):
        y = np.array([0, 1, 0.5, 1, 0, np.nan])
        d = np.array([1, 1, 1, 1, 1, 0], bool)
        e = est.srs_mean(y, d, frame_n=60)
        values = y[:5]
        self.assertAlmostEqual(e.mean, values.mean())
        self.assertAlmostEqual(e.se, values.std(ddof=1) / math.sqrt(5) * math.sqrt(1 - 6 / 60))
        self.assertEqual((e.n_sample, e.n_domain, e.df), (6, 5, 4))
        self.assertAlmostEqual(est.srs_mean(y, d).se, values.std(ddof=1) / math.sqrt(5))

    def test_empty_domain(self):
        e = est.srs_mean([1, 0], [False, False])
        self.assertTrue(math.isnan(e.mean))
        self.assertEqual(e.n_domain, 0)


class TwoPhaseTest(unittest.TestCase):
    strata = {'A': est.Stratum(frame_n=1000, inspected=1000, pool=1000),     # census pool (like Z1)
              'B': est.Stratum(frame_n=10000, inspected=500, pool=100)}      # phase-1 subsample (like Z2)

    def test_weights(self):
        w = est.zenodo_weights(self.strata, {'A': 50, 'B': 40})
        self.assertAlmostEqual(w['A'], 1000 / 50)                            # 1 / (n/N)
        self.assertAlmostEqual(w['B'], 10000 * 100 / (500 * 40))             # 1 / ((m/N)(n/|P|))

    def test_hajek_mean(self):
        rng = np.random.default_rng(1)
        stratum = np.array(['A'] * 50 + ['B'] * 40, object)
        y = np.r_[np.full(50, 0.2), np.full(40, 0.6)]
        domain = np.ones(90, bool)
        domain[[0, 1, 50]] = False                                           # outside the domain
        e = est.two_phase_mean(y, domain, stratum, self.strata, rng, bootstrap=200)
        wa, wb = 1000 / 50, 10000 * 100 / (500 * 40)
        expected = (wa * 48 * 0.2 + wb * 39 * 0.6) / (wa * 48 + wb * 39)
        self.assertAlmostEqual(e.mean, expected)
        self.assertEqual(e.n_domain, 87)
        self.assertGreater(e.se, 0)                                          # pool size varies in stratum B

    def test_bootstrap_matches_srs_for_one_census_stratum(self):
        rng = np.random.default_rng(2)
        y = rng.uniform(size=300)
        strata = {'A': est.Stratum(100000, 100000, 100000)}
        e = est.two_phase_mean(y, np.ones(300, bool), np.array(['A'] * 300, object), strata,
                               est.child_rng(7, 'x'), bootstrap=3000)
        analytic = y.std(ddof=1) / math.sqrt(300)
        self.assertAlmostEqual(e.mean, y.mean())
        self.assertLess(abs(e.se / analytic - 1), 0.1)
        self.assertAlmostEqual(e.n_eff, 300)                                 # equal weights: Kish n = n

    def test_child_rng_is_order_independent(self):
        a = est.child_rng(5, 'primary:zenodo').integers(0, 10 ** 9)
        est.child_rng(5, 'other').integers(0, 10 ** 9)
        self.assertEqual(a, est.child_rng(5, 'primary:zenodo').integers(0, 10 ** 9))


class TestsTest(unittest.TestCase):
    def test_holm(self):
        self.assertEqual([round(p, 6) for p in est.holm([0.01, 0.04, 0.03])], [0.03, 0.06, 0.06])
        self.assertEqual(est.holm([0.5, 0.9]), [1.0, 1.0])

    def test_difference_welch_and_normal(self):
        a = est.Estimate(0.3, 0.02, 0, 0, 100, 100, df=99)
        b = est.Estimate(0.2, 0.03, 0, 0, 100, 100, df=99)
        d = est.difference('a', a, 'b', b)
        se = math.sqrt(0.02 ** 2 + 0.03 ** 2)
        self.assertAlmostEqual(d.se, se)
        self.assertAlmostEqual(d.df, se ** 4 / (0.02 ** 4 / 99 + 0.03 ** 4 / 99))
        z = est.difference('a', a, 'z', est.Estimate(0.2, 0.03, 0, 0, 100, 100))
        self.assertTrue(math.isinf(z.df))
        self.assertAlmostEqual(z.high - z.diff, est.Z95 * se)

    def test_wilson(self):
        low, high = est.wilson(0, 10)
        self.assertAlmostEqual(low, 0, places=12)
        self.assertAlmostEqual(high, 0.2775, places=4)
        low, high = est.wilson(5, 10)
        self.assertAlmostEqual(low + high, 1.0)


if __name__ == '__main__':
    unittest.main()
