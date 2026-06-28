#!/usr/bin/env bash
# Archive the raw collected fleet session logs so backfills can rebuild history
# even after hosts rotate/prune their own logs. Opt-in: does nothing unless
# SESSION_ARCHIVE_DEST is set.
#
#   SESSION_ARCHIVE_DEST=/durable/path        bash scripts/archive-fleet-sessions.sh
#   SESSION_ARCHIVE_DEST=gs://bucket/sessions  bash scripts/archive-fleet-sessions.sh
#
# Each run writes one dated tarball of the staged sessions:
#   <dest>/fleet-sessions-YYYYmmddTHHMMSSZ.tar.gz
#
# Restore / backfill from archives (content-hash dedup makes overlaps safe):
#   mkdir -p /tmp/restore && for t in <tarballs>; do tar -xzf "$t" -C /tmp/restore; done
#   python3 scripts/fleet_warehouse_attribution.py --no-collect --stage-dir /tmp/restore/fleet-sessions --out-dir reports
#   python3 scripts/warehouse_cost_demo.py load-bigquery
set -euo pipefail

STAGE_DIR="${FLEET_STAGE_DIR:-/tmp/fleet-sessions}"
DEST="${SESSION_ARCHIVE_DEST:-}"

if [[ -z "$DEST" ]]; then
  echo "SESSION_ARCHIVE_DEST not set; skipping archive."
  exit 0
fi
if [[ ! -d "$STAGE_DIR" ]]; then
  echo "stage dir $STAGE_DIR missing; nothing to archive." >&2
  exit 1
fi

ts="$(date -u +%Y%m%dT%H%M%SZ)"
name="fleet-sessions-$ts.tar.gz"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
tarball="$tmp/$name"

# jsonl only, keeps the stage's <host>/<kind>/ layout under a stable top dir.
tar -czf "$tarball" -C "$(dirname "$STAGE_DIR")" "$(basename "$STAGE_DIR")"
size="$(du -h "$tarball" | cut -f1)"

case "$DEST" in
  gs://*)
    gcloud storage cp "$tarball" "${DEST%/}/$name"
    ;;
  *)
    mkdir -p "$DEST"
    cp "$tarball" "$DEST/$name"
    ;;
esac

echo "archived $size -> ${DEST%/}/$name"
