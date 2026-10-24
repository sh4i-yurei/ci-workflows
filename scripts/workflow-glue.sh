#!/usr/bin/env bash
set -euo pipefail

PYTHON=${PYTHON:-python}

resolved_base() {
  if [[ -z ${BASE_SHA:-} || ${BASE_SHA} =~ ^0+$ ]]; then
    git hash-object -t tree /dev/null
    return
  fi
  if ! git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
    echo "base SHA is not an available commit: $BASE_SHA" >&2
    return 2
  fi
  local common
  if ! common=$(git merge-base "$BASE_SHA" HEAD); then
    echo "base SHA has no common ancestor with HEAD: $BASE_SHA" >&2
    return 2
  fi
  printf '%s\n' "$common"
}

diff_files() {
  local format=$1
  shift
  local options=()
  if [[ $format == names ]]; then options+=(--name-only); else options+=(--unified=0 -p); fi
  local base
  base=$(resolved_base)
  git diff "${options[@]}" "$base" HEAD -- "$@"
}

base_workflow() {
  local base
  base=$(resolved_base)
  if git cat-file -e "$base:$1" 2>/dev/null; then
    git show "$base:$1"
  else
    return 1
  fi
}

sources() {
  case $1 in
    python) "$PYTHON" -m ci_workflows filter-sources --config pyproject.toml ;;
    javascript) "$PYTHON" -m ci_workflows filter-sources --roots-json "$MUTATION_SOURCES" --extension .js --extension .mjs --extension .cjs ;;
    *) echo "unknown language: $1" >&2; return 2 ;;
  esac
}

test_python() {
  local paths_text path
  local -a paths=() coverage_args=()
  paths_text=$("$PYTHON" -m ci_workflows source-paths --config pyproject.toml) || return $?
  mapfile -t paths <<< "$paths_text"
  for path in "${paths[@]}"; do
    coverage_args+=("--cov=$path")
  done
  "$PYTHON" -m pytest "${coverage_args[@]}" --cov-branch --cov-report=xml
}

floor_gate() {
  local kind=$1 report=$2
  local base
  base=$(resolved_base)
  local -a args=(floor --kind "$kind" --report "$report" --head-floor "$COVERAGE_FLOOR")
  if [[ -n ${CALLER_JOB:-} ]]; then
    args+=(--head-workflow "$CALLER_WORKFLOW" --caller-job "$CALLER_JOB")
  fi
  if git cat-file -e "$base:$CALLER_WORKFLOW" 2>/dev/null; then
    git show "$base:$CALLER_WORKFLOW" | "$PYTHON" -m ci_workflows "${args[@]}" --base-workflow /dev/stdin
  else
    "$PYTHON" -m ci_workflows "${args[@]}"
  fi
}

changed_gate() {
  local kind=$1 report=$2
  local -a args=(changed-lines --kind "$kind" --report "$report" --diff-file /dev/stdin)
  case $kind in
    python) args+=(--source-config pyproject.toml) ;;
    javascript) args+=(--source-roots-json "$MUTATION_SOURCES") ;;
    shell|powershell) args+=(--source-root "$SOURCE_PATH") ;;
    *) echo "unknown language: $kind" >&2; return 2 ;;
  esac
  diff_files patch | "$PYTHON" -m ci_workflows "${args[@]}"
}

mutate_python() {
  local -a changed=() selected=() functions=() selectors=() scope=()
  local source selected_text function
  local changed_text
  changed_text=$(diff_files names '*.py' | sources python)
  if [[ -n $changed_text ]]; then mapfile -t changed <<< "$changed_text"; fi
  for source in "${changed[@]}"; do
    [[ -f $source ]] || continue
    if ! selected_text=$(diff_files patch "$source" | "$PYTHON" -m ci_workflows select-functions --source "$source" --source-name "$source" --diff-file /dev/stdin --mutmut | jq -r '.[]'); then
      echo "function selection failed for $source" >&2
      return 1
    fi
    selected=()
    if [[ -n $selected_text ]]; then mapfile -t selected <<< "$selected_text"; fi
    functions+=("${selected[@]}")
  done
  if (( ${#functions[@]} )); then
    for function in "${functions[@]}"; do
      selectors+=("${function}__mutmut_*")
      scope+=(--selected-function "$function")
    done
    mutmut run "${selectors[@]}"
    "$PYTHON" -m ci_workflows survivors --format mutmut --results mutants --survivors-file .mutation-survivors.yml "${scope[@]}"
  fi
}

mutate_javascript() {
  local -a changed=() scope=()
  local file mutate_globs
  local changed_text
  changed_text=$(diff_files names '*.js' '*.mjs' '*.cjs' | sources javascript)
  if [[ -n $changed_text ]]; then mapfile -t changed <<< "$changed_text"; fi
  (( ${#changed[@]} )) || return 0
  for file in "${changed[@]}"; do
    if [[ $file == *,* ]]; then
      echo "Stryker source path contains a comma: $file" >&2
      return 2
    fi
    scope+=(--selected-file "$file")
  done
  mutate_globs=$(IFS=,; echo "${changed[*]}")
  npx --no-install stryker run --mutate "$mutate_globs" --reporters json,clear-text
  "$PYTHON" -m ci_workflows survivors --format stryker --results reports/mutation/mutation.json --survivors-file .mutation-survivors.yml "${scope[@]}"
}

command=${1:?command required}
shift
case $command in
  diff) diff_files "$@" ;;
  sources) sources "$@" ;;
  test-python) test_python ;;
  base-workflow) base_workflow "$@" ;;
  floor) floor_gate "$@" ;;
  changed) changed_gate "$@" ;;
  mutate-python) mutate_python ;;
  mutate-javascript) mutate_javascript ;;
  *) echo "unknown command: $command" >&2; exit 2 ;;
esac
