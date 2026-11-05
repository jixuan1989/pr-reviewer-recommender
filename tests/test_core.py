# SPDX-License-Identifier: Apache-2.0
import datetime as dt
import subprocess
from pathlib import Path

import pytest

from reviewer_recommender.config import Config, UNASSIGNED
from reviewer_recommender.deps import build_dep_graph
from reviewer_recommender.history import ModuleSignal, collect_history
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
