# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the GitHub-side signals and for what the comment says about
review requests — plain Python, no gh and no network."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from fake_github import merged_pr

from reviewer_recommender import action, reviewlog
from reviewer_recommender.config import Config, UNASSIGNED
from reviewer_recommender.history import AuthorHistory, ModuleSignal
from reviewer_recommender.reviewlog import (
    GhError,
    IdentityIndex,
    decayed_counts,
    merge_identities,
    review_events,
    reviewers_of,
    unresolved_authors,
)
from reviewer_recommender.score import Candidate

CFG = Config()


def _at(day: int) -> dt.datetime:
    return reviewlog.parse_time(f"2026-05-{day:02d}T00:00:00Z")


class _TwoModules:
    """path -> module, for files under java/a or java/b; anything else is (root)."""

    def of(self, path: str) -> str:
        return "/".join(path.split("/")[:2]) if "/" in path else "(root)"


# ---------- who counts as a reviewer ----------

def test_reviewers_of_counts_other_humans_only():
    pr = merged_pr(1, "2026-05-01T00:00:00Z", "Alice", ["java/a/X.java"], [
        ("alice", "COMMENTED", "2026-05-02T00:00:00Z"),        # the author replying inline
        ("Bob", "APPROVED", "2026-05-02T00:00:00Z"),
        ("bob", "CHANGES_REQUESTED", "2026-05-03T00:00:00Z"),  # same person, once, lowercase
        ("codecov-commenter", "COMMENTED", "2026-05-02T00:00:00Z"),
        ("carol", "PENDING", "2026-05-02T00:00:00Z"),          # not submitted yet
    ])
    pr["reviews"]["nodes"].append({"author": None, "state": "APPROVED",   # deleted account
                                   "submittedAt": "2026-05-02T00:00:00Z"})
    assert reviewers_of(pr, CFG) == {"bob"}


def test_accounts_github_labels_as_bots_never_review():
    """`github-advanced-security` posts code-scanning remarks as reviews. Its
    name gives no hint; GitHub types the account as a Bot."""
    scanner = {"author": {"login": "github-advanced-security", "__typename": "Bot"},
               "state": "COMMENTED", "submittedAt": "2026-05-02T00:00:00Z"}
    pr = merged_pr(1, "2026-05-01T00:00:00Z", "alice", ["java/a/X.java"], [])
    pr["reviews"]["nodes"].append(scanner)
    assert reviewers_of(pr, CFG) == set()
    assert review_events([pr], _TwoModules(), CFG, _at(5)) == []
    # a person is whatever their handle looks like, when GitHub says "User"
    person = merged_pr(2, "2026-05-01T00:00:00Z", "alice", ["java/a/X.java"],
                       [("bob", "APPROVED", "2026-05-02T00:00:00Z")])
    person["reviews"]["nodes"][0]["author"]["__typename"] = "User"
    assert reviewers_of(person, CFG) == {"bob"}


def test_review_signals_use_only_what_existed_at_the_time():
    pr = merged_pr(1, "2026-05-01T00:00:00Z", "alice",
                   ["java/a/X.java", "java/b/Y.java", "README.md"],
                   [("alice", "APPROVED", "2026-05-02T00:00:00Z"),   # self-review: no signal
                    ("bob", "APPROVED", "2026-05-02T00:00:00Z"),
                    ("carol", "APPROVED", "2026-05-20T00:00:00Z")])
    later = merged_pr(2, "2026-05-10T00:00:00Z", "dave", ["java/a/Z.java"],
                      [("bob", "APPROVED", "2026-05-02T00:00:00Z")])
    mmap = _TwoModules()

    assert review_events([pr, later], mmap, CFG, _at(5)) == [
        ("bob", "java/a", _at(2)), ("bob", "java/b", _at(2)),   # README.md is (root): not scored
    ]
    assert review_events([pr, later], mmap, CFG, _at(10)) == [
        ("bob", "java/a", _at(2)), ("bob", "java/b", _at(2)),   # PR 2 created at the reference time
    ]
    assert review_events([pr, later], mmap, CFG, _at(12)) == [
        ("bob", "java/a", _at(2)), ("bob", "java/b", _at(2)), ("bob", "java/a", _at(2)),
    ]                                                        # carol's review was submitted later
    assert review_events([pr, later], mmap, CFG, _at(25)) == [
        ("bob", "java/a", _at(2)), ("bob", "java/b", _at(2)),
        ("carol", "java/a", _at(20)), ("carol", "java/b", _at(20)),   # one review, both modules
        ("bob", "java/a", _at(2)),
    ]


