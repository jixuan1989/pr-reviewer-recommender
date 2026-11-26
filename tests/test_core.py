# SPDX-License-Identifier: Apache-2.0
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from reviewer_recommender.config import Config, UNASSIGNED
from reviewer_recommender.deps import build_dep_graph
from reviewer_recommender.history import ModuleSignal, collect_history, parse_time
from reviewer_recommender.modules import build_module_map
from reviewer_recommender.score import recommend

REF = dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc)
CFG = Config()


# ---------- modules ----------

def test_module_map_maven_and_markers(tmp_path):
    (tmp_path / "java" / "tsfile").mkdir(parents=True)
    (tmp_path / "java" / "common").mkdir(parents=True)
    (tmp_path / "cpp").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "java" / "pom.xml").write_text(
        "<project><modules><module>tsfile</module><module>common</module></modules></project>"
    )
    (tmp_path / "cpp" / "CMakeLists.txt").write_text("")
    m = build_module_map(tmp_path)
    assert m.of("java/tsfile/src/A.java") == "java/tsfile"
    assert m.of("java/common/x/B.java") == "java/common"
    assert m.of("cpp/src/x.cc") == "cpp"
    assert m.of("README.md") == UNASSIGNED
    # longest prefix wins over shallower dirs
    assert m.of("java/tsfile/pom.xml") == "java/tsfile"


def test_module_map_fallback_top_dirs(tmp_path):
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
    m = build_module_map(tmp_path)
    assert m.of("a/x.py") == "a"


def test_build_output_dirs_are_skipped_by_exact_name(tmp_path):
    """`dist` is build output; `distribution` is a module of its own."""
    for d in ("distribution", "dist", "build", "server"):
        (tmp_path / d).mkdir()
    (tmp_path / "pom.xml").write_text(
        "<project><modules><module>distribution</module><module>dist</module>"
        "<module>server</module></modules></project>"
    )
    assert set(build_module_map(tmp_path).modules) == {"distribution", "server"}


# ---------- configuration ----------

@pytest.mark.parametrize("login", ["dependabot[bot]", "github-actions", "codecov-commenter",
                                   "renovate-bot", "Copilot", "semantic-release", "ci[bot] <ci@x>"])
def test_bots_are_recognised(login):
    assert CFG.is_bot(login)


@pytest.mark.parametrize("login", ["Talbot", "robotman", "abby", "abb", "bob", "Bottom",
                                   "codecove", "AllBotsAreHere"])
def test_people_whose_handle_contains_a_bot_word_are_not_bots(login):
    assert not CFG.is_bot(login)


def test_timestamps_with_and_without_zone():
    assert parse_time("2026-11-03T07:04:59Z") == dt.datetime(2026, 11, 3, 7, 4, 59, tzinfo=dt.timezone.utc)
    assert parse_time("2026-11-03T15:04:59+08:00") == parse_time("2026-11-03T07:04:59Z")
    assert parse_time("2026-11-03T07:04:59").tzinfo == dt.timezone.utc   # bare stamps are UTC


# ---------- scoring ----------

def _hist(author, module, commits, days_ago=10):
    from reviewer_recommender.history import AuthorHistory, ModuleSignal

    h = AuthorHistory()
    when = REF - dt.timedelta(days=days_ago)
    ms = ModuleSignal(commits=commits, weight=commits * CFG.decay(when, REF), last=when)
    h.modules[module] = ms
    h.last_active = when
    h.total_commits = commits
    return {author: h}


def test_decay():
    old = REF - dt.timedelta(days=180)
    assert CFG.decay(old, REF) == pytest.approx(0.5)
    assert CFG.decay(REF, REF) == pytest.approx(1.0)


def test_recommend_filters():
    h = {}
    h.update(_hist("alice <a@x>", "m1", 5))
    h.update(_hist("bob[bot] <b@x>", "m1", 99))     # bot
    h.update(_hist("carol <c@x>", "m1", 2, days_ago=400))
    h.update(_hist("author <z@x>", "m1", 50))        # pr author
    out = recommend(["m1"], h, None, CFG, REF, pr_author="author")
    names = [c.key for c in out]
    assert "bob[bot]" not in str(names)
    assert "author" not in names
    assert names[0] == "alice"


