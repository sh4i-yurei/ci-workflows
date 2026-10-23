# Shared CI workflows

Reusable GitHub Actions workflows and the `ci_workflows` Python package behind them. Other
repositories call these workflows, pinned to a commit, so a change here changes every caller's
checks once they move their pin.

Read `ARCHITECTURE.md` before changing anything. It says which piece owns each shared thing, and
new code must fit it.

Checks, run from the repository root inside a virtual environment with the package installed:

- tests and coverage: `python -m pytest --cov=ci_workflows --cov-branch --cov-report=xml -q`
- coverage floor: `python -m ci_workflows floor --report coverage.xml --head-floor 100`
- mutation: `mutmut run`, then
  `python -m ci_workflows survivors --format mutmut --results mutants --survivors-file .mutation-survivors.yml`
- workflow lint: `go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.12 .github/workflows/*.yml`
- whitespace: `git diff --check`

This repository is public. Keep private infrastructure details, hostnames and links to private
notes out of every file.