def test_decayed_counts_sums_the_weights():
    events = [("bob", "java/a", _at(2)), ("bob", "java/a", _at(3))]
    assert decayed_counts(events, CFG, _at(10)) == {
        ("bob", "java/a"): (2, pytest.approx(CFG.decay(_at(2), _at(10)) + CFG.decay(_at(3), _at(10))))
    }


# ---------- identity: git author -> GitHub login ----------

def test_identity_is_resolved_by_email_and_never_by_name():
    idx = IdentityIndex(email_to_login={"alice@x": "alice"}, login_case={"alice": "Alice"})
    assert idx.resolve("Alice <alice@x>") == "alice"
    assert idx.resolve("A. Smith <ALICE@X>") == "alice"   # the address is what GitHub links
    assert idx.resolve("alice <other@x>") is None         # a name that happens to be a login
    assert idx.resolve("alice") is None                   # no address at all
    assert idx.display("alice") == "Alice" and idx.display("bob") == "bob"


def _history(commits: dict[str, int], last: dt.datetime | None = None) -> AuthorHistory:
    """module -> commit count (the decayed weight is made half the count)."""
    h = AuthorHistory(total_commits=sum(commits.values()), last_active=last)
    for m, n in commits.items():
        h.modules[m] = ModuleSignal(commits=n, weight=0.5 * n, last=last)
    return h


def test_history_is_keyed_by_login_and_unlinked_authors_are_dropped():
    history = {
        "Alice <alice@x>": _history({"java/a": 3}, _at(2)),
        "A. Smith <ALICE@X>": _history({"java/a": 2, "java/b": 2}, _at(9)),
        "alice <other@x>": _history({"java/a": 9}),      # same name, another account
        "Ghost <ghost@x>": _history({"java/a": 5}),      # no GitHub account
        "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>": _history({}, _at(9)),
    }
    idx = IdentityIndex(email_to_login={"alice@x": "alice", "other@x": "someone"})
    merged = merge_identities(history, idx)
    assert set(merged) == {"alice", "someone"}
    assert merged["alice"].total_commits == 7
    assert merged["alice"].modules["java/a"].commits == 5
    assert merged["alice"].modules["java/a"].weight == pytest.approx(2.5)
    assert merged["alice"].modules["java/b"].commits == 2
    assert merged["someone"].modules["java/a"].commits == 9 and "java/b" not in merged["someone"].modules
    assert merged["alice"].last_active == _at(9)
    assert unresolved_authors(history, idx) == ["Ghost <ghost@x>"]   # the bot is not "unlinked"


# ---------- gh failures ----------

@pytest.mark.parametrize("stderr, status, retryable", [
    ("gh: Server Error (HTTP 500)\n", 500, True),
    ("gh: Request Timeout (HTTP 408)\n", 408, True),
    ("gh: API rate limit exceeded (HTTP 403)\n", 403, True),
    ("gh: Too many requests (HTTP 429)\n", 429, True),
    ('gh: Get "http://x/y": dial tcp 127.0.0.1:1: connect: connection refused\n', None, True),
    ("gh: Resource not accessible by integration (HTTP 403)\n", 403, False),
    ("gh: Not Found (HTTP 404)\n", 404, False),
    ('gh: {"message":"Validation Failed"} (HTTP 422)\n', 422, False),
    ("unknown flag: --nope\n", None, False),
    ("Usage:\n  gh api <endpoint>\n", None, False),
])
def test_which_failures_are_worth_retrying(stderr, status, retryable):
    assert reviewlog._http_status(stderr) == status
    assert reviewlog._retryable(stderr, status) is retryable


def test_gh_output_must_be_json(monkeypatch):
    import subprocess

    monkeypatch.setattr(reviewlog.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="not json", stderr=""))
    with pytest.raises(GhError) as info:
        reviewlog.gh_json(["api", "user"])
    assert "non-JSON" in str(info.value)
    assert reviewlog._json_values('{"id": 1}\n{"id": 2}\n[3]\n', []) == [{"id": 1}, {"id": 2}, [3]]


