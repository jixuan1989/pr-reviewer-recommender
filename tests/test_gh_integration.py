# SPDX-License-Identifier: Apache-2.0
"""Tests that run the real `gh` CLI against an in-process fake GitHub.

They cover what a fake argv cannot: the body gh really sends (`reviewers[]`
has to serialize to an array), the output shape of `--paginate --jq`, how gh
reports HTTP errors, and the action's whole write path. Skipped when `gh` or
`bash` is not installed.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from reviewer_recommender import action, reviewlog
from reviewer_recommender.history import head_commit
from reviewer_recommender.reviewlog import (
    GhError,
    build_identity_index,
    fetch_merged_prs,
    gh_api,
    gh_json,
    gh_json_values,
    pr_files,
)
from fake_github import FakeGitHub, merged_pr   # noqa: E402  (tests/ is on sys.path under pytest)

OWNER, NAME, NUMBER = "acme", "widgets", 7
REF = "2026-11-10T00:00:00Z"          # creation time of the PR under review
COMMIT_DAY = "2026-10-01T00:00:00+00:00"
SLUG = f"repos/{OWNER}/{NAME}"
MARKER = action.MARKER

# Hands `gh api <endpoint>` to the fake server and leaves everything else
# (flags, field serialization, pagination, --jq) to the real CLI.
SHIM = """#!/usr/bin/env bash
real={real!r}
base={base!r}
args=(); skip=0; in_api=0; placed=0
for a in "$@"; do
  if (( skip )); then args+=("$a"); skip=0; continue; fi
  if (( in_api && ! placed )); then
    case "$a" in
      -X|-f|-F|-H|--jq|--input|--hostname|--cache|--field|--header|--raw-field) skip=1 ;;
      -*) ;;
      *) a="$base/$a"; placed=1 ;;
    esac
  fi
  [ "$a" = api ] && in_api=1
  args+=("$a")
