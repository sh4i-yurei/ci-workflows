#!/usr/bin/env bats

@test "first folder and one second function" {
  source "$BATS_TEST_DIRNAME/../first/first.sh"
  source "$BATS_TEST_DIRNAME/../second/second.sh"
  run first_label
  [ "$output" = "first" ]
  run second_label
  [ "$output" = "second" ]
}