def test_a_missing_gh_binary_is_an_error_not_a_traceback(monkeypatch):
    monkeypatch.setenv("GH_BIN", "/nonexistent/gh")
    with pytest.raises(GhError) as info:
        reviewlog.gh_api(["api", "user"])
    assert "not found" in str(info.value)


def test_hint_names_the_fix_for_a_read_only_token():
    hint = lambda stderr, status: action._hint(GhError([], stderr, status))  # noqa: E731
    assert "pull_request_target" in hint("gh: Resource not accessible by integration (HTTP 403)", 403)
    limited = hint("gh: API rate limit exceeded (HTTP 403)", 403)
    assert "rate limit" in limited.lower() and "pull_request_target" not in limited
    assert hint("gh: Not Found (HTTP 404)", 404)
    assert hint("gh: Validation Failed (HTTP 422)", 422) == ""


def test_env_flags_accept_the_usual_spellings(monkeypatch):
    for raw, value in (("TRUE", True), ("1", True), ("yes", True), ("False", False), ("0", False), ("no", False)):
        monkeypatch.setenv("FLAG", raw)
        assert action._env_flag("FLAG") is value
    monkeypatch.delenv("FLAG")
    assert action._env_flag("FLAG", default=True) is True
    monkeypatch.setenv("FLAG", "on")
    with pytest.raises(ValueError):
        action._env_flag("FLAG")


# ---------- what the comment says ----------

def _outcome(enabled=True, dry_run=False, requested=(), already=(), failed=()):
    return action.Requests(enabled, dry_run, list(requested), list(already), list(failed))


def test_comment_reports_review_requests_as_they_happened():
    foot = lambda outcome: action._footer(outcome, str)  # noqa: E731
    assert foot(_outcome(requested=["alice", "bob"])) == ["Reviews requested from @alice and @bob."]
    assert foot(_outcome(requested=["alice"], already=["bob"])) == [
        "Reviews requested from @alice. Already requested: @bob."]
    refused = "Reviews may only be requested from collaborators. (HTTP 422)"
    assert foot(_outcome(requested=["alice"], failed=[("bob", refused)])) == [
        f"Reviews requested from @alice. Could not request a review from @bob ({refused}); "
        "add @bob in the Reviewers box."]
    assert foot(_outcome(failed=[("bob", "x (HTTP 403)"), ("carol", "y (HTTP 403)")])) == [
        "Could not request a review from @bob (x (HTTP 403)); add @bob in the Reviewers box. "
        "Could not request a review from @carol (y (HTTP 403)); add @carol in the Reviewers box."]
    assert foot(_outcome(failed=[("bob", refused), ("carol", refused)])) == [   # one reason: said once
        f"Could not request a review from @bob and @carol ({refused}); "
        "add @bob and @carol in the Reviewers box."]
    assert foot(_outcome(enabled=False, requested=["alice"])) == [
        "No review was requested (`request-reviewers: false`)."]
    assert foot(_outcome(dry_run=True, requested=["alice", "bob"], already=["carol"])) == [
        "Would request reviews from @alice and @bob. Already requested: @carol."]
    assert foot(_outcome(dry_run=True)) == []


def test_reason_keeps_the_first_sentence_and_the_status():
    reason = lambda stderr, status=None: action._reason(GhError([], stderr, status))  # noqa: E731
    github = ("gh: Reviews may only be requested from collaborators. One or more of the users "
              "or teams you specified is not a collaborator  (HTTP 422)\n")
    assert reason(github, 422) == "Reviews may only be requested from collaborators. (HTTP 422)"
    assert reason('gh: For \'properties/reviewers\', "a,b" is not an array. (HTTP 422)\n') == \
        'For \'properties/reviewers\', "a,b" is not an array. (HTTP 422)'
    assert reason("gh: Resource not accessible by integration (HTTP 403)\n") == \
        "Resource not accessible by integration (HTTP 403)"
    long = "gh: " + "x" * 300 + "\n"                  # no sentence to cut at: capped, marked as cut
    assert len(reason(long)) == 121 and reason(long).endswith("…")
    assert reason("", 503) == "HTTP 503" and reason("") == "gh failed"


