# SPDX-License-Identifier: Apache-2.0
"""Parameter grid search on the leak-free backtest.

Per PR the commit events and the review events are collected once, frozen at
the PR's creation time; every parameter combination then re-scores them in
memory, so the grid costs no further git/gh work.

  python scripts/tune.py --repo-spec tsfile=/path/to/repos/tsfile \
                         --repo-spec iotdb=/path/to/repos/iotdb

The sample is the backtest's (see scripts/backtest.py). Results are pooled
over all repositories: hits and PR counts are summed before dividing, so one
repository's rate does not get the weight of a whole second "vote".
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewer_recommender.config import Config  # noqa: E402
from reviewer_recommender.deps import build_dep_graph  # noqa: E402
from reviewer_recommender.history import collect_events, materialize, parse_time  # noqa: E402
from reviewer_recommender.modules import build_module_map  # noqa: E402
from reviewer_recommender.reviewlog import (  # noqa: E402
    IdentityIndex,
    build_identity_index,
    decayed_counts,
    fetch_merged_prs,
    merge_identities,
    pr_author,
    review_events,
    reviewers_of,
)
from reviewer_recommender.score import recommend  # noqa: E402
from scripts.backtest import identity_window, pick_sample  # noqa: E402

GRID = {
    "halflife_days": [90.0, 180.0, 365.0],
    "review_weight": [0.0, 1.0, 1.5, 2.5, 4.0],
    "coupling_weight": [0.0, 0.5, 1.0],
    "dep_weight": [0.0, 0.3, 0.6],
}
DEFAULTS = {"halflife_days": 180.0, "review_weight": 2.5, "coupling_weight": 0.5, "dep_weight": 0.3}
TOP = 3


@dataclass
class Repo:
    name: str
    cases: list[dict]
    ident: IdentityIndex
    adj: dict[str, set[str]]


def prepare(
    spec: str, owner: str, scan_prs: int = 100,
    before: str | None = None, max_commits: int = 20000,
) -> Repo:
    """name=/path/to/clone -> frozen per-PR cases plus one identity index."""
    name, path = spec.split("=", 1)
    repo = Path(path).resolve()
    base = Config()
    mmap = build_module_map(repo)
    cutoff = parse_time(before) if before else dt.datetime.now(dt.timezone.utc)
    prs = fetch_merged_prs(owner, name, max_prs=scan_prs, updated_before=cutoff)
    sample, stale = pick_sample(prs, base, repo)
    if stale:
        print(f"{name}: 警告: 克隆过旧，跳过 {stale} 个创建于 HEAD 之后的 PR", flush=True)
    since, until = identity_window(sample, base.window_days)
    ident = build_identity_index(owner, name, since, until=until, max_commits=max_commits)

    cases: list[dict] = []
    for pr in sample:
        ref = parse_time(pr["createdAt"])
        since = (ref - dt.timedelta(days=base.window_days)).isoformat()
        cases.append({
            "truth": reviewers_of(pr, base),
            "author": pr_author(pr),
            "modules": base.touched_modules(mmap.of(f["path"]) for f in pr["files"]["nodes"]),
            "ref": ref,
            "cev": collect_events(repo, mmap, since, before=pr["createdAt"]),
            "rev": review_events(prs, mmap, base, ref),
        })
    print(f"{name}: {len(cases)} 个可用 PR（扫描 {len(prs)} 个已合并 PR）", flush=True)
    return Repo(name, cases, ident, build_dep_graph(repo, mmap).adj)


def evaluate(repo: Repo, cfg: Config) -> tuple[int, int, float, int]:
    """(top-1 hits, top-TOP hits, reciprocal-rank sum, PR count) for one repository."""
    h1 = hk = n = 0
    rr = 0.0
    for c in repo.cases:
        ref = c["ref"]
        hist = merge_identities(materialize(c["cev"], cfg, ref), repo.ident)
        rsig = decayed_counts(c["rev"], cfg, ref)
        cands = recommend(c["modules"], hist, rsig, cfg, ref,
                          pr_author=c["author"], dep_adj=repo.adj)
        top = [x.key.lower() for x in cands[:TOP]]
        n += 1
        rank = next((i + 1 for i, t in enumerate(top) if t in c["truth"]), None)
        if rank == 1:
            h1 += 1
        if rank:
            hk += 1
            rr += 1.0 / rank
    return h1, hk, rr, n


def main() -> int:
    ap = argparse.ArgumentParser(description="Grid search on the leak-free backtest")
    ap.add_argument("--repo-spec", action="append", required=True, help="name=/path/to/clone")
    ap.add_argument("--owner", default="apache")
    ap.add_argument("--scan-prs", type=int, default=100)
    ap.add_argument("--before", default=None, help="ISO timestamp the sample is frozen at (default: now)")
    args = ap.parse_args()

    repos = [prepare(s, args.owner, args.scan_prs, args.before) for s in args.repo_spec]
    total = sum(len(r.cases) for r in repos)
    print(f"样本: {total} 个 PR（"
          + " + ".join(f"{r.name} {len(r.cases)}" for r in repos) + "）\n")

    rows = []
    for hl, rw, cw, dw in itertools.product(GRID["halflife_days"],
                                            GRID["review_weight"],
                                            GRID["coupling_weight"],
                                            GRID["dep_weight"]):
        cfg = Config(halflife_days=hl, review_weight=rw, coupling_weight=cw, dep_weight=dw)
        h1 = hk = n = 0
        rr = 0.0
        for repo in repos:
            a, b, c, d = evaluate(repo, cfg)
            h1, hk, rr, n = h1 + a, hk + b, rr + c, n + d
        combo = (hl, rw, cw, dw)
        tag = "  <-- 当前默认" if combo == tuple(DEFAULTS.values()) else ""
        rows.append((hk / n, h1 / n, rr / n, hl, rw, cw, dw, tag))

    rows.sort(reverse=True)
    print(f"{'Top3':>6} {'Top1':>6} {'MRR':>6}   半衰期  λr λx λd")
    print("-" * 58)
    for t3, t1, mrr, hl, rw, cw, dw, tag in rows:
        print(f"{t3:6.0%} {t1:6.0%} {mrr:6.3f}   {hl:>4.0f}d {rw:>4} {cw:>3} {dw:>3}{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
