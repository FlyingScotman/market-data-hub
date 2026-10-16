#!/usr/bin/env bash
# Build the deployable archive: code (git archive HEAD) + seed dump + seed report + manifest.
#
#   deploy/make_bundle.sh --database-url URL [--out-dir DIR] [--date YYYYMMDD] [--allow-dirty]
#   MDHUB_BUNDLE_DATABASE_URL=URL deploy/make_bundle.sh      # or MDHUB_DATABASE_URL
#
# Writes DIR/market-data-hub-<YYYYMMDD>.tar.gz (default DIR: dist/) and its .sha256. The archive
# has one top-level directory mdhub/:
#   mdhub/...                   git archive of HEAD (refused if the work tree is dirty unless
#                               --allow-dirty; uncommitted changes are never in the archive)
#   mdhub/db/mdhub.dump         pg_dump -Fc --no-owner --no-acl (pg_dump 16) of the database
#   mdhub/db/mdhub.dump.sha256  "<sha256>  mdhub.dump"
#   mdhub/SEED_REPORT.md        `mdhub report seed` of the same snapshot as the dump
#   mdhub/MANIFEST.txt          git commit, migrations in the dump, pg_dump version, sizes
#   mdhub/SHA256SUMS            sha256 of every file in mdhub/ (sha256sum -c format)
#
# Dump, report and manifest read one exported snapshot (pg_export_snapshot), so they agree even
# while loaders keep writing. Tools: pg_dump/psql 16 from $PG_BIN, else PATH, else the pgserver
# wheel in .venv; mdhub from $MDHUB_CMD (default .venv/bin/mdhub, else `uv run mdhub`).
# The URL reaches pg_dump/psql as an argument (visible in ps): use a socket / .pgpass password.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

die() { echo "make_bundle: $*" >&2; exit 1; }
log() { echo "==> $*" >&2; }