def _candidate(login: str, score: float, evidence: int = 10) -> Candidate:
    c = Candidate(name=login, score=score)
    c.module_detail = {"java/a": (evidence, float(evidence), 0, 0.0)}
    return c


def test_comment_table_shows_logins_the_way_github_does():
    ident = IdentityIndex(email_to_login={"bob@x": "bobhu"}, login_case={"bobhu": "BobHu"})
    body = action._render([_candidate("bobhu", 9.0), _candidate("alice", 8.0)], ["java/a"], CFG,
                          _outcome(requested=["bobhu", "alice"]), ident)
    assert body.splitlines()[0] == action.MARKER
    assert "| @BobHu | 9.0 | `java/a` 10 commits |" in body
    assert "| @alice | 8.0 | `java/a` 10 commits |" in body
    assert "Reviews requested from @BobHu and @alice." in body
    assert "Low confidence" not in body
    assert body.index("Reviews requested from") < body.index("<details>")   # the status is not folded away


def test_comment_warns_when_the_evidence_is_thin():
    body = action._render([_candidate("alice", 1.0, evidence=4)], ["java/a"], CFG,
                          _outcome(requested=["alice"]))
    assert "Low confidence" in body and "only 4 commits" in body


def test_comment_counts_say_commit_and_review_when_there_is_one():
    c = Candidate(name="alice", score=3.0)
    c.module_detail = {"java/a": (1, 1.0, 0, 0.0), "java/b": (0, 0.0, 1, 0.5)}
    assert action._why(c) == "`java/a` 1 commit · `java/b` 1 review"
    c.module_detail = {"java/a": (2, 1.0, 3, 0.5)}
    assert action._why(c) == "`java/a` 2 commits, 3 reviews"


def test_comment_names_the_three_bonuses_and_the_workload():
    c = Candidate(name="alice", score=3.0, coupling=0.5, pending=1,
                  seams=["java/a↔java/c", "java/a↔java/d", "java/b↔java/e"])
    c.module_detail = {"java/a": (2, 1.0, 3, 0.5)}
    assert action._why(c) == (
        "`java/a` 2 commits, 3 reviews · **history in every touched module**"
        " · on both sides of `java/a↔java/c` and `java/a↔java/d` · 1 review pending")   # two seams at most
    c = Candidate(name="bob", score=2.0)
    assert action._why(c) == "-"                  # (root) only: nothing to show


def test_comment_for_a_root_only_pull_request():
    """`(root)` is left out of the evidence count, so a PR outside every module
    is always low-confidence. The warning must not talk of 'only 0 commits'."""
    top = Candidate(name="alice", score=3.0)
    top.module_detail = {"(root)": (30, 15.0, 0, 0.0)}     # the only kind of evidence here
    assert top.evidence_count == 0
    body = action._render([top], [UNASSIGNED], CFG, _outcome(requested=["alice"]))
    assert body.splitlines()[2] == ("This PR touches no module: its files are outside "
                                    "every module found in the repository.")
    assert "Low confidence" in body and "touches no module, so there is no module history" in body
    assert "only 0" not in body


def test_comment_lists_the_modules_with_a_comma_and_a_final_and():
    line = action._modules_line   # (root) is filtered out before the line is built
    assert line(["java/a"]) == "This PR touches `java/a`."
    assert line(["java/a", "java/b", "java/c"]) == "This PR touches `java/a`, `java/b` and `java/c`."
    assert action._join(["x"]) == "x"


def test_readme_shows_what_the_action_renders():
    """The sample comment in the README is what the code writes for two made-up
    candidates, not something typed by hand and later out of date."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme.split("### What the comment looks like", 1)[1]
    sample = section.split("```markdown\n", 1)[1].split("```", 1)[0].rstrip("\n")
    top = Candidate(name="alice", score=15.8, coupling=0.5)
    top.module_detail = {"java/tsfile": (12, 6.0, 4, 2.0), "java/common": (6, 3.0, 0, 0.0)}
    second = Candidate(name="bob", score=7.4, coupling=0.5)
    second.module_detail = {"java/tsfile": (16, 8.0, 0, 0.0), "java/common": (1, 0.5, 0, 0.0)}
    assert sample == action._render([top, second], ["java/common", "java/tsfile"], CFG,
                                    _outcome(enabled=False))
