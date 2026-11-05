# SPDX-License-Identifier: Apache-2.0
"""Backtest: recommend for historical PRs using only data available at PR creation,
then compare with the humans who actually reviewed.

  python scripts/backtest.py --owner apache --name tsfile \
      --repo /path/to/repos/tsfile --max-prs 50
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewer_recommender.config import Config, UNASSIGNED  # noqa: E402
from reviewer_recommender.history import AuthorHistory, collect_history  # noqa: E402
from reviewer_recommender.modules import build_module_map  # noqa: E402
from reviewer_recommender.reviewlog import (  # noqa: E402
    build_identity_map,
    canonical_identities,
    fetch_merged_prs,
    review_signals,
)
from reviewer_recommender.score import recommend  # noqa: E402

HUMAN_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}


def real_reviewers(pr: dict, cfg: Config) -> set[str]:
    out = set()
    for rv in pr["reviews"]["nodes"]:
        login = (rv["author"] or {}).get("login")
        if login and rv["state"] in HUMAN_STATES and not cfg.is_bot(login):
            out.add(login.lower())
    return out


def canonicalize(history: dict[str, AuthorHistory], ident: dict) -> dict[str, AuthorHistory]:
    """Merge history keys into login-identified authors."""
    out: dict[str, AuthorHistory] = {}
    who_map = canonical_identities(history, ident)
    for who, h in history.items():
        key = who_map[who].lower()
        tgt = out.setdefault(key, AuthorHistory())
        tgt.total_commits += h.total_commits
        if h.last_active and (tgt.last_active is None or h.last_active > tgt.last_active):
            tgt.last_active = h.last_active
        for m, ms in h.modules.items():
            t = tgt.modules.setdefault(m, type(ms)())
            t.commits += ms.commits
            t.weight += ms.weight
            if ms.last and (t.last is None or ms.last > t.last):
                t.last = ms.last
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--max-prs", type=int, default=50)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = Config()
    mmap = build_module_map(args.repo.resolve())
    print(f"模块: {len(mmap.modules)} 个")

    print("拉取 merged PR 与身份映射 ...")
    prs = fetch_merged_prs(args.owner, args.name, max_prs=args.max_prs * 2)
    ident = build_identity_map(args.owner, args.name, commits_to_scan=500)
    print(f"PR: {len(prs)} 个; 身份映射: {len(ident['email'])} email, {len(ident['name'])} name")

    usable = [p for p in prs if real_reviewers(p, cfg)]
    print(f"有真人 review 记录的 PR: {len(usable)} 个 (其余不参与评估)\n")

    hits1 = hitsk = 0
    rr_sum = 0.0
    evaluated = 0
    for pr in usable[: args.max_prs]:
        ref = dt.datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
        files = [f["path"] for f in pr["files"]["nodes"]]
        modules = cfg.touched_modules(mmap.of(f) for f in files)
        pr_author = (pr["author"] or {}).get("login")

        raw_hist = collect_history(args.repo.resolve(), mmap, cfg, ref, before=pr["createdAt"])
        history = canonicalize(raw_hist, ident)
        rsig = review_signals(prs, mmap, cfg, ref)
        cands = recommend(modules, history, rsig, cfg, ref, pr_author=pr_author)

        truth = real_reviewers(pr, cfg)
        top = [c.key.lower() for c in cands[: args.top]]
        if not top:
            continue
        evaluated += 1
        rank = next((i + 1 for i, t in enumerate(top) if t in truth), None)
        if rank == 1:
            hits1 += 1
        if rank:
            hitsk += 1
            rr_sum += 1.0 / rank

        if args.verbose:
            title = pr["title"][:38]
            print(f"#{pr['number']} [{','.join(modules)[:60]}] {title}")
            for c in cands[: args.top]:
                bits = []
                for m, (nc, _cw, nr, _rw) in sorted(
                    c.module_detail.items(), key=lambda kv: -(kv[1][1] + kv[1][3])
                ):
                    parts = []
                    if nc:
                        parts.append(f"{nc}c")
                    if nr:
                        parts.append(f"{nr}r")
                    bits.append(f"{m}:{'+'.join(parts) if parts else '?'}")
                extra = " 跨模块" if c.coupling > 0 else ""
                if c.seams:
                    extra += " " + "、".join(c.seams[:2])
                print(f"  {c.score:5.1f} {c.key:16} {' · '.join(bits) or '-'}{extra}")
            print(f"  实际: {', '.join(sorted(truth))} {'✓ HIT' if rank else '✗ MISS'}")

    if evaluated:
        print(f"\n=== 回测结果 ({evaluated} 个 PR, top-{args.top}) ===")
        print(f"Top-1 命中率: {hits1}/{evaluated} = {hits1/evaluated:.0%}")
        print(f"Top-{args.top} 命中率: {hitsk}/{evaluated} = {hitsk/evaluated:.0%}")
        print(f"MRR: {rr_sum/evaluated:.3f}")
    else:
        print("没有可评估的 PR（均无真人 review 记录或模块无法解析）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
