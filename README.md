# Shared CI workflows

These public reusable GitHub Actions workflows run the checks in the testing standard. Caller repositories own their test suites and coverage floor.

## Workflows

| Workflow | Checks |
| --- | --- |
| `python.yml` | pytest branch coverage, changed lines, floor, and mutmut survivors |
| `javascript.yml` | Node test runner branch coverage, changed lines, floor, and Stryker survivors |
| `shell.yml` | Bats under kcov, changed lines, and floor |
| `powershell.yml` | Pester 5 coverage, changed lines, and floor |
| `molecule.yml` | Caller Molecule scenarios under rootless Podman |
| `prometheus.yml` | promtool tests and every alert's firing and quiet cases |
| `tailscale.yml` | Tailscale validate tests of the policy and three mutations per grant or ACL |

## Inputs

Each language workflow accepts the caller's required numeric `coverage_floor` from 0 to 100. The Python workflow also requires `install_command` (how to install the caller's code and tests) and `caller_workflow` (the path to the caller's workflow for the floor comparison). It reads `[tool.mutmut].source_paths` from the caller's `pyproject.toml` to measure coverage over every declared path and to scope its changed-line and mutation steps. A pull request must keep its floor at or above the base branch's value. The Python, JavaScript, shell, and PowerShell workflows require coverage reports produced by their test steps. The Molecule workflow runs the caller's `molecule/` scenarios. The Prometheus and Tailscale workflows take paths to caller rule tests and policy files.

Every language workflow also requires `caller_job`, the job ID in the caller workflow that invokes it. The floor ratchet checks that job in the working-tree caller workflow and reads its base floor, so different jobs may use different floors. When a pull request adds the job for the first time, the missing base job has no prior floor; the new job's requested floor and reported coverage are still checked.

The JavaScript workflow requires `install_command`, `caller_workflow`, and `mutation_sources`, a JSON list of source directories such as `["src"]`. The caller installs StrykerJS and configures its command runner to execute `node --test`. The workflow writes Node's LCov report and Stryker's default JSON report at `reports/mutation/mutation.json`.

The shell workflow requires `caller_workflow`, `source_path` (the shell source directory for kcov), and `tests_path` (a Bats test file or directory). kcov writes Cobertura XML to `.coverage-shell/cov.xml`.

The PowerShell workflow requires `caller_workflow`, `source_path` (the scripts to measure), and `tests_path` (a Pester test file or directory). Pester 5 writes JaCoCo XML to `coverage-jacoco.xml`.

The Molecule workflow requires `scenarios`, a JSON list of caller scenario names such as `["default"]`. It runs each scenario with `molecule test` on an Ubuntu runner using rootless Podman; the caller scenario defines its container images and verify assertions. Each caller scenario must read `PODMAN_EXECUTABLE` for the Podman command, because the workflow sets that variable to the runner's executable.

The Prometheus workflow requires `test_files`, a JSON list of promtool rule-test paths such as `["rules/test.yml"]`. One job passes all listed files to `promtool test rules` and checks their combined evidence for every referenced alert's firing and quiet cases. It resolves `rule_files` globs relative to each test file. Recording rules do not need these cases.

The Tailscale workflow requires `policy_file` and the caller's `TS_OAUTH_ID`, `TS_AUDIENCE` and `TS_TAILNET` secrets. GitHub issues an identity token, which the check exchanges for a short-lived Tailscale access token. The federated identity needs `policy_file:read` and its required read-only device scopes. The check sends the original HuJSON policy and each grant or legacy ACL mutation to Tailscale's validate endpoint. It never applies a policy. A surviving mutation needs a reason in the caller's `.mutation-survivors.yml`.

The caller must grant `id-token: write` in its workflow permissions so GitHub can issue the identity token.

## Pinning a caller

Call a workflow with a full commit SHA, then update the pin deliberately after reviewing changes:

```yaml
jobs:
  tests:
    uses: sh4i-yurei/ci-workflows/.github/workflows/python.yml@0123456789abcdef0123456789abcdef01234567
    with:
      coverage_floor: 80
      install_command: python -m pip install -e '.[test]'
      caller_workflow: .github/workflows/ci.yml
      caller_job: tests
```

The example SHA is a placeholder. Replace it with a real commit SHA from this repository. The Python workflow installs the verified mutmut 3.8.0 version through the shared checks' `mutation` extra. A caller's later install command must preserve that version; installing a different mutmut version makes function selection fail. The shared check dependencies are pinned in `pyproject.toml` and its transitive dependency constraints are pinned in `requirements/ci-lock.txt`.

## Mutation survivors

A caller can justify equivalent or unkillable mutants in `.mutation-survivors.yml`. Entries form a list. Each entry names the exact mutant and gives a non-empty reason:

```yaml
- mutant: pkg.f.x__mutmut_3
  reason: Equivalent transformation
```

For StrykerJS, use the stable file, mutator, source location and replacement fingerprint key reported by the gate, such as `src/app.js#ArithmeticOperator@2:0-2:1~cb23f6635a581786`.

For a tailnet policy survivor, copy the `tailscale:` key printed by the policy check. The key uses the entry's content hash and mutation kind, so moving an entry keeps its reason valid and editing it requires a new reason.

Every survivor in the selected scope must be listed. A mutmut entry is stale when its mutant is absent from the generated inventory or ran and did not survive. A Stryker entry outside the selected files is retained while its source file exists; entries for deleted files or mutants that ran and did not survive are stale. Blank reasons fail regardless of scope.

## Mutation tool gaps

Shell and PowerShell have no mutation tool. Their workflows enforce tests and changed-line coverage.
Mutmut does not generate mutants for some Python functions, including functions with route decorators. The Python selector names each such function as a mutation tool gap in the CI log.
