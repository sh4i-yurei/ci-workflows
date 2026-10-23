#!/usr/bin/env bash

classify() {
  if (( $1 > 0 )); then
    printf 'positive\n'
  else
    printf 'not positive\n'
  fi
}

witness() {
  printf 'covered\n'
}
