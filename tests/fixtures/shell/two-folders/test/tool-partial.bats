#!/usr/bin/env bats

@test "second tool without its name option" {
  source "$BATS_TEST_DIRNAME/../first/first.sh"
  run first_label
  [ "$output" = "first" ]
  run "$BATS_TEST_DIRNAME/../second/second-tool"
  [ "$output" = "tool" ]
}