def test_pr_author_is_excluded_by_login_whatever_the_case():
    """History keyed by GitHub login, as the action passes it in."""
    hist = {}
    for login in ("alice", "ALICE", "bob"):
        hist.update(_hist(f"{login} <{login}@x>", "m1", 5))
    keys = [c.key for c in recommend(["m1"], hist, None, CFG, REF, pr_author="Alice")]
    assert keys == ["bob"]


def test_touched_modules_filters_root():
    assert CFG.touched_modules(["java/a", UNASSIGNED]) == ["java/a"]
    assert CFG.touched_modules([UNASSIGNED]) == [UNASSIGNED]
    assert CFG.touched_modules(["b/a", "b/c"]) == ["b/a", "b/c"]


def test_root_downweighted():
    h = _hist("alice <a@x>", UNASSIGNED, 10)
    full = 10 * CFG.decay(REF - dt.timedelta(days=10), REF)
    out = recommend([UNASSIGNED], h, None, CFG, REF)
    assert out[0].score == pytest.approx(full * CFG.root_weight)
    out2 = recommend(["m1"], _hist("bob <b@x>", "m1", 10), None, CFG, REF)
    assert out2[0].score == pytest.approx(full)  # normal modules unaffected


def test_coupling_bonus():
    both = {}
    both.update(_hist("alice <a@x>", "m1", 5))
    both["alice <a@x>"].modules["m2"] = both["alice <a@x>"].modules["m1"]
    single = _hist("bob <b@x>", "m1", 8)
    out = recommend(["m1", "m2"], {**both, **single}, None, CFG, REF)
    a = next(c for c in out if c.key == "alice")
    b = next(c for c in out if c.key == "bob")
    assert a.coupling > 0
    assert b.coupling == 0


# ---------- history (real mini git repo) ----------

# ---------- v2: dep graph / seam bonus / workload ----------

def test_dep_graph_reactor_edges(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><modules><module>a</module><module>b</module></modules></project>"
    )
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
    (tmp_path / "a" / "pom.xml").write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
        <groupId>g</groupId><artifactId>a</artifactId>
        <dependencies>
          <dependency><groupId>g</groupId><artifactId>b</artifactId></dependency>
          <dependency><groupId>ext</groupId><artifactId>outside</artifactId></dependency>
        </dependencies></project>"""
    )
    (tmp_path / "b" / "pom.xml").write_text(
        "<project><parent><groupId>g</groupId><artifactId>root</artifactId></parent>"
        "<artifactId>b</artifactId></project>"
    )
    mmap = build_module_map(tmp_path)
    g = build_dep_graph(tmp_path, mmap)
    assert g.neighbors("a") == {"b"}   # reactor 内部边保留，外部依赖丢弃
    assert g.neighbors("b") == {"a"}   # 双向


def test_seam_bonus():
    h = _hist("alice <a@x>", "m1", 5)
    when = REF - dt.timedelta(days=10)
    h["alice <a@x>"].modules["n1"] = ModuleSignal(
        commits=4, weight=4 * CFG.decay(when, REF), last=when
    )
    out = recommend(["m1"], h, None, CFG, REF, dep_adj={"m1": {"n1"}})
    a = out[0]
    assert a.dep_score == pytest.approx(CFG.dep_weight * min(5, 4) * CFG.decay(when, REF))
    assert "m1↔n1" in a.seams
    # 无邻居历史则无加成
    out2 = recommend(["m1"], _hist("bob <b@x>", "m1", 5), None, CFG, REF, dep_adj={"m1": {"n1"}})
    assert out2[0].dep_score == 0


def test_workload_penalty_and_cap():
    w = 10 * CFG.decay(REF - dt.timedelta(days=10), REF)
    out = recommend(["m1"], _hist("bob <b@x>", "m1", 10), None, CFG, REF, pending={"bob": 2})
    assert out[0].score == pytest.approx(w * CFG.workload_factor ** 2)
    assert out[0].pending == 2
    out2 = recommend(["m1"], _hist("carol <c@x>", "m1", 10), None, CFG, REF, pending={"carol": CFG.max_pending})
    assert out2 == []  # 过载直接排除


def test_top_n_clamp_and_low_confidence():
    assert CFG.effective_top_n(1) == 2      # 社区反馈：至少 2 个
    assert CFG.effective_top_n() == 3
    assert CFG.effective_top_n(5) == 5
    # 证据计数判定：冷模块（iotdb-client/cli 实测 top 仅 4 次）vs 热模块（java/tsfile 21c+）
    assert CFG.is_low_confidence(4)
    assert not CFG.is_low_confidence(21)


def test_evidence_count_excludes_root_and_bonuses():
    h = _hist("alice <a@x>", "m1", 8)
    out = recommend(["m1"], h, None, CFG, REF)
    assert out[0].evidence_count == 8
    # (root) 证据不计入
    h2 = _hist("bob <b@x>", UNASSIGNED, 30)
    h2["bob <b@x>"].modules["m1"] = h2["bob <b@x>"].modules[UNASSIGNED]
    out2 = recommend(["m1"], h2, None, CFG, REF)
    assert out2[0].evidence_count == 30  # 只有 m1 的 30c 计入


def test_collect_history(tmp_path):
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), *a], check=True, capture_output=True
    )
    run("init", "-q")
    run("config", "user.email", "a@x")
    run("config", "user.name", "alice")
    (repo / "src" / "f.py").write_text("x=1\n")
    run("add", ".")
    run("commit", "-q", "-m", "c1", "--date=2026-11-01T00:00:00+00:00")
    mmap = build_module_map(repo)
    h = collect_history(repo, mmap, CFG, REF)
    assert "alice <a@x>" in h
    assert h["alice <a@x>"].modules["src"].commits == 1


def test_the_same_pr_gets_the_same_recommendation_in_every_process():
    """Set iteration order changes from one process to the next (hash
    randomisation). Nothing that ends up in the comment may depend on it."""
    code = """
