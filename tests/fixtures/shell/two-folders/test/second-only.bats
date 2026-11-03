#!/usr/bin/env bats

@test "second folder only" {
  source "$BATS_TEST_DIRNAME/../second/second.sh"
  run second_label
  [ "$output" = "second" ]
  run second_extra
  [ "$output" = "extra" ]
}
