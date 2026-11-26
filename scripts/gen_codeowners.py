# SPDX-License-Identifier: Apache-2.0
"""Generate a suggested CODEOWNERS from decayed module history.

  python scripts/gen_codeowners.py --owner apache --name iotdb \
      --repo /path/to/repos/iotdb [--top 2] [--min-weight 3.0] [--output CODEOWNERS.suggested]

Output goes to stdout unless --output is given. Owners are GitHub logins (git
author emails are resolved through the commits API; authors without a linked
account are left out). A module is skipped when its total decayed commit
weight is below --min-weight.
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
from reviewer_recommender.reviewlog import build_identity_index, merge_identities  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Suggest a CODEOWNERS file from module history")
    ap.add_argument("--owner", required=True, help="GitHub owner of the upstream repository")
    ap.add_argument("--name", required=True, help="GitHub repository name (upstream)")
    ap.add_argument("--repo", required=True, type=Path, help="path to an up-to-date clone")
    ap.add_argument("--top", type=int, default=2, help="owners listed per module")
    ap.add_argument("--min-weight", type=float, default=3.0, help="模块最低衰减权重，低于则跳过")
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = Config()
    ref = dt.datetime.now(tz=dt.timezone.utc)
    repo = args.repo.resolve()
    mmap = build_module_map(repo)
    history = collect_history(repo, mmap, cfg, ref)
    since = (ref - dt.timedelta(days=cfg.window_days)).isoformat()
    ident = build_identity_index(args.owner, args.name, since)
    owners = merge_identities(history, ident)

    lines = [
        "# 由 pr-reviewer-recommender 生成（仅供维护者参考，非自动生效）",
        f"# 数据窗口: 近 {cfg.window_days} 天 commit 历史，半衰期 {cfg.halflife_days:.0f} 天，"
        f"生成于 {ref.date().isoformat()}",
        f"# 身份解析: {len(ident.email_to_login)} 个 email -> login（扫描 {ident.commits_scanned} 个 commit"
        + ("，已达扫描上限" if ident.truncated else "") + "）",
        "",
    ]
    n_modules = 0
    for m in sorted(mmap.modules):
        scores: dict[str, float] = {}
        for login, h in owners.items():
            ms = h.modules.get(m)
            if ms and ms.weight > 0 and not cfg.is_bot(login):
                scores[login] = ms.weight
        if not scores or sum(scores.values()) < args.min_weight:
            continue
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[: args.top]
        lines.append(f"/{m}/ " + " ".join(f"@{ident.display(login)}" for login, _ in ranked))
        n_modules += 1

    lines += ["", f"# 共 {n_modules} 个模块（{UNASSIGNED} 与低信号模块已跳过）"]
    text = "\n".join(lines) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"written -> {args.output} ({n_modules} modules)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