done
exec "$real" "${{args[@]}}"
"""


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    gh, bash = shutil.which("gh"), shutil.which("bash")
    if not gh or not bash:
        pytest.skip("needs the gh CLI and bash")
    server = FakeGitHub()
    base = server.start()
    shim = tmp_path / "gh-shim.sh"
    shim.write_text(SHIM.format(real=gh, base=base), encoding="utf-8")
    shim.chmod(0o755)
    config = tmp_path / "gh-config"
    config.mkdir()
    monkeypatch.setenv("GH_BIN", str(shim))
    # A dummy token, an empty config dir and the default host: the CLI must
    # not touch the developer's own gh login, and nothing here can reach
    # github.com anyway (every endpoint is rewritten to the fake server).
    monkeypatch.setenv("GH_TOKEN", "dummy-not-a-real-token")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "dummy-not-a-real-token")
    monkeypatch.setenv("GH_CONFIG_DIR", str(config))
    monkeypatch.setenv("GH_HOST", "github.com")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(reviewlog.time, "sleep", lambda seconds: None)  # no back-off waits
    yield server
    server.stop()


def _no_writes(fake: FakeGitHub) -> list[tuple[str, str]]:
    """Requests other than reads and GraphQL queries (GraphQL is a POST)."""
    return [(method, path) for method, path, _ in fake.requests
            if method != "GET" and not path.endswith("graphql")]


# ---------- what gh sends and reports ----------

def test_reviewer_request_is_sent_as_an_array(fake_gh):
    endpoint = f"{SLUG}/pulls/{NUMBER}/requested_reviewers"
    for login in ("alice", "bob"):
        gh_api(["api", "-X", "POST", endpoint, "-f", f"reviewers[]={login}"])
    bodies = [body for _, body in fake_gh.calls("POST", "requested_reviewers")]
    assert bodies == [{"reviewers": ["alice"]}, {"reviewers": ["bob"]}]


def test_comma_joined_reviewers_is_the_rejected_string_form(fake_gh):
    """The original defect: `-f reviewers=a,b` posts a string; GitHub answers 422."""
    endpoint = f"{SLUG}/pulls/{NUMBER}/requested_reviewers"
    with pytest.raises(GhError) as info:
        gh_api(["api", "-X", "POST", endpoint, "-f", "reviewers=alice,bob"])
    # (the server sees the path without host)
    assert fake_gh.calls("POST", "requested_reviewers") == [(f"/{endpoint}", {"reviewers": "alice,bob"})]
    assert info.value.status == 422
    assert "is not an array" in action._reason(info.value)


def test_transient_errors_are_retried_client_errors_fail_fast(fake_gh):
    commits = ["api", "-X", "GET", f"{SLUG}/commits", "-f", "per_page=100", "-f", "page=1"]

    fake_gh.fail_queue.append(("commits", 500, 2))          # twice, then fine
    assert gh_json(commits) == []
    assert len(fake_gh.calls("GET", "commits")) == 3

    fake_gh.requests.clear()
    fake_gh.fail_queue.append(("commits", 403, 1))          # deterministic: no point in retrying
    with pytest.raises(GhError) as info:
        gh_json(commits)
    assert info.value.status == 403
    assert len(fake_gh.calls("GET", "commits")) == 1

    fake_gh.requests.clear()
    fake_gh.fail_queue.append(("commits", 429, 99))         # retryable, here forever
    with pytest.raises(GhError) as info:
        gh_json(commits)
    assert info.value.status == 429
    assert len(fake_gh.calls("GET", "commits")) == 3


def test_list_endpoints_must_not_take_f_fields(fake_gh):
    """`gh api --paginate <path> -f k=v` becomes a POST, which GitHub answers 404.
    That is why list calls put their parameters in the path."""
    with pytest.raises(GhError) as info:
        gh_json_values(["api", "--paginate", f"{SLUG}/pulls/{NUMBER}/files",
                        "-f", "per_page=100", "--jq", ".[] | {filename}"])
    assert info.value.status == 404
    assert fake_gh.calls("POST", "/files")


def test_big_file_lists_are_paged_completely(fake_gh):
    fake_gh.pull_files = [f"src/{i}.py" for i in range(250)]
    assert pr_files(OWNER, NAME, NUMBER) == fake_gh.pull_files
    assert len(fake_gh.calls("GET", f"/pulls/{NUMBER}/files")) == 3


def test_json_values_parse_one_object_per_line_and_whole_documents(fake_gh):
    fake_gh.comments = [{"id": i, "user": "u", "body": f"c{i}"} for i in range(5)]
    rows = gh_json_values(["api", "--paginate", f"{SLUG}/issues/{NUMBER}/comments",
                           "--jq", ".[] | {id, body}"])
    assert rows == [{"id": i, "body": f"c{i}"} for i in range(5)]
    # without `--jq`: one whole JSON document per page
    pages = gh_json_values(["api", "--paginate", f"{SLUG}/issues/{NUMBER}/comments?per_page=2"])
    assert [c["id"] for page in pages for c in page] == list(range(5))


def test_gh_error_keeps_the_first_words_of_the_invocation_only(fake_gh):
    with pytest.raises(GhError) as info:
        gh_api(["api", "-X", "POST", f"{SLUG}/pulls/{NUMBER}/requested_reviewers",
                "-f", "reviewers=alice,bob"], retries=1)
    assert info.value.cmd == "gh api -X POST"
    assert "alice,bob" in info.value.stderr      # the API's answer is kept ...
    assert "alice,bob" not in info.value.cmd     # ... but the payload is not echoed as a command


# ---------- reviewlog against the fake API ----------

def test_fetch_merged_prs_pages_and_completes_truncated_file_lists(fake_gh):
    fake_gh.merged = [merged_pr(i, "2026-01-01T00:00:00Z", "u", [f"f/{i}"]) for i in range(120)]
    big = fake_gh.merged[7]
    big["changedFiles"] = 150                      # GraphQL keeps only 100 of them
    fake_gh.merged_files[7] = [f"big/{i}" for i in range(150)]
    prs = fetch_merged_prs(OWNER, NAME, max_prs=120)
    assert [p["number"] for p in prs] == list(range(120))
    assert [f["path"] for f in prs[7]["files"]["nodes"]] == fake_gh.merged_files[7]
    assert len(fake_gh.calls("POST", "graphql")) == 3
    assert len(fake_gh.calls("GET", "/pulls/7/files")) == 2


def test_fetch_merged_prs_freezes_the_sample_at_the_cutoff(fake_gh):
    """Newest first. PRs updated after the cutoff are skipped — and paging
    goes on past them instead of stopping at the first trimmed page."""
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    fake_gh.merged = [merged_pr(i, "2025-12-01T00:00:00Z", "u", ["f"],
                                updated=(t0 + dt.timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
                      for i in reversed(range(120))]         # 119 (newest) .. 0
    prs = fetch_merged_prs(OWNER, NAME, max_prs=100, updated_before=t0 + dt.timedelta(days=110))
    assert [p["number"] for p in prs] == list(range(109, 9, -1))
    assert len(fake_gh.calls("POST", "graphql")) == 3


def test_identity_index_scans_the_window_and_reports_truncation(fake_gh):
    fake_gh.commits = [{"login": "Alice", "email": "alice@x", "date": "2026-10-01T00:00:00Z"},
                       {"login": "bob", "email": "BOB@x", "date": "2026-10-02T00:00:00Z"},
                       {"login": "", "email": "ghost@x", "date": "2026-10-03T00:00:00Z"}] * 100
    idx = build_identity_index(OWNER, NAME, "2025-11-10T00:00:00+00:00", until=REF, max_commits=10000)
    assert idx.commits_scanned == 300 and not idx.truncated
    assert idx.resolve("Any Name <ALICE@x>") == "alice"      # by email, whatever the name
    assert idx.resolve("Any Name <bob@x>") == "bob"
    assert idx.resolve("Ghost <ghost@x>") is None
    assert idx.display("alice") == "Alice"
    query = parse_qs(urlparse(fake_gh.calls("GET", "commits")[0][0]).query)
    assert query["since"] == ["2025-11-10T00:00:00+00:00"] and query["until"] == [REF]
    assert "sha" not in query                                 # no ref given: the default branch

    cut = build_identity_index(OWNER, NAME, "2025-11-10T00:00:00+00:00", until=REF, max_commits=150)
    assert cut.commits_scanned == 150 and cut.truncated


def test_identity_index_can_be_built_from_another_branch_or_commit(fake_gh):
    """`sha` names the commit whose history git reads: the commits API lists the
    default branch unless it is told."""
    fake_gh.commits = [{"login": "alice", "email": "alice@x", "date": "2026-10-01T00:00:00Z"}]
    fake_gh.commits_by_ref["c0ffee"] = [{"login": "bob", "email": "bob@x", "date": "2026-10-02T00:00:00Z"}]
    since = "2025-11-10T00:00:00+00:00"
    idx = build_identity_index(OWNER, NAME, since, until=REF, ref="c0ffee")
    assert idx.commits_scanned == 1 and idx.resolve("Bob <bob@x>") == "bob"
    assert parse_qs(urlparse(fake_gh.calls("GET", "commits")[0][0]).query)["sha"] == ["c0ffee"]

    with pytest.raises(GhError) as info:                     # a ref GitHub does not know
        build_identity_index(OWNER, NAME, since, until=REF, ref="deadbeef")
    assert info.value.status == 404


def test_existing_comment_is_recognised_by_marker_and_our_author(fake_gh):
    fake_gh.comments = [
        {"id": 1, "user": "someone", "body": MARKER + "\nnot ours"},
        *[{"id": 1000 + i, "user": "u", "body": f"filler {i}"} for i in range(120)],
        {"id": 2, "user": "github-actions[bot]", "body": MARKER + "\nold"},
        {"id": 3, "user": "github-actions[bot]", "body": "plain bot comment"},
        {"id": 4, "user": "Alice", "body": MARKER + "\nmine"},
    ]
    find = lambda me: action._existing_comment_id(OWNER, NAME, NUMBER, me)  # noqa: E731
    assert find("alice") == 4                    # a user token: that account's comment
    assert find("u") is None                     # that account has none: never somebody else's
    assert find("someone") == 1                  # whoever holds the token owns the comment
    assert find("github-actions") == 2           # an app token reading `/user`: "<app>[bot]"
    assert find(None) == 2                       # `/user` refused: any bot account


# ---------- the action, end to end ----------

def _commit(repo, author, email, path):
    path = repo / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    who = dict(GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL=email,
               GIT_AUTHOR_DATE=COMMIT_DAY, GIT_COMMITTER_DATE=COMMIT_DAY)
    subprocess.run([*git, "commit", "-q", "-m", "c"],
                   env={**os.environ, **who}, check=True, capture_output=True)


def _workspace(repo) -> None:
    """Two Maven modules; commits as listed in AUTHORS."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "pom.xml").write_text("<project><modules><module>java/a</module>"
                                  "<module>java/b</module></modules></project>")
    for name, email, commits, _ in AUTHORS:
        for module, n in commits.items():
            for i in range(n):
                _commit(repo, name, email, f"{module}/{name}-{i}.java")