import datetime as dt, sys
sys.path.insert(0, ".")
from reviewer_recommender.config import Config
from reviewer_recommender.history import AuthorHistory, ModuleSignal
from reviewer_recommender.score import recommend

names = ["a", "bb", "ccc", "dddd", "e5", "f6", "g7", "h8"]
mods = ["java/common", "java/tsfile", "java/tools", "java/examples", "java/x", "java/y"]
history = {}
for i, n in enumerate(names):
    h = AuthorHistory(total_commits=1)
    for j, m in enumerate(mods):
        if (i + j) % 3:                      # a different subset for everyone, all of equal weight
            h.modules[m] = ModuleSignal(commits=2, weight=1.0)
    history[f"{n} <{n}@x>"] = h
adj = {m: {mods[(k + 1) % len(mods)], mods[(k + 2) % len(mods)]} for k, m in enumerate(mods)}
out = recommend(mods[:3], history, None, Config(),
                dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), dep_adj=adj)
print([(c.key, c.score, list(c.module_detail), c.seams) for c in out])
"""
    seen = set()
    for seed in ("0", "1", "2", "3", "4", "5"):
        run = subprocess.run(
            [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, check=True,
        )
        seen.add(run.stdout)
    assert len(seen) == 1, f"{len(seen)} different recommendations for one pull request"


def test_action_file_checks_out_the_base_commit_only_and_wires_every_input():
    """action.yml runs on pull_request_target with a write token, and nothing
    here is exercised by the other tests. Three properties are load-bearing and
    easy to undo with a well-meant edit: the checkout is of the PR's base commit
    (never the PR head or the default branch), it leaves no credentials behind,
    and the checkout action is pinned to a commit. An input that is declared but
    not passed on would be a knob that silently does nothing."""
    text = (Path(__file__).resolve().parents[1] / "action.yml").read_text(encoding="utf-8")
    checkout = re.search(r"^\s*uses:\s*(\S+)", text, flags=re.M)
    assert checkout and re.fullmatch(r"actions/checkout@[0-9a-f]{40}", checkout.group(1))
    assert re.findall(r"^\s*ref:\s*(.+?)\s*$", text, flags=re.M) == [
        "${{ github.event.pull_request.base.sha }}"
    ]
    assert re.search(r"^\s*persist-credentials:\s*false\s*$", text, flags=re.M)
    declared = text.split("\ninputs:", 1)[1].split("\nruns:", 1)[0]
    names = re.findall(r"^  ([a-z0-9-]+):\s*$", declared, flags=re.M)
    assert names, "no inputs found: the layout of action.yml changed"
    for name in names:
        assert "${{ inputs." + name + " }}" in text, f"input {name!r} is declared but never used"
