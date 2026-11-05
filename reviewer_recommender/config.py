# SPDX-License-Identifier: Apache-2.0
"""Scoring configuration."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

BOT_PATTERNS = ("[bot]", "bot", "github-actions", "dependabot", "renovate", "codecov", "semantic-release", "copilot")
UNASSIGNED = "(root)"


@dataclass(frozen=True)
class Config:
    halflife_days: float = 180.0
    commit_weight: float = 1.0        # λc
    review_weight: float = 2.5        # λr（调参结论：review 行为是最强预测信号，2026-11-05 网格搜索）
    coupling_weight: float = 0.5      # λx
    dep_weight: float = 0.3           # λd：依赖接缝加成（v2）
    workload_factor: float = 0.75     # 每个挂起 review 请求对总分的乘数（v2）
    max_pending: int = 6              # 挂起请求达到该数则排除（v2）
    low_confidence_min_evidence: int = 5  # top 候选触及模块证据数(commit+review)低于此 → 低置信（v2.1）
    root_weight: float = 0.2          # (root)/infra 路径信号降权
    window_days: int = 365            # 信号采集窗口
    inactive_days: int = 365          # 超过该天数无动作则排除
    top_n: int = 3
    bot_patterns: tuple[str, ...] = field(default_factory=lambda: BOT_PATTERNS)

    def touched_modules(self, modules) -> list[str]:
        """Scoring view of a module set: infra/(root) excluded unless it is all there is."""
        real = [m for m in set(modules) if m != UNASSIGNED]
        return sorted(real) if real else [UNASSIGNED]

    def effective_top_n(self, requested: int | None = None) -> int:
        """Recommend at least 2 reviewers — single-name lists leave no backup (社区反馈 2026-11-05)."""
        n = requested if requested is not None else self.top_n
        return max(2, n)

    def is_low_confidence(self, evidence_count: int) -> bool:
        """Cold-module heuristic: evidence = commit+review count of the top candidate
        across touched modules. Count-based (not score-based) so it is invariant to
        repo size, decay window and bonus weights (seam/coupling can inflate scores)."""
        return evidence_count < self.low_confidence_min_evidence

    def decay(self, when: dt.datetime, ref: dt.datetime) -> float:
        """Exponential time-decay weight, halflife `halflife_days`."""
        age_days = max((ref - when).total_seconds() / 86400.0, 0.0)
        return 0.5 ** (age_days / self.halflife_days)

    def is_bot(self, identity: str) -> bool:
        low = identity.lower()
        return any(p in low for p in self.bot_patterns)
