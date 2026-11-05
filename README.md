# pr-reviewer-recommender

Recommend and request PR reviewers based on **which modules a PR touches**, who has
expertise in those modules, and who has reviewed them before — no LLM, no config file
per repo, fully explainable scores.

Backtested on Apache IoTDB (36 PRs): **Top-3 hit rate 75%, MRR 0.53**; on Apache TsFile
(15 PRs): Top-3 53%. All evaluations are leak-free (only data available *before* each PR
existed).

## How it scores

For each touched module `m` and candidate `u`:

```
score(u) = [ Σ_m [ λc·C(u,m) + λr·R(u,m) ] + coupling(u, M) + seams(u, M) ] · workload(u)
```

- `C(u,m)` — decayed commit count of `u` in module `m` (halflife 180d)
- `R(u,m)` — decayed review count of `u` on PRs touching `m`
- `coupling` — bonus when the PR spans multiple modules and `u` has commits in **all** of them
- `seams` (v2) — bonus for history on both sides of a Maven dependency boundary (`m↔n`)
- `workload` (v2) — ×0.75 per pending review request; ≥6 pending excludes the candidate

Also ships `scripts/gen_codeowners.py`: emit a suggested CODEOWNERS from module history.

Modules are auto-discovered: Maven `<module>` entries (recursive) → directories with
build markers (`go.mod`, `CMakeLists.txt`, `pyproject.toml`, …) → top-level dirs.
Files map to the longest matching module prefix. Bots, the PR author, and dormant
contributors are filtered out.

## Usage as a GitHub Action

```yaml
# .github/workflows/reviewer-recommend.yml in the target repository
name: reviewer-recommend
on:
  pull_request:
    types: [opened]
permissions:
  contents: read
  pull-requests: write
jobs:
  recommend:
    runs-on: ubuntu-latest
    steps:
      - uses: YOUR_FORK/pr-reviewer-recommender@main
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          top-n: '3'
```

Inputs: `github-token` (required), `top-n` (3), `window-days` (365), `dry-run` (false).

The action checks out full history, posts a comment with per-reviewer rationale, and
requests the top-N reviewers. Requires `gh` CLI (preinstalled on GitHub runners) and
Python 3.10+. Zero third-party Python dependencies.

## Local usage

```bash
# recommend for a file list against a local clone (commit signals only)
python3 -m reviewer_recommender.cli --repo /path/to/clone \
    --files "java/tsfile/src/main/java/org/apache/tsfile/X.java,java/common/Y.java"

# leak-free backtest against merged PRs (needs gh auth)
python3 scripts/backtest.py --owner apache --name iotdb \
    --repo /path/to/iotdb --max-prs 40 --verbose
```

## Layout

- `reviewer_recommender/modules.py` — module discovery & file→module mapping
- `reviewer_recommender/history.py` — git-log authorship signals (time-decayed)
- `reviewer_recommender/reviewlog.py` — GitHub review history + email→login identity resolution
- `reviewer_recommender/score.py` — scoring, filtering, coupling bonus
- `reviewer_recommender/action.py` — Action entrypoint (comment + review requests)
- `scripts/backtest.py` — leak-free backtest harness

See `DESIGN.md` for the full design rationale and backtest analysis.
