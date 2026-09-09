#!/usr/bin/env bash
# Load a `make docker-export` tarball into the Compose volume.
#
#   scripts/import-state.sh --state openexec-state.tar.gz --replace
#
# Runs the import through `docker compose run` on the `api` service purely to
# borrow its volume mount, so Compose resolves the volume name itself — worth
# doing, because the volume is named after the Compose project
# (`docker_executive_data`, from the docker/ directory), not `executive_data`.
#
# Do this while the stack is down, before `up -d`. The API writes to the same
# files, and swapping a database under a running process is how you get a
# half-imported install.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"

state="openexec-state.tar.gz"
compose="$root/docker/docker-compose.ghcr.yml"
env_file="$root/.env"
mode="safe"
chosen=""
dry_run=0

usage() {
    cat <<'USAGE'
Usage: scripts/import-state.sh [options]

  --state FILE      archive from `make docker-export` (default: openexec-state.tar.gz)
  --compose FILE    compose file (default: docker/docker-compose.ghcr.yml)
  --env-file FILE   env file passed to compose (default: .env)
  --replace         move existing state aside into the volume, then import
  --merge           extract over existing state
  --dry-run         show what would run, touch nothing
  -h, --help        this

With neither --replace nor --merge, an import into a volume that already holds
state is refused rather than silently merged.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --state)    state="${2:?--state needs a path}"; shift 2 ;;
        --compose)  compose="${2:?--compose needs a path}"; shift 2 ;;
        --env-file) env_file="${2:?--env-file needs a path}"; shift 2 ;;
        --replace)  chosen="$chosen replace"; mode="replace"; shift ;;
        --merge)    chosen="$chosen merge"; mode="merge"; shift ;;
        --dry-run)  dry_run=1; shift ;;
        -h|--help)  usage; exit 0 ;;
        *)          echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# Both flags set `mode`, so without this the second silently wins — and the
# two mean opposite things about existing data.
if [ "$(echo $chosen | wc -w)" -gt 1 ]; then
    echo "--replace and --merge are mutually exclusive (got:$chosen)" >&2
    exit 2
fi

[ -f "$state" ] || { echo "no such archive: $state" >&2; exit 2; }
[ -f "$compose" ] || { echo "no such compose file: $compose" >&2; exit 2; }
[ -f "$env_file" ] || { echo "no such env file: $env_file" >&2; exit 2; }

# Validate the archive on the host, before it is handed to a tar running as
# root inside the container. GNU tar refuses `..` members, but relying on that
# puts the only check on the far side of the trust boundary — and an archive
# carrying entries the volume does not expect is a sign it is not the file the
# operator thinks it is, which is worth catching either way.
expected="company chroma_db episodic_memory.db"
bad=""
while IFS= read -r member; do
    [ -n "$member" ] || continue
    case "$member" in
        /*|*..*) bad="$bad\n  absolute or traversing path: $member" ; continue ;;
    esac
    top="${member%%/*}"
    case " $expected " in
        *" $top "*) ;;
        *) bad="$bad\n  unexpected entry: $member" ;;
    esac
done < <(tar -tzf "$state")

if [ -n "$bad" ]; then
    echo "$state does not look like a docker-export archive:" >&2
    printf "%b\n" "$bad" >&2
    echo >&2
    echo "Expected only: $expected" >&2
    exit 2
fi

state_abs="$(cd "$(dirname "$state")" && pwd)/$(basename "$state")"

compose_cmd=(
    docker compose --env-file "$env_file" -f "$compose"
    run --rm --no-deps -T
    -v "$state_abs:/state.tar.gz:ro"
    api sh -s -- "$mode"
)

if [ "$dry_run" -eq 1 ]; then
    echo "Would run:"
    printf '  %s\n' "${compose_cmd[*]}"
    echo "  < $here/_import-into-volume.sh"
    echo
    echo "Archive contents:"
    tar -tzf "$state" | sed 's/^/  /' | head -20
    exit 0
fi

"${compose_cmd[@]}" < "$here/_import-into-volume.sh"

cat <<EOF

Now start the stack:
  docker compose --env-file $env_file -f $compose up -d

The API runs its schema migrations on startup (idempotent, additive), so a
database exported from an older build upgrades itself on first boot.
EOF
