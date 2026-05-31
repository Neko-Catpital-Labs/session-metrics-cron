#!/usr/bin/env bash
set -euo pipefail

invoker_home="$HOME/.invoker"
host="$(hostname 2>/dev/null || echo unknown)"
exists=0
size_kib=0
size_human=0

if [[ -e "$invoker_home" ]]; then
  exists=1
  size_kib="$(du -sk "$invoker_home" 2>/dev/null | awk '{print $1}' || true)"
  size_human="$(du -sh "$invoker_home" 2>/dev/null | awk '{print $1}' || true)"
fi

[[ "$size_kib" =~ ^[0-9]+$ ]] || size_kib=0
[[ -n "$size_human" ]] || size_human=0

df -Pk "$HOME" 2>/dev/null | awk -v host="$host" -v path="$invoker_home" -v exists="$exists" -v size_kib="$size_kib" -v size_human="$size_human" '
  NR == 2 {
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", host, path, exists, size_kib, size_human, $1, $2, $3, $4, $5
    found = 1
  }
  END {
    if (!found) {
      printf "%s\t%s\t%s\t%s\t%s\t\t\t\t\t\n", host, path, exists, size_kib, size_human
    }
  }
'
