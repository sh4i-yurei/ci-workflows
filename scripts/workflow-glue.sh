#!/usr/bin/env bash
set -euo pipefail

PYTHON=${PYTHON:-python}

resolved_base() {
  if [[ -n ${PUSHED_REF:-} && -n ${DEFAULT_BRANCH:-} && $PUSHED_REF != "$DEFAULT_BRANCH" ]]; then
    local default_ref="refs/remotes/origin/$DEFAULT_BRANCH" common
    if ! git show-ref --verify --quiet "$default_ref"; then
      echo "default branch is not an available ref: origin/$DEFAULT_BRANCH" >&2
      return 2
    fi
    if ! common=$(git merge-base HEAD "$default_ref"); then
      echo "default branch has no common ancestor with HEAD: origin/$DEFAULT_BRANCH" >&2
      return 2
    fi
    echo "base $common is the merge base of HEAD and origin/$DEFAULT_BRANCH" >&2
    printf '%s\n' "$common"
    return
  fi
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

kcov_paths() {
  local paths_text path
  local -a paths=()
  paths_text=$("$PYTHON" -m ci_workflows source-paths --roots-json "$SOURCE_PATHS") || return $?
  mapfile -t paths <<< "$paths_text"
  for path in "${paths[@]}"; do
    if [[ $path == *,* ]]; then
      echo "kcov source path contains a comma: $path" >&2
      return 2
    fi
  done
  (IFS=,; printf '%s\n' "${paths[*]}")
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
    shell|powershell) args+=(--source-roots-json "$SOURCE_PATHS") ;;
    *) echo "unknown language: $kind" >&2; return 2 ;;
  esac
  diff_files patch | "$PYTHON" -m ci_workflows "${args[@]}"
}

