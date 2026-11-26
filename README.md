# pr-reviewer-recommender

Recommend and request PR reviewers based on **which modules a PR touches**, who has
committed to those modules and who has reviewed them before. No LLM, no
per-repository configuration, and every score can be read off the repository's
history.

Leak-free backtest (only data available *before* each PR was used), 2026-11-25:

| Repository | Merged PRs scored | Top-1 | Top-3 | MRR |
|---|---|---|---|---|
| apache/iotdb | 99 | 23% (16–32%) | 77% (68–84%) | 0.487 |
| apache/tsfile | 51 | 57% (43–69%) | 82% (70–90%) | 0.670 |

A PR counts as a hit when someone the action recommended did review it; the
parentheses hold 95% Wilson intervals. Protocol and caveats: `DESIGN.md` §5 and §7.

## How it scores

For each candidate `u` and the set `M` of modules the PR touches:

```
score(u) = [ Σ_m λc·C(u,m) + λr·R(u,m) + coupling(u, M) + seams(u, M) ] · workload(u)
```

- `C(u,m)` — commits of `u` in module `m`, time-decayed (180-day half-life)
- `R(u,m)` — reviews by `u` of merged PRs that touched `m`, time-decayed; λr = 2.5,
  since reviewing is the closer signal to the task at hand
- `coupling` — the PR spans several modules and `u` has committed to all of them
- `seams` — `u` has history on both sides of a Maven dependency boundary (`m↔n`)
- `workload` — ×0.75 per review request waiting for `u`; six or more: not a candidate

A PR that touches nothing but files outside every module (the root README, say)
is scored on that remainder with weight 0.2. The PR author, bots and accounts
with six or more reviews waiting are left out of the ranking.

Also ships `scripts/gen_codeowners.py`, which writes a suggested CODEOWNERS from
module history.

Modules are discovered from the repository: Maven `<module>` entries (recursive) →
directories with build markers (`go.mod`, `CMakeLists.txt`, `pyproject.toml`, …) →
top-level directories. A file belongs to the module with the longest matching prefix.

## Usage as a GitHub Action

The workflow file has to be on the **default branch**: `pull_request_target` runs in
the context of the default branch and is not triggered by a workflow that lives
elsewhere (checked on 2026-11-25 — on a branch that is not the default one, nothing
ran). The PR that adds the workflow therefore gets no run itself; the next ones do.

### Remote action

The shortest setup, and the one that covers PRs against every branch — release
branches included:

```yaml
# .github/workflows/reviewer-recommend.yml, on the default branch
name: reviewer-recommend
on:
  pull_request_target:
    types: [opened, ready_for_review]
permissions:
  contents: read
  pull-requests: write
jobs:
  recommend:
    if: github.event.pull_request.draft == false   # a draft waits until it is ready
    runs-on: ubuntu-latest
    steps:
      - uses: OWNER/pr-reviewer-recommender@FULL_COMMIT_SHA
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

Pin the action to a full commit SHA. The job runs on `pull_request_target` with a
token that can write, so the code it runs must not be able to change underneath it.

The action checks out the **base commit** of the PR itself (full history, and no
credentials left in the workspace) and reads everything about the PR through the
API. Code from the PR is never checked out or run.

### Vendored action

Everything the workflow runs then lives in your own repository and is reviewed with
it. Copy `action.yml`, `reviewer_recommender/` and `LICENSE` to
`.github/actions/reviewer-recommender/` and replace the `uses:` step by:

```yaml
    steps:
      - uses: actions/checkout@v4          # a local action needs a checkout first
        with:
          ref: ${{ github.event.pull_request.base.sha }}
          fetch-depth: 0
          persist-credentials: false
      - uses: ./.github/actions/reviewer-recommender
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

The local action is read from the PR's base commit, so the directory has to exist
on every branch whose PRs you want covered. A release branch cut before the action
was added does not have it — use the remote form for those. This matters more than
it sounds: 4,778 of the 18,075 pull requests of apache/iotdb target a branch other
than the default one. Pin `actions/checkout` to a commit SHA here as well: the
step runs in a job that holds a write token.

### Inputs