# git author name / email / commits per module / login the commits API reports
AUTHORS = [
    ("alice", "alice@x", {"java/a": 6}, "alice"),
    ("BobHu", "bob@x", {"java/b": 6}, "BobHu"),   # ties with alice; key "bobhu" sorts after
    ("carol", "carol@x", {"java/a": 1, "java/b": 1}, "carol"),
    ("dave", "dave@x", {"java/a": 5}, "dave"),     # the PR author: never a candidate
    ("eve", "eve@x", {"java/a": 3}, ""),           # no GitHub account: dropped, not @-mentioned
    ("ci[bot]", "ci@x", {"java/a": 6}, "ci[bot]"),  # a bot
]


@pytest.fixture
def scenario(fake_gh, tmp_path, monkeypatch):
    repo = tmp_path / "workspace"
    _workspace(repo)
    fake_gh.pull = {"number": NUMBER, "user": "dave", "created_at": REF,
                    "changed_files": 150, "requested_reviewers": []}
    fake_gh.pull_files = [f"java/a/F{i}.java" for i in range(120)] + \
                         [f"java/b/G{i}.java" for i in range(30)]
    fake_gh.commits = [{"login": login, "email": email, "date": "2026-10-01T00:00:00Z"}
                       for _, email, _, login in AUTHORS]
    fake_gh.commits.append({"login": "some-integration", "email": "app@x",   # a GitHub App
                            "date": "2026-10-01T00:00:00Z", "type": "Bot"})
    fake_gh.commits_by_ref[head_commit(repo)] = fake_gh.commits   # reachable from what is checked out
    fake_gh.merged = [merged_pr(3, "2026-05-01T00:00:00Z", "frank", ["java/a/His.java"],
                                [("frank", "COMMENTED", "2026-05-02T00:00:00Z")])]
    monkeypatch.setenv("GITHUB_REPOSITORY", f"{OWNER}/{NAME}")
    monkeypatch.setenv("PR_NUMBER", str(NUMBER))
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo))
    for name in ("TOP_N", "WINDOW_DAYS", "DRY_RUN", "REQUEST_REVIEWERS"):
        monkeypatch.delenv(name, raising=False)
    return fake_gh


