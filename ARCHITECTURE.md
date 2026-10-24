# Architecture

How the pieces of this repository fit together, and which piece owns each shared thing. When a
change doesn't fit this, change this file first, on purpose, in the same commit.

## The shape

Callers use one reusable workflow per kind of code. Each workflow runs the caller's own tests and
coverage tool, then hands the results to one Python command-line tool, `ci_workflows`, which makes
every pass or fail decision. Shell logic the workflows share lives in one script. Nothing in a
workflow file decides a verdict on its own.

The PowerShell workflow runs Pester 5.7.1 under Windows PowerShell 5.1 on a Windows runner,
matching the caller's runtime. It uploads its JaCoCo report for a dependent Ubuntu job to run
the existing floor and changed-line gates. Those shared shell and Python gates are tested on Linux.

```
caller repository
  └─ .github/workflows/*.yml  (workflow_call, pinned by commit SHA)
       ├─ runs the caller's tests and coverage tool
       ├─ scripts/workflow-glue.sh   base commit, diffs, source filtering
       └─ python -m ci_workflows <command>   every verdict
```

## The pieces

| Piece | Owns | Must not |
|---|---|---|
| `.github/workflows/<kind>.yml` | One reusable workflow per kind: `python`, `javascript`, `shell`, `powershell`, `molecule`, `prometheus`, `tailscale`. Each installs pinned tools, runs the caller's tests, and calls the CLI. | Decide a verdict in shell; every pass or fail comes from the CLI |
| `.github/workflows/ci.yml` | This repository's own CI: actionlint, and the Python workflow run on this package. | Test anything but this repository |
| `scripts/workflow-glue.sh` | The shell steps the workflows share: resolving the base commit, producing diffs, filtering to the caller's declared source paths, and running Python tests with coverage over those paths through `test-python`. It is the only place this logic lives. | Judge results; it only prepares inputs |
| `ci_workflows/checks.py` | The CLI entry point and the code-coverage gates: `changed-lines`, `floor`, `select-functions`, `survivors`, `filter-sources` and `source-paths`. | Grow another kind of check; a new kind gets its own module, as `rules.py` and `tailnet.py` did |
| `ci_workflows/rules.py` | The alert-rule check: every alert has a firing case and a quiet case in the caller's promtool tests. | Run promtool's logic itself; promtool runs the tests |
| `ci_workflows/tailnet.py` | The tailnet policy check: mutates each grant and legacy ACL entry and asks Tailscale's validate endpoint to run the policy's tests on each copy. | Evaluate policy rules itself, or reach any Tailscale endpoint but validate |
| `ci_workflows/survivors.py` | The one loader for `.mutation-survivors.yml`. | Judge whether an explanation is stale; each gate judges its own keys |
| `ci_workflows/errors.py` | `ConfigurationError`, the error every command reports as exit 2. | Hold anything but the shared error types |
| `pyproject.toml`, `requirements/` | Pinned direct dependencies, and lock files for transitive ones. | Allow a version range |
| `tests/` | One test module per area, with real sample inputs under `tests/fixtures/`. | Use an input invented for the test where a real one exists |

## Rules every piece follows

- **Exit codes mean one thing everywhere.** 0 is pass. 1 is a check that ran and failed. 2 is a
  configuration or infrastructure error. An infrastructure error, such as a timeout, a refused
  connection or an unexpected response, is never counted as a pass or as a killed mutant.
- **Call the authoritative tool; don't copy it.** diff-cover decides which changed lines are
  covered. promtool runs alert tests. Tailscale's validate endpoint decides whether a policy's
  tests pass. This package prepares inputs for those tools and reads their results. It never
  reimplements what they decide.
- **Paths are normalised once, at the source.** Every coverage report is rewritten to
  repository-relative paths before any gate reads it.
- **Scope is the caller's declared source paths.** A changed file outside them is skipped and
  named as skipped. It is never a configuration error.
- **One owner per shared file.** `.mutation-survivors.yml` is read only through
  `survivors.py`. Each gate judges only its own kind of key: mutmut names, Stryker keys or
  `tailscale:` keys. A key of another kind is not stale to it.
- **Mutation evidence must be complete and current.** A mutation result counts only if the run
  finished every mutant and reflects the current code and tests. CI always runs mutation fresh.
  Locally, mutmut's own cache invalidation reruns every mutant a source or test change affects.
  Freshness is never judged by comparing file timestamps. A survivor may be explained only when
  no test could kill it. The repository's local mutation command in `AGENTS.md` calls the same
  changed-function selector and survivors gate as CI, using the pull request's merge base. A
  first pull request that changes every function therefore selects the whole package locally.
- **Temporary files never live inside the repository,** because mutmut copies the repository, and
  parallel mutmut workers never share a temporary directory.
- **Everything is pinned.** Actions by full commit SHA, tools and Python packages by exact version.
  Callers pin this repository by commit SHA.
- **Tests use real inputs.** Fixtures are real tool output, such as `git diff --unified=0`, real
  coverage reports and real policy files, never shapes invented for the test.

## Adding a kind of code

A new language joins by adding a workflow file, the CLI's report handling for its coverage format,
and its mutation results to the survivors gate under its own key kind. Then add its row to the
pieces table above.
