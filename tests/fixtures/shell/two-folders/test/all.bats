#!/usr/bin/env bats

setup() {
  source "$BATS_TEST_DIRNAME/../first/first.sh"
  source "$BATS_TEST_DIRNAME/../second/second.sh"
}

@test "both folders and all lines" {
  run first_label
  [ "$output" = "first" ]
  run second_label
  [ "$output" = "second" ]
  run second_extra
  [ "$output" = "extra" ]
  run "$BATS_TEST_DIRNAME/../second/second-tool"
  [ "$output" = "tool" ]
  run "$BATS_TEST_DIRNAME/../second/second-tool" --name
  [ "$output" = "second-tool" ]
}
