# Shared CI workflows

Reusable GitHub Actions workflows and the `ci_workflows` Python package behind them. Other
repositories call these workflows, pinned to a commit, so a change here changes every caller's
checks once they move their pin.

Read `ARCHITECTURE.md` before changing anything. It says which piece owns each shared thing, and
new code must fit it.

Checks, run from the repository root inside a virtual environment with the package installed:

- tests and coverage: `python -m pytest --cov=ci_workflows --cov-branch --cov-report=xml -q`
- coverage floor: `python -m ci_workflows floor --report coverage.xml --head-floor 100`
- mutation for this repository's pull request diff: with `origin/main` fetched, run
  `BASE_SHA=$(git merge-base origin/main HEAD) bash scripts/workflow-glue.sh mutate-python`.
  This uses the same changed-function selection and survivors gate as CI. Run it on a clean
  checkout with the package and mutation extra installed, and let every selected mutant finish.
- workflow lint: `go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.12 .github/workflows/*.yml`
- whitespace: `git diff --check`

This repository is public. Keep private infrastructure details, hostnames and links to private
notes out of every file.
