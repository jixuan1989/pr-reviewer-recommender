# SPDX-License-Identifier: Apache-2.0
"""The backtest's sample selection and interval arithmetic."""
from __future__ import annotations

import datetime as dt
import os
import subprocess

import pytest
from fake_github import merged_pr

from reviewer_recommender.config import Config
from scripts.backtest import identity_window, pick_sample, wilson


def test_wilson_interval_brackets_the_known_values():
    assert wilson(0, 10) == pytest.approx((0.0, 0.2775), abs=1e-4)
    assert wilson(10, 10) == pytest.approx((0.7225, 1.0), abs=1e-4)
    assert wilson(30, 40) == pytest.approx((0.5980, 0.8581), abs=1e-4)   # 75% sits right of centre
    assert wilson(0, 0) == (0.0, 0.0)
    assert wilson(20, 40)[0] < 0.5 < wilson(20, 40)[1]


def _clone(tmp_path) -> None:
    """A git repository whose HEAD was committed on 2026-01-10."""
    env = {**os.environ, "GIT_COMMITTER_DATE": "2026-01-10T00:00:00+00:00"}
    git = ["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run([*git, "add", "."], check=True, capture_output=True)
    subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "c"],
                   check=True, capture_output=True, env=env)


def test_sample_keeps_reviewed_prs_and_skips_ones_newer_than_the_clone(tmp_path):
    _clone(tmp_path)
    day = "2026-01-09T00:00:00Z"
    by_someone_else = [("bob", "APPROVED", "2026-01-05T00:00:00Z")]
    prs = [
        merged_pr(1, day, "alice", ["f"], by_someone_else),
        merged_pr(2, day, "alice", ["f"], [("alice", "COMMENTED", day)]),   # author only
        merged_pr(3, day, "alice", ["f"]),                                  # nobody reviewed
        merged_pr(4, day, "alice", ["f"], by_someone_else),
        merged_pr(5, "2026-01-11T00:00:00Z", "alice", ["f"], by_someone_else),   # after HEAD
    ]
    sample, skipped = pick_sample(prs, Config(), tmp_path)
    assert [p["number"] for p in sample] == [1, 4] and skipped == 1


def test_identity_window_covers_every_prs_signal_window():
    prs = [merged_pr(1, "2026-06-01T00:00:00Z", "a", ["f"]),
           merged_pr(2, "2026-03-01T00:00:00Z", "a", ["f"])]
    since, until = identity_window(prs, 365)
    oldest = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)
    assert since == (oldest - dt.timedelta(days=365)).isoformat()
    assert until == "2026-06-01T00:00:00+00:00"