ORDER = ["alice", "bobhu", "carol"]
REQUESTED = "Reviews requested from @alice, @BobHu and @carol."


def _reviewers(body: str) -> list[str]:
    return [line.split("|")[1].strip() for line in body.splitlines() if line.startswith("| @")]


def test_action_requests_each_reviewer_then_posts_a_comment_that_matches(scenario, capsys):
    assert action.main() == 0
    sent = [body for _, body in scenario.calls("POST", "requested_reviewers")]
    assert sent == [{"reviewers": [login]} for login in ORDER]     # one array per request
    assert len(scenario.comments) == 1
    body = scenario.comments[0]["body"]
    assert body.startswith(MARKER) and "This PR touches `java/a` and `java/b`." in body
    assert _reviewers(body) == ["@alice", "@BobHu", "@carol"]      # logins shown as GitHub spells them
    assert REQUESTED in body
    for unwanted in ("@dave", "@eve", "@ci", "Low confidence", "dropped"):
        assert unwanted not in body                                     # diagnostics stay in the log
    assert "without a linked GitHub account dropped" in capsys.readouterr().out


def test_action_honours_top_n(scenario, monkeypatch):
    monkeypatch.setenv("TOP_N", "2")
    assert action.main() == 0
    assert [body for _, body in scenario.calls("POST", "requested_reviewers")] == \
        [{"reviewers": ["alice"]}, {"reviewers": ["bobhu"]}]


def test_action_looks_identities_up_in_the_history_it_reads(scenario):
    """A pull request against a branch other than the default one. The commits
    API lists the default branch unless it is asked for a ref, so an author who
    only committed on the PR's base branch came out as "without a linked GitHub
    account" and was dropped (seen on a real run)."""
    head = head_commit(Path(os.environ["GITHUB_WORKSPACE"]))
    scenario.commits_by_ref[head] = scenario.commits                   # the base branch
    scenario.commits = [{"login": "dave", "email": "dave@x", "date": "2026-10-01T00:00:00Z"}]
    assert action.main() == 0
    assert parse_qs(urlparse(scenario.calls("GET", "commits")[0][0]).query)["sha"] == [head]
    assert _reviewers(scenario.comments[0]["body"]) == ["@alice", "@BobHu", "@carol"]


