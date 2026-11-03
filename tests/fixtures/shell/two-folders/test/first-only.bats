#!/usr/bin/env bats

@test "first folder only" {
  source "$BATS_TEST_DIRNAME/../first/first.sh"
  run first_label
  [ "$output" = "first" ]
}
