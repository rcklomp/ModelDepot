#!/usr/bin/env bash
#
# Replicate the mirror store to another drive or NAS over rsync.
#
# The store is plain files (content-addressed blobs + manifests + refs), so
# rsync is the natural, bandwidth-efficient replication tool: it only transfers
# new/changed blobs, and blobs are immutable so partial transfers are safe to
# resume — an interrupted blob is simply re-fetched on the next run.
#
# Usage:
#   ./scripts/rsync-to-target.sh /volume1/llm-mirror /mnt/backup/llm-mirror
#   ./scripts/rsync-to-target.sh /volume1/llm-mirror nas2:/volume1/llm-mirror
#
# Common patterns:
#   - Replicate to an external USB SSD: ./scripts/rsync-to-target.sh /volume1/llm-mirror /Volumes/MySSD/llm-mirror
#   - Replicate to a second NAS:         ./scripts/rsync-to-target.sh /volume1/llm-mirror nas2:/volume1/llm-mirror
#   - Restore (reverse direction):       ./scripts/rsync-to-target.sh /Volumes/MySSD/llm-mirror /volume1/llm-mirror
#
set -euo pipefail

SRC="${1:?usage: $0 <src-store> <dest-store>}"
DEST="${2:?usage: $0 <src-store> <dest-store>}"

log() { echo "[rsync-to-target] $*"; }

# Ensure trailing slash on SRC so rsync copies *contents* into DEST (not a
# nested dir). DEST is created if missing (for local targets) or must exist
# (for remote targets).
SRC="${SRC%/}/"

log "replicating $SRC -> $DEST"

# --partial --inplace: resume interrupted blob transfers.
# --delete:            keep DEST as a true mirror (removes blobs GC'd on SRC).
# -a:                  recursive, preserve perms/times (blobs are immutable).
# --checksum:          blobs are content-addressed, but --checksum avoids
#                      re-transferring when only mtime changed after a copy.
rsync -av --partial --inplace --delete --checksum --human-readable "$SRC" "$DEST"

log "done. Verify on the target with: MIRROR_ROOT=$DEST repo verify <repo_id>"