def test_action_reports_requests_that_failed(scenario):
    scenario.reject_reviewers = {"bobhu": 422}
    assert action.main() == 0
    assert len(scenario.calls("POST", "requested_reviewers")) == 3
    body = scenario.comments[0]["body"]
    assert ("Reviews requested from @alice and @carol. Could not request a review from @BobHu "
            "(Reviews may only be requested from collaborators. (HTTP 422)); "
            "add @BobHu in the Reviewers box.") in body
    assert REQUESTED not in body                    # never claims what did not happen


def test_action_exits_nonzero_when_the_comment_cannot_be_posted(scenario, capsys):
    scenario.comment_status = 403                   # e.g. a fork PR on `pull_request`
    scenario.reject_reviewers = dict.fromkeys(ORDER, 403)
    assert action.main() == 1
    err = capsys.readouterr().err
    assert "(HTTP 403)" in err and "pull_request_target" in err
    assert scenario.comments == []


def test_action_comment_only_mode(scenario, monkeypatch):
    monkeypatch.setenv("REQUEST_REVIEWERS", "false")
    assert action.main() == 0
    assert scenario.calls("POST", "requested_reviewers") == []
    body = scenario.comments[0]["body"]
    assert _reviewers(body) == ["@alice", "@BobHu", "@carol"]
    assert "No review was requested (`request-reviewers: false`)." in body
    assert "Reviews requested from" not in body


def test_action_dry_run_writes_nothing(scenario, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "true")
    assert action.main() == 0
    assert _no_writes(scenario) == [] and scenario.comments == []
    out = capsys.readouterr().out
    assert "Would request reviews from @alice, @BobHu and @carol." in out
    assert "dry-run: nothing was posted" in out


def test_action_updates_its_earlier_comment_in_place(scenario):
    scenario.comments = [{"id": 55, "user": "github-actions[bot]", "body": MARKER + "\nold"}]
    assert action.main() == 0
    assert [(m, p) for m, p in _no_writes(scenario) if "issues" in p] == \
        [("PATCH", f"/{SLUG}/issues/comments/55")]
    assert len(scenario.comments) == 1 and scenario.comments[0]["id"] == 55
    assert REQUESTED in scenario.comments[0]["body"]


def test_action_replaces_a_comment_left_by_the_chinese_version(scenario):
    """Comments posted before 2026-11-26 start with the Chinese title. A re-run
    must not leave the old one there and add a second one next to it."""
    old = action.OLD_MARKERS[0]
    scenario.comments = [{"id": 56, "user": "github-actions[bot]", "body": old + "\n旧内容"}]
    assert action.main() == 0
    assert [(m, p) for m, p in _no_writes(scenario) if "issues" in p] == \
        [("PATCH", f"/{SLUG}/issues/comments/56")]
    assert len(scenario.comments) == 1 and scenario.comments[0]["id"] == 56
    assert scenario.comments[0]["body"].startswith(action.MARKER)


def test_action_does_not_request_who_is_already_on_the_list(scenario):
    scenario.pull["requested_reviewers"] = ["Alice"]
    assert action.main() == 0
    assert [body for _, body in scenario.calls("POST", "requested_reviewers")] == \
        [{"reviewers": ["bobhu"]}, {"reviewers": ["carol"]}]
    assert "Already requested: @alice." in scenario.comments[0]["body"]


def test_action_weighs_pending_reviews(scenario):
    scenario.open_prs = [{"requested_reviewers": [{"login": "alice"}]}] * 2
    assert action.main() == 0
    body = scenario.comments[0]["body"]
    assert _reviewers(body) == ["@BobHu", "@alice", "@carol"]   # alice is busy: two reviews pending
    assert "2 reviews pending" in body


@pytest.mark.parametrize("user, kind", [("dependabot[bot]", None),      # says so in the name
                                        ("some-integration", "Bot")])   # only GitHub knows
def test_action_ignores_pull_requests_opened_by_bots(scenario, user, kind):
    scenario.pull.update(user=user, user_type=kind)
    assert action.main() == 0
    assert _no_writes(scenario) == [] and scenario.comments == []


@pytest.mark.parametrize("name, value", [("PR_NUMBER", "abc"), ("TOP_N", "three"),
                                         ("REQUEST_REVIEWERS", "maybe"), ("WINDOW_DAYS", "0")])
def test_action_rejects_malformed_input(monkeypatch, capsys, name, value):
    monkeypatch.setenv("GITHUB_REPOSITORY", f"{OWNER}/{NAME}")
    monkeypatch.setenv("PR_NUMBER", str(NUMBER))
    monkeypatch.setenv(name, value)
    assert action.main() == 2
    assert name in capsys.readouterr().err