select_python_functions() {
  changed_sources=()
  functions=()
  local -a changed=() selected=()
  local source selected_text
  local changed_text
  changed_text=$(diff_files names '*.py' | sources python) || return $?
  if [[ -n $changed_text ]]; then mapfile -t changed <<< "$changed_text"; fi
  for source in "${changed[@]}"; do
    [[ -f $source ]] || continue
    if ! selected_text=$(diff_files patch "$source" | "$PYTHON" -m ci_workflows select-functions --source "$source" --source-name "$source" --diff-file /dev/stdin --mutmut | jq -r '.[]'); then
      echo "function selection failed for $source" >&2
      return 1
    fi
    selected=()
    if [[ -n $selected_text ]]; then mapfile -t selected <<< "$selected_text"; fi
    if (( ${#selected[@]} )); then changed_sources+=("$source"); fi
    functions+=("${selected[@]}")
  done
}

mutate_python() {
  local -a changed_sources=() functions=() selectors=() scope=()
  local function
  select_python_functions || return $?
  if (( ${#functions[@]} )); then
    for function in "${functions[@]}"; do
      selectors+=("${function}__mutmut_*")
      scope+=(--selected-function "$function")
    done
    mutmut run "${selectors[@]}"
    "$PYTHON" -m ci_workflows survivors --format mutmut --results mutants --survivors-file .mutation-survivors.yml "${scope[@]}"
  fi
}

plan_python() {
  if [[ -z ${GITHUB_OUTPUT:-} ]]; then
    echo 'GITHUB_OUTPUT is not set' >&2
    return 2
  fi
  local reason='' shard_numbers mutants count
  local -a changed_sources=() functions=() args=(mutation-plan)
  if [[ -n ${PUSHED_REF:-} && -n ${DEFAULT_BRANCH:-} && $PUSHED_REF == "$DEFAULT_BRANCH" ]]; then
    reason="push to the default branch $DEFAULT_BRANCH"
  else
    select_python_functions || return $?
    if (( ${#functions[@]} == 0 )); then reason='no Python function changed'; fi
  fi
  if [[ -n $reason ]]; then
    printf 'mutation skipped: %s\n' "$reason"
    printf 'shards=[]\nskipped=%s\n' "$reason" >> "$GITHUB_OUTPUT"
    return 0
  fi
  local source function
  for source in "${changed_sources[@]}"; do args+=(--source "$source"); done
  for function in "${functions[@]}"; do args+=(--selected-function "$function"); done
  "$PYTHON" -m ci_workflows "${args[@]}" > mutation-plan.json || return $?
  shard_numbers=$(jq -c '[range(.shards | length)]' mutation-plan.json) || return $?
  mutants=$(jq -r '.mutants' mutation-plan.json) || return $?
  count=$(jq -r '.shards | length' mutation-plan.json) || return $?
  printf 'mutation plan: %s mutants in %s shards\n' "$mutants" "$count"
  printf 'shards=%s\nskipped=\n' "$shard_numbers" >> "$GITHUB_OUTPUT"
}

shard_python() {
  if (( $# == 0 )); then echo 'shard-python needs a shard number' >&2; return 2; fi
  local number=$1
  if [[ ! $number =~ ^[0-9]+$ ]]; then
    printf 'shard number must be a whole number: %s\n' "$number" >&2
    return 2
  fi
  number=$((10#$number))
  if [[ -z ${JOB_STARTED:-} ]]; then echo 'JOB_STARTED is not set' >&2; return 2; fi
  if [[ -z ${GITHUB_STEP_SUMMARY:-} ]]; then echo 'GITHUB_STEP_SUMMARY is not set' >&2; return 2; fi
  if [[ ! -f mutation-plan.json ]]; then echo 'mutation-plan.json not found' >&2; return 2; fi
  local count
  count=$(jq -r '.shards | length' mutation-plan.json) || return 2
  if (( number >= count )); then
    printf 'no shard %s in mutation-plan.json\n' "$number" >&2
    return 2
  fi
  local -a sources=() names=() args=(mutation-config --config pyproject.toml)
  mapfile -t sources < <(jq -r '.sources[]' mutation-plan.json)
  mapfile -t names < <(jq -r --argjson n "$number" '.shards[$n][]' mutation-plan.json)
  local source
  for source in "${sources[@]}"; do args+=(--source "$source"); done
  "$PYTHON" -m ci_workflows "${args[@]}" || return $?

  local timing_dir status setup='unknown' speed='mutations/second unknown' line relative file
  timing_dir=$(mktemp -d)
  if mutmut run "${names[@]}" | {
    while IFS= read -r line || [[ -n $line ]]; do
      printf '%s\n' "$line"
      if [[ $line == *'Running mutation testing'* && ! -f $timing_dir/started ]]; then
        date +%s > "$timing_dir/started"
      fi
      if [[ $line == *'mutations/second'* ]]; then
        printf '%s\n' "${line##*$'\r'}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' > "$timing_dir/speed"
      fi
    done
  }; then
    status=0
  else
    status=${PIPESTATUS[0]}
  fi
  if (( status != 0 )); then rm -rf "$timing_dir"; return "$status"; fi
  if [[ -f $timing_dir/started ]]; then setup=$(( $(<"$timing_dir/started") - JOB_STARTED )); fi
  if [[ -f $timing_dir/speed ]]; then speed=$(<"$timing_dir/speed"); fi
  rm -rf "$timing_dir"

  if [[ -d mutants ]]; then
    while IFS= read -r -d '' file; do
      relative=${file#mutants/}
      [[ /$relative == */.*/* ]] && continue
      mkdir -p "mutation-results/$number/$(dirname "$relative")"
      cp "$file" "mutation-results/$number/$relative"
    done < <(find mutants -type f -name '*.meta' -print0)
  fi
  if [[ $setup != unknown ]]; then setup="$setup s"; fi
  printf 'shard %s of %s: %s mutants, setup %s, %s\n' "$((number + 1))" "$count" "${#names[@]}" "$setup" "$speed" >> "$GITHUB_STEP_SUMMARY"
}

checks_python() {
  local required
  for required in TESTS_RESULT PLAN_RESULT MUTATE_RESULT; do
    if [[ -z ${!required:-} ]]; then
      printf '%s is not set\n' "$required" >&2
      return 2
    fi
  done
  local -a args=(mutation-checks --tests "$TESTS_RESULT" --plan "$PLAN_RESULT" --mutate "$MUTATE_RESULT")
  if [[ ${REPOSITORY_PRIVATE:-} == true ]]; then
    args+=(--private)
  elif [[ -n ${PLAN_SKIPPED:-} ]]; then
    args+=(--skipped "$PLAN_SKIPPED")
  else
    args+=(--plan-file mutation-plan.json --shards mutation-shards --survivors-file .mutation-survivors.yml --source-config pyproject.toml)
  fi
  "$PYTHON" -m ci_workflows "${args[@]}"
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
  kcov-paths) kcov_paths ;;
  base-workflow) base_workflow "$@" ;;
  floor) floor_gate "$@" ;;
  changed) changed_gate "$@" ;;
  mutate-python) mutate_python ;;
  plan-python) plan_python ;;
  shard-python) shard_python "$@" ;;
  checks-python) checks_python ;;
  mutate-javascript) mutate_javascript ;;
  *) echo "unknown command: $command" >&2; exit 2 ;;
esac
