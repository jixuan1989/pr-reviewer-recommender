# SPDX-License-Identifier: Apache-2.0
"""Generate a suggested CODEOWNERS from decayed module history (v2).

  python scripts/gen_codeowners.py --owner apache --name iotdb \
      --repo /path/to/repos/iotdb [--top 2] [--output CODEOWNERS.suggested]

Output goes to stdout unless --output is given. Reviewers are login-resolved;
modules with fewer than --min-commits total signal are skipped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewer_recommender.config import Config, UNASSIGNED  # noqa: E402
from reviewer_recommender.history import collect_history  # noqa: E402
from reviewer_recommender.modules import build_module_map  # noqa: E402
from reviewer_recommender.reviewlog import build_identity_map, canonical_identities  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--top", type=int, default=2)
    ap.add_argument("--min-weight", type=float, default=3.0, help="模块最低衰减权重，低于则跳过")
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args()

    cfg = Config()
    ref = dt.datetime.now(tz=dt.timezone.utc)
    repo = args.repo.resolve()
    mmap = build_module_map(repo)
    history = collect_history(repo, mmap, cfg, ref)
    ident = build_identity_map(args.owner, args.name, commits_to_scan=500)
    who_map = canonical_identities(history, ident)

    lines = [
        "# 由 pr-reviewer-recommender 生成（仅供维护者参考，非自动生效）",
        f"# 数据窗口: 近 {cfg.window_days} 天 commit 历史，半衰期 {cfg.halflife_days:.0f} 天，"
        f"生成于 {ref.date().isoformat()}",
        "",
    ]
    n_modules = 0
    for m in sorted(mmap.modules):
        scores: dict[str, float] = {}
        for who, h in history.items():
            ms = h.modules.get(m)
            if ms and ms.weight > 0 and not cfg.is_bot(who):
                scores[who_map.get(who, who)] = scores.get(who_map.get(who, who), 0.0) + ms.weight
        if not scores or sum(scores.values()) < args.min_weight:
            continue
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[: args.top]
        owners = " ".join(f"@{login}" for login, _ in ranked)
        lines.append(f"/{m}/ {owners}")
        n_modules += 1

    lines += ["", f"# 共 {n_modules} 个模块（{UNASSIGNED} 与低信号模块已跳过）"]
    text = "\n".join(lines) + "\n"
    if args.output:
        args.output.write_text(text)
        print(f"written -> {args.output} ({n_modules} modules)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
