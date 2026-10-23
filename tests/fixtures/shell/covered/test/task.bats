#!/usr/bin/env bats

setup() {
  source "$BATS_TEST_DIRNAME/../src/task.sh"
}

@test "both branches and witness" {
  run classify 2
  [ "$status" -eq 0 ]
  [ "$output" = "positive" ]
  run classify -2
  [ "$status" -eq 0 ]
  [ "$output" = "not positive" ]
  run witness
  [ "$status" -eq 0 ]
  [ "$output" = "covered" ]
}
