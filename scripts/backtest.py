# SPDX-License-Identifier: Apache-2.0
"""Leak-free backtest: for historical PRs, recommend reviewers from the data
that existed when each PR was opened, then compare with who actually reviewed.

  python scripts/backtest.py --owner apache --name tsfile \
      --repo /path/to/repos/tsfile --max-prs 50 [--before 2026-11-25T00:00:00Z]

Sample (defined so a run can be repeated): among the `--scan-prs` most recently
updated merged PRs before `--before` (default: now), the PRs that received a
review from a human other than their author, newest first, up to `--max-prs`.
Ground truth = those reviewers.

The clone must be up to date: history is read at each PR's creation time, so
a clone older than a PR starves it of history. PRs newer than the clone's HEAD
are skipped and counted.

Scoring is the action's with two differences: the workload signal (pending
reviews at that moment) is not reconstructed, and the module map and the
dependency graph are read from the clone's HEAD, not from the commit the PR
was proposed against.

A PR for which nobody can be recommended counts as a miss.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewer_recommender.config import Config  # noqa: E402
from reviewer_recommender.deps import build_dep_graph  # noqa: E402
from reviewer_recommender.history import collect_history, parse_time  # noqa: E402
from reviewer_recommender.modules import build_module_map  # noqa: E402
from reviewer_recommender.reviewlog import (  # noqa: E402
    build_identity_index,
    fetch_merged_prs,
    merge_identities,
    pr_author,
    reviewers_of,
    review_signals,
)
from reviewer_recommender.score import recommend  # noqa: E402


def clone_head_date(repo: Path) -> dt.datetime:
    """Committer date of the checked-out HEAD (how far the clone's history goes)."""
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%cI"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    return dt.datetime.fromisoformat(out)


def pick_sample(prs: list[dict], cfg: Config, repo: Path) -> tuple[list[dict], int]:
    """(evaluable PRs, number skipped). Evaluable = reviewed by a human other
    than the author, and opened no later than the clone's HEAD."""
    head = clone_head_date(repo)
    usable = [p for p in prs if reviewers_of(p, cfg)]
    fresh = [p for p in usable if parse_time(p["createdAt"]) <= head]
    return fresh, len(usable) - len(fresh)


def identity_window(prs: list[dict], window_days: int) -> tuple[str, str]:
    """`[since, until]` covering the signal window of every PR in the sample."""
    refs = [parse_time(p["createdAt"]) for p in prs]
    since = min(refs) - dt.timedelta(days=window_days)
    return since.isoformat(), max(refs).isoformat()


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k/n."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--repo", required=True, type=Path, help="up-to-date clone (default branch checked out)")
    ap.add_argument("--scan-prs", type=int, default=100, help="merged PRs scanned for the sample and used as review corpus")
    ap.add_argument("--max-prs", type=int, default=50, help="PRs evaluated at most")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--before", default=None, help="ISO timestamp the sample is frozen at (default: now)")
    ap.add_argument("--max-commits", type=int, default=20000, help="identity scan limit")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = Config()
    repo = args.repo.resolve()
    cutoff = parse_time(args.before) if args.before else dt.datetime.now(dt.timezone.utc)
    head = clone_head_date(repo)
    mmap = build_module_map(repo)
    dep_adj = build_dep_graph(repo, mmap).adj
    print(f"拉取 merged PR（updatedAt < {cutoff.isoformat()}）与身份映射 ...", flush=True)
    prs = fetch_merged_prs(args.owner, args.name, max_prs=args.scan_prs, updated_before=cutoff)
    sample, stale = pick_sample(prs, cfg, repo)
    sample = sample[: args.max_prs]
    since, until = identity_window(sample, cfg.window_days)
    ident = build_identity_index(args.owner, args.name, since, until=until, max_commits=args.max_commits)
    capped = "，已达上限" if ident.truncated else ""
    print(
        f"模块 {len(mmap.modules)} 个；扫描 {len(prs)} 个已合并 PR；"
        f"克隆 HEAD {head.isoformat()}"
    )
    print(
        f"身份映射 {len(ident.email_to_login)} 个 email -> login"
        f"（扫描 {ident.commits_scanned} 个 commit{capped}）\n"
    )
    if stale:
        print(f"警告: 克隆过旧，跳过 {stale} 个创建于 HEAD 之后的 PR（先 git pull）\n")

    hits1 = hitsk = 0
    rr_sum = 0.0
    no_cand = 0
    for pr in sample:
        ref = parse_time(pr["createdAt"])
        files = [f["path"] for f in pr["files"]["nodes"]]
        modules = cfg.touched_modules(mmap.of(f) for f in files)
        author = pr_author(pr)

        raw_hist = collect_history(repo, mmap, cfg, ref, before=pr["createdAt"])
        history = merge_identities(raw_hist, ident)
        rsig = review_signals(prs, mmap, cfg, ref)
        cands = recommend(modules, history, rsig, cfg, ref, pr_author=author, dep_adj=dep_adj)

        truth = reviewers_of(pr, cfg)
        top = [c.key.lower() for c in cands[: args.top]]
        rank = next((i + 1 for i, t in enumerate(top) if t in truth), None)
        if not top:
            no_cand += 1
        if rank == 1:
            hits1 += 1
        if rank:
            hitsk += 1
            rr_sum += 1.0 / rank

        if args.verbose:
            print(f"#{pr['number']} [{','.join(modules)[:60]}] {pr['title'][:38]}")
            for c in cands[: args.top]:
                bits = []
                for m, (nc, _cw, nr, _rw) in sorted(
                    c.module_detail.items(), key=lambda kv: -(kv[1][1] + kv[1][3])
                ):
                    parts = [f"{nc}c"] * bool(nc) + [f"{nr}r"] * bool(nr)
                    bits.append(f"{m}:{'+'.join(parts) if parts else '?'}")
                extra = " 跨模块" if c.coupling > 0 else ""
                if c.seams:
                    extra += " " + "、".join(c.seams[:2])
                print(f"  {c.score:5.1f} {c.key:16} {' · '.join(bits) or '-'}{extra}")
            print(f"  实际: {', '.join(sorted(truth))} {'✓ HIT' if rank else '✗ MISS'}")

    n = len(sample)
    print(f"\n=== 回测结果 ({args.owner}/{args.name}, 数据截止 {cutoff.isoformat()}, top-{args.top}) ===")
    print(f"样本: {n} 个 PR = 最近更新的 {len(prs)} 个已合并 PR 中，"
          f"有「作者以外真人 review」且不晚于克隆 HEAD 的 PR（取前 {args.max_prs} 个）")
    print("PR 列表: " + " ".join(f"#{p['number']}" for p in sample))
    if n:
        lo1, hi1 = wilson(hits1, n)
        lok, hik = wilson(hitsk, n)
        print(f"Top-1 命中率: {hits1}/{n} = {hits1 / n:.0%}（95% 置信区间 {lo1:.0%}–{hi1:.0%}）")
        print(f"Top-{args.top} 命中率: {hitsk}/{n} = {hitsk / n:.0%}（95% 置信区间 {lok:.0%}–{hik:.0%}）")
        print(f"MRR: {rr_sum / n:.3f}")
        print(f"无候选的 PR: {no_cand} 个（按未命中计）")
    else:
        print("没有可评估的 PR（均无作者以外的真人 review）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