url="${MDHUB_BUNDLE_DATABASE_URL:-${MDHUB_DATABASE_URL:-}}"
out_dir="$ROOT/dist"
stamp="$(date -u +%Y%m%d)"
allow_dirty=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --database-url) [[ $# -ge 2 ]] || die "--database-url needs a value"; url="$2"; shift 2 ;;
        --out-dir) [[ $# -ge 2 ]] || die "--out-dir needs a value"; out_dir="$2"; shift 2 ;;
        --date) [[ $# -ge 2 ]] || die "--date needs a value"; stamp="$2"; shift 2 ;;
        --allow-dirty) allow_dirty=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done
[[ -n "$url" ]] || die "no database: pass --database-url or set MDHUB_BUNDLE_DATABASE_URL"
[[ "$stamp" =~ ^[0-9]{8}$ ]] || die "--date: expected YYYYMMDD, got $stamp"

# ------------------------------------------------------------------------------ git state
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$ROOT is not a git work tree"
commit="$(git rev-parse HEAD)"
dirty="$(git status --porcelain --untracked-files=normal)"
if [[ -n "$dirty" ]]; then
    if [[ "$allow_dirty" != 1 ]]; then
        echo "$dirty" >&2
        die "work tree is dirty (commit first, or --allow-dirty to bundle HEAD anyway)"
    fi
    echo "warning: work tree is dirty; the bundle contains HEAD ${commit:0:12} only" >&2
fi

# ------------------------------------------------------------------------------ tools
pg_major() { "$1" --version | sed -E 's/.* ([0-9]+)(\.[0-9]+)*.*/\1/'; }
if [[ -z "${PG_BIN:-}" ]]; then
    if command -v pg_dump >/dev/null && [[ "$(pg_major "$(command -v pg_dump)")" == 16 ]]; then
        PG_BIN="$(dirname "$(command -v pg_dump)")"
    else
        for d in "$ROOT"/.venv/lib/python3*/site-packages/pgserver/pginstall/bin; do
            if [[ -x "$d/pg_dump" ]]; then PG_BIN="$d"; break; fi
        done
    fi
fi
[[ -n "${PG_BIN:-}" && -x "$PG_BIN/pg_dump" && -x "$PG_BIN/psql" ]] \
    || die "pg_dump/psql 16 not found (set PG_BIN)"
[[ "$(pg_major "$PG_BIN/pg_dump")" == 16 ]] \
    || die "need pg_dump 16 (the VPS restores with pg_restore 16), found: $("$PG_BIN/pg_dump" --version)"
PSQL="$PG_BIN/psql"
PG_DUMP="$PG_BIN/pg_dump"

if [[ -z "${MDHUB_CMD:-}" ]]; then
    if [[ -x "$ROOT/.venv/bin/mdhub" ]]; then MDHUB_CMD="$ROOT/.venv/bin/mdhub"; else MDHUB_CMD="uv run --frozen mdhub"; fi
fi
if command -v sha256sum >/dev/null; then SHA256="sha256sum"; else SHA256="shasum -a 256"; fi
sha_of() { $SHA256 "$1" | cut -d' ' -f1; }
size_of() { wc -c <"$1" | tr -d ' '; }

server_major="$("$PSQL" -X -q -A -t -v ON_ERROR_STOP=1 -d "$url" -c "SELECT current_setting('server_version_num')::int / 10000")" \
    || die "cannot connect to the database"
[[ "$server_major" == 16 ]] || die "source server is PostgreSQL $server_major, expected 16"

# ------------------------------------------------------------------------------ staging
mkdir -p "$out_dir"
out_dir="$(cd "$out_dir" && pwd)"
name="market-data-hub-${stamp}"
out="$out_dir/${name}.tar.gz"
work="$(mktemp -d "${TMPDIR:-/tmp}/mdhub-bundle.XXXXXX")"
stage="$work/stage/mdhub"
holder_pid=""
cleanup() {
    if [[ -n "$holder_pid" ]]; then
        exec 3>&- 2>/dev/null || true
        kill "$holder_pid" 2>/dev/null || true
        wait "$holder_pid" 2>/dev/null || true
    fi
    rm -rf "${work:?}"
}
trap cleanup EXIT

log "git archive ${commit:0:12}"
mkdir -p "$stage"
git archive --format=tar HEAD | tar -x -C "$stage"
code_files="$(find "$stage" -type f | wc -l | tr -d ' ')"
code_bytes="$(find "$stage" -type f -exec cat {} + | wc -c | tr -d ' ')"
mkdir -p "$stage/db"

# ------------------------------------------------------------------------------ snapshot
log "exporting a snapshot"
mkfifo "$work/ctl"
"$PSQL" -X -q -A -t -v ON_ERROR_STOP=1 -d "$url" <"$work/ctl" >"$work/snapshot.out" 2>"$work/snapshot.err" &
holder_pid=$!
exec 3>"$work/ctl"
printf '%s\n' "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;" "SELECT pg_export_snapshot();" >&3
snap=""
for _ in $(seq 1 300); do
    snap="$(head -n 1 "$work/snapshot.out" 2>/dev/null || true)"
    [[ -n "$snap" ]] && break
    kill -0 "$holder_pid" 2>/dev/null || die "snapshot export failed: $(cat "$work/snapshot.err")"
    sleep 0.1
done
[[ "$snap" =~ ^[0-9A-Fa-f]+-[0-9A-Fa-f]+(-[0-9]+)?$ ]] || die "no snapshot id (got '$snap')"

q() { # query inside the exported snapshot; tab-separated, unaligned
    "$PSQL" -X -q -A -t -F $'\t' -v ON_ERROR_STOP=1 -d "$url" \
        -c "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" \
        -c "SET TRANSACTION SNAPSHOT '$snap'" -c "$1" -c "COMMIT"
}

log "pg_dump (snapshot $snap)"
"$PG_DUMP" -Fc --no-owner --no-acl --snapshot="$snap" -f "$stage/db/mdhub.dump" -d "$url"
(cd "$stage/db" && $SHA256 mdhub.dump >mdhub.dump.sha256)

log "seed report"
MDHUB_DATABASE_URL="$url" $MDHUB_CMD report seed --snapshot "$snap" --out "$stage/SEED_REPORT.md"

log "manifest"
db_info="$(q "SELECT current_database(), COALESCE(host(inet_server_addr()), 'local socket') || ':' || current_setting('port'), current_user, current_setting('server_version'), pg_database_size(current_database())")"
IFS=$'\t' read -r db_name db_host db_user server_version db_size <<<"$db_info"
db_migrations="$(q "SELECT version, checksum, to_char(applied_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM ops.schema_migration ORDER BY version")" \
    || die "ops.schema_migration not readable: is the database migrated?"
printf 'COMMIT;\n' >&3
exec 3>&-
wait "$holder_pid" || die "snapshot session failed: $(cat "$work/snapshot.err")"
holder_pid=""

# Code migrations vs the dump: a checksum mismatch would make `mdhub migrate` refuse after the
# restore, so it is fatal here; code migrations missing from the dump are applied by the restore.
code_migrations=""
pending=""
mismatch=""
for f in "$stage"/src/mdhub/db/migrations/*.sql; do
    v="$(basename "$f" .sql)"
    sum="$(sha_of "$f")"
    code_migrations="${code_migrations}${v}"$'\t'"${sum}"$'\n'
    db_sum="$(printf '%s\n' "$db_migrations" | awk -F'\t' -v v="$v" '$1 == v {print $2}')"
    if [[ -z "$db_sum" ]]; then
        pending="${pending} ${v}"
    elif [[ "$db_sum" != "$sum" ]]; then
        mismatch="${mismatch} ${v}"
    fi
done
[[ -z "$mismatch" ]] || die "migration checksums differ between the dump and HEAD:${mismatch}"
unknown=""
while IFS=$'\t' read -r v _rest; do
    [[ -z "$v" ]] && continue
    grep -q "^${v}"$'\t' <<<"$code_migrations" || unknown="${unknown} ${v}"
done <<<"$db_migrations"
[[ -z "$unknown" ]] || die "the dump has migrations that HEAD does not know:${unknown}"

dump_bytes="$(size_of "$stage/db/mdhub.dump")"
dump_sha="$(sha_of "$stage/db/mdhub.dump")"
{
    echo "market-data-hub bundle ${name}"
    echo
    echo "created_at_utc:   $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "git_commit:       ${commit}"
    echo "git_describe:     $(git describe --always --dirty 2>/dev/null || echo "${commit:0:12}")"
    if [[ -n "$dirty" ]]; then
        echo "git_dirty:        yes (--allow-dirty; uncommitted changes are NOT in this bundle)"
    else
        echo "git_dirty:        no"
    fi
    echo "code_files:       ${code_files} files, ${code_bytes} bytes (git archive HEAD)"
    echo "source_database:  ${db_name} on ${db_host} as ${db_user}"
    echo "server_version:   PostgreSQL ${server_version}"
    echo "database_size:    ${db_size} bytes (pg_database_size)"
    echo "pg_dump:          $("$PG_DUMP" --version) (-Fc --no-owner --no-acl)"
    echo "snapshot:         ${snap} (dump, SEED_REPORT.md and this manifest)"
    echo "dump:             db/mdhub.dump ${dump_bytes} bytes sha256 ${dump_sha}"
    echo "seed_report:      SEED_REPORT.md $(size_of "$stage/SEED_REPORT.md") bytes"
    echo
    echo "migrations applied in the dumped database (ops.schema_migration):"
    printf '%s\n' "$db_migrations" | awk -F'\t' '{printf "  %-32s %s  %s\n", $1, $2, $3}'
    echo
    echo "migrations in the code not in the dump (applied by mdhub migrate after restore):${pending:- none}"
    echo
    echo "restore: see deploy/README_DEPLOY.md (deploy/restore_seed.sh [--database-url URL])"
} >"$stage/MANIFEST.txt"

log "SHA256SUMS"
(
    cd "$stage"
    find . -type f ! -name SHA256SUMS | sed 's|^\./||' | LC_ALL=C sort | tr '\n' '\0' \
        | xargs -0 $SHA256 >"$work/SHA256SUMS"
)
mv "$work/SHA256SUMS" "$stage/SHA256SUMS"

# ------------------------------------------------------------------------------ archive
log "archive ${out}"
if tar --version 2>/dev/null | grep 'GNU tar' >/dev/null; then
    tar_opts=(--owner=0 --group=0 --numeric-owner)
else
    tar_opts=(--uid 0 --gid 0 --uname root --gname root --no-xattrs --no-mac-metadata)
fi
COPYFILE_DISABLE=1 tar "${tar_opts[@]}" -C "$work/stage" -czf "$out.partial" mdhub
mv "$out.partial" "$out"

log "verifying the archive listing"
listing="$(tar -tzf "$out")"
forbidden="$(printf '%s\n' "$listing" | grep -E \
    '(^|/)\.env$|(^|/)\.env\.|^mdhub/data/|(^|/)\.handoff/|(^|/)\.venv/|(^|/)\.git/|(^|/)__pycache__/|\.pyc$|\.sqlite3|^mdhub/dist/|(^|/)\._|\.DS_Store|^mdhub/deploy/backups/|^mdhub/deploy/seed/.+\.dump$|(^|/)\.(pytest|mypy|ruff)_cache/' \
    || true)"
if [[ -n "$forbidden" ]]; then
    rm -f "$out"
    echo "$forbidden" >&2
    die "forbidden paths in the archive (removed it)"
fi
for required in mdhub/db/mdhub.dump mdhub/db/mdhub.dump.sha256 mdhub/SEED_REPORT.md \
    mdhub/MANIFEST.txt mdhub/SHA256SUMS mdhub/pyproject.toml mdhub/uv.lock \
    mdhub/deploy/restore_seed.sh mdhub/src/mdhub/cli.py; do
    grep -qx "$required" <<<"$listing" || die "missing in the archive: $required"
done
(cd "$out_dir" && $SHA256 "$(basename "$out")" >"$(basename "$out").sha256")

echo "bundle:   $out ($(size_of "$out") bytes, $(printf '%s\n' "$listing" | grep -vc '/$') files)"
echo "sha256:   $(sha_of "$out")"
echo "commit:   ${commit}$([[ -n "$dirty" ]] && echo ' (work tree dirty, not included)')"
echo "dump:     ${dump_bytes} bytes, snapshot ${snap}"
