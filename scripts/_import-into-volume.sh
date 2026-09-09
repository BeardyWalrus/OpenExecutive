#!/bin/sh
# Runs INSIDE the API container, against the mounted volume. Piped in by
# scripts/import-state.sh; not meant to be run directly on a host.
#
#   sh _import-into-volume.sh safe|replace|merge
#
# The volume holds more than the export does. `WORKSPACE_MCP_CREDENTIALS_DIR`
# puts Google Workspace credentials at /data/google_credentials, and they are
# deliberately not in the tarball — so "replace" must never mean wiping /data.
# Only the three entries the export actually manages are touched; everything
# else in the volume is left exactly as it was.
#
# Paths are overridable so the logic can be exercised against a scratch
# directory without a Docker daemon.
set -eu

MODE="${1:?usage: $0 safe|replace|merge}"
DATA="${IMPORT_DATA_DIR:-/data}"
STATE="${IMPORT_STATE_FILE:-/state.tar.gz}"

# Exactly what `scripts/export-state.py` writes, and therefore the only
# entries this may move aside. Keep the two in step.
MANAGED="company chroma_db episodic_memory.db"

[ -d "$DATA" ] || { echo "no such directory: $DATA" >&2; exit 2; }
[ -f "$STATE" ] || { echo "no such archive: $STATE" >&2; exit 2; }

present=""
for entry in $MANAGED; do
    [ -e "$DATA/$entry" ] && present="$present $entry"
done
present="${present# }"

if [ -n "$present" ]; then
    case "$MODE" in
        safe)
            echo "The volume already holds state:" >&2
            for entry in $present; do echo "  $entry" >&2; done
            echo >&2
            echo "Extracting over it would merge the two installs — an old database" >&2
            echo "beside a newer vector store, or documents from both. Choose:" >&2
            echo "  --replace   move the above aside, then import (recoverable)" >&2
            echo "  --merge     extract over it anyway" >&2
            exit 3
            ;;
        replace)
            # `mkdir` without -p fails when the directory exists, which is the
            # uniqueness primitive here: the timestamp is second-granular, and
            # re-importing twice within the same second would otherwise `mv`
            # the new state INSIDE the previous backup — nesting
            # .superseded-X/chroma_db/chroma_db and destroying the recovery
            # path this mode exists to provide. Repeated re-imports are the
            # normal workflow, so this is not a corner case.
            base="$DATA/.superseded-$(date -u +%Y%m%dT%H%M%SZ)"
            superseded="$base"
            attempt=1
            until mkdir "$superseded" 2>/dev/null; do
                if [ "$attempt" -gt 99 ]; then
                    echo "cannot find an unused name for $base" >&2
                    exit 2
                fi
                superseded="$base-$attempt"
                attempt=$((attempt + 1))
            done
            for entry in $present; do
                mv "$DATA/$entry" "$superseded/$entry"
            done
            echo "Moved aside: $present"
            echo "  -> $superseded (delete it once the import looks right)"
            ;;
        merge)
            echo "Merging over: $present"
            ;;
        *)
            echo "unknown mode: $MODE" >&2
            exit 2
            ;;
    esac
fi

# --no-same-owner/--no-same-permissions: this runs as root (the image sets no
# USER), where tar would otherwise restore archived ownership and modes —
# setuid bits included — from a file that has crossed hosts.
tar --no-same-owner --no-same-permissions -xzf "$STATE" -C "$DATA"

echo "Imported into $DATA:"
for entry in $MANAGED; do
    [ -e "$DATA/$entry" ] && echo "  $entry"
done
exit 0
