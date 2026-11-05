# SPDX-License-Identifier: Apache-2.0
"""Parameter grid search on the leak-free backtest.

Events are collected once per PR (frozen at PR creation time), then every
parameter combination re-scores them in memory — no repeated git/gh work.

  python scripts/tune.py --repo-spec tsfile=/path/to/repos/tsfile \
                          --repo-spec iotdb=/path/to/repos/iotdb
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewer_recommender.config import Config  # noqa: E402
from reviewer_recommender.deps import build_dep_graph  # noqa: E402
from reviewer_recommender.history import collect_events, materialize  # noqa: E402
from reviewer_recommender.modules import build_module_map  # noqa: E402
from reviewer_recommender.reviewlog import (  # noqa: E402
    build_identity_map,
    fetch_merged_prs,
    review_events,
)
from reviewer_recommender.score import recommend  # noqa: E402
from scripts.backtest import canonicalize, real_reviewers  # noqa: E402

GRID = {
    "halflife_days": [90.0, 180.0, 365.0],
    "review_weight": [1.0, 1.5, 2.5],
    "coupling_weight": [0.0, 0.5, 1.0],
    "dep_weight": [0.0, 0.3, 0.6],
}
DEFAULTS = {"halflife_days": 180.0, "review_weight": 2.5, "coupling_weight": 0.5, "dep_weight": 0.3}
TOP = 3


def prepare(spec: str) -> tuple[str, list[dict], dict]:
    """name=path -> frozen per-PR cases + identity map (parameter-independent)."""
    name, path = spec.split("=", 1)
    repo = Path(path).resolve()
    base = Config()
    mmap = build_module_map(repo)
    prs = fetch_merged_prs("apache", name, max_prs=100)
    ident = build_identity_map("apache", name, commits_to_scan=500)

    cases: list[dict] = []
    for pr in prs:
        truth = real_reviewers(pr, base)
        if not truth:
            continue
        ref = dt.datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
        since = (ref - dt.timedelta(days=base.window_days)).isoformat()
        cases.append({
            "truth": truth,
            "author": (pr["author"] or {}).get("login"),
            "modules": base.touched_modules(mmap.of(f["path"]) for f in pr["files"]["nodes"]),
            "ref": ref,
            "cev": collect_events(repo, mmap, since, before=pr["createdAt"]),
            "rev": review_events(prs, mmap, base, ref),
        })
    print(f"{name}: {len(cases)} 个可用 PR", flush=True)
    adj = build_dep_graph(repo, mmap).adj
    return name, cases, ident, adj


def evaluate(cases, ident, adj, cfg: Config) -> tuple[float, float, float]:
    h1 = hk = 0
    rr = 0.0
    n = 0
    for c in cases:
        ref = c["ref"]
        hist = canonicalize(materialize(c["cev"], cfg, ref), ident)
        acc: dict[tuple[str, str], list[float]] = defaultdict(list)
        for login, m, when in c["rev"]:
            acc[(login, m)].append(cfg.decay(when, ref))
        rsig = {k: (len(v), sum(v)) for k, v in acc.items()}
        cands = recommend(c["modules"], hist, rsig, cfg, ref, pr_author=c["author"], dep_adj=adj)
        top = [x.key.lower() for x in cands[:TOP]]
        if not top:
            continue
        n += 1
        rank = next((i + 1 for i, t in enumerate(top) if t in c["truth"]), None)
        if rank == 1:
            h1 += 1
        if rank:
            hk += 1
            rr += 1.0 / rank
    return h1 / n, hk / n, rr / n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-spec", action="append", required=True, help="name=/path/to/clone")
    args = ap.parse_args()

    repos = [prepare(s) for s in args.repo_spec]

    rows = []
    for hl, rw, cw, dw in itertools.product(GRID["halflife_days"],
                                            GRID["review_weight"],
                                            GRID["coupling_weight"],
                                            GRID["dep_weight"]):
        cfg = Config(halflife_days=hl, review_weight=rw, coupling_weight=cw, dep_weight=dw)
        t1 = t3 = mrr = cnt = 0.0
        for _, cases, ident, adj in repos:
            a, b, c = evaluate(cases, ident, adj, cfg)
            t1 += a
            t3 += b
            mrr += c
            cnt += 1
        combo = (hl, rw, cw, dw)
        tag = "  <-- 当前默认" if combo == tuple(DEFAULTS.values()) else ""
        rows.append((t3 / cnt, t1 / cnt, mrr / cnt, hl, rw, cw, dw, tag))

    rows.sort(reverse=True)
    print(f"\n{'Top3':>6} {'Top1':>6} {'MRR':>6}   半衰期 λr λx λd")
    print("-" * 56)
    for t3, t1, mrr, hl, rw, cw, dw, tag in rows:
        print(f"{t3:6.0%} {t1:6.0%} {mrr:6.3f}   {hl:>4.0f}d {rw:>3} {cw:>3} {dw:>3}{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