| Input | Default | Meaning |
|---|---|---|
| `github-token` | — (required) | Token with `pull-requests: write`. `secrets.GITHUB_TOKEN` is enough on `pull_request_target`. |
| `top-n` | `3` | How many reviewers to recommend and request. At least 2. |
| `window-days` | `365` | How far back the history goes. |
| `request-reviewers` | `true` | Also request the recommended reviewers. `false` posts the comment only. |
| `dry-run` | `false` | Print the comment and the requests to the log. Writes nothing. |

### Behaviour

- One comment per PR, edited in place on later runs. A comment left by an earlier
  version of the action is replaced as well.
- Reviewers are requested **before** the comment is written, and the comment reports
  what actually happened. GitHub refuses a request for someone who is not a
  collaborator of the repository (HTTP 422); the comment then says who could not be
  requested.
- A comment that cannot be posted fails the job (exit 1) rather than letting a run
  pass that posted nothing.
- Bots are recognised by their GitHub account type; the account name is only a
  fallback.
- `pull_request` instead of `pull_request_target` works only for PRs from branches
  of the same repository. For PRs from forks `GITHUB_TOKEN` is read-only there, and
  the action stops with a message that says so.
- To run again when the PR gets new commits, add `synchronize` to `types`. Review
  requests are only ever added, never removed.

### What the comment looks like

```markdown
## 🤖 Suggested reviewers

This PR touches `java/common` and `java/tsfile`.

| Reviewer | Score | Why |
|---|---|---|
| @alice | 15.8 | `java/tsfile` 12 commits, 4 reviews · `java/common` 6 commits · **history in every touched module** |
| @bob | 7.4 | `java/tsfile` 16 commits · `java/common` 1 commit · **history in every touched module** |

No review was requested (`request-reviewers: false`).

<details><summary>How the score is computed</summary>

Commits and reviews in the modules this PR touches, each weighted by a 180-day half-life. A PR that touches several modules adds a bonus for people with history in every one of them, and for people with history on both sides of a dependency between a touched module and its neighbours. People with several reviews waiting rank lower.
</details>
```

The line outside the fold reports on the review requests, so it varies: who
was requested (`Reviews requested from @alice and @bob.`), who was already on the
list (`Already requested: @carol.`), who could not be (`Could not request a review
from @dave (…); add @dave in the Reviewers box.`). When the top candidate has
fewer than five commits and reviews in the touched modules, the table is preceded
by a "Low confidence" note.

### What has been run on GitHub

On 2026-11-25, on a fork of apache/tsfile: the vendored form on `pull_request_target`
and on `pull_request` (a same-repository PR), the remote form on
`pull_request_target` with the previously published version, a comment created and
edited in place, the failed-review-request path, and the comment-only mode. Not run
on GitHub so far: a review request that succeeds — the fork has no collaborators;
and a PR from another repository on `pull_request` — the read-only token case. Both
are covered by tests that drive the real `gh` CLI against a fake API.

Needs a GitHub-hosted runner (for the preinstalled `gh`), Python 3.10+ and no
third-party Python package.

## Local usage

```bash
# recommend for a file list against a local clone (commit signals only)
python3 -m reviewer_recommender.cli --repo /path/to/clone \
    --files "java/tsfile/src/main/java/org/apache/tsfile/X.java,java/common/Y.java"

# leak-free backtest against merged PRs (needs gh auth; the clone must be up to date)
python3 scripts/backtest.py --owner apache --name iotdb --repo /path/to/iotdb \
    --before 2026-11-25T00:00:00Z --scan-prs 200 --max-prs 100

# tests
python3 -m pytest
```

## Layout

- `action.yml`, `reviewer_recommender/action_main.py` — the action: what a workflow calls
- `reviewer_recommender/action.py` — the action's steps: PR data, review requests, comment
- `reviewer_recommender/modules.py` — module discovery and file → module mapping
- `reviewer_recommender/deps.py` — Maven dependency graph between modules
- `reviewer_recommender/history.py` — git-log authorship signals (time-decayed)
- `reviewer_recommender/reviewlog.py` — GitHub review history, pending reviews, identity
- `reviewer_recommender/score.py` — scoring and filtering
- `reviewer_recommender/config.py`, `reviewer_recommender/cli.py` — defaults and the local CLI
- `scripts/backtest.py`, `scripts/tune.py`, `scripts/gen_codeowners.py` — evaluation and tooling
- `tests/` — unit tests, and tests that run the real `gh` against a fake GitHub
- `workflow-example.yml` — the workflow file, ready to copy

See `DESIGN.md` for the design rationale, the backtest and its limits.
