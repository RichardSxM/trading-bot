#!/usr/bin/env bash
# Export the paper loop's decisions and runs from the VPS for the parity check (spec 0008 AC-15).
#
#   scripts/fetch-paper-decisions.sh <from-date> <to-date>
#
# Copies, for the UTC days <from-date> to <to-date> (both inclusive, YYYY-MM-DD), every paper run of
# placeholder_timed that overlaps them (start, end, and its strategy_versions.config) and every
# decision of those runs inside them, into lab-reports/paper/<from>_<to>.csv. A run still going
# ends at the export time. The query runs read only, as dashboard_app, in the VPS Postgres
# container over `tailscale ssh kfs@<host>` (KFS_HOST, default kfs-vps). The rows then pass
# through `python -m lab.paper_csv` in the lab image, which writes each config in canonical_json
# form with its sha256, so `lab parity` hashes exactly what the run file's [paper] hashes.
#
# Exit codes: 0 written, 1 the query or the lab step failed, 64 bad arguments.
set -euo pipefail

usage() {
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

die() {
    echo "fetch-paper-decisions: $*" >&2
    exit 1
}

[ $# -eq 2 ] || usage
for day in "$1" "$2"; do
    case $day in [0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]) ;; *) usage ;; esac
done
[ "$1" \< "$2" ] || [ "$1" = "$2" ] || die "$1 is after $2"
FROM=$1
TO=$2
KFS_HOST=${KFS_HOST:-kfs-vps}

cd "$(git rev-parse --show-toplevel)"
command -v tailscale >/dev/null || die "tailscale CLI not found"
OUT=lab-reports/paper/${FROM}_${TO}.csv
mkdir -p lab-reports/paper
RAW=$(mktemp)
trap 'rm -f "$RAW"' EXIT

# psql reads the query from stdin, so :'from' and :'to' are its variables, quoted by psql.
# A day is checked by Postgres (a bad date fails the cast). Column order is lab.paper_csv.COLUMNS.
QUERY=$(
    cat <<'SQL'
\set ON_ERROR_STOP on
BEGIN READ ONLY;
COPY (
  WITH paper_runs AS (
    SELECT r.id, r.started_at, coalesce(r.ended_at, now()) AS ended_at, v.config
    FROM strategy_runs r
    JOIN strategy_versions v ON v.id = r.strategy_version_id
    JOIN strategies s ON s.id = v.strategy_id
    WHERE r.mode = 'paper' AND s.name = 'placeholder_timed'
      AND r.started_at < ((:'to'::date + 1)::timestamp AT TIME ZONE 'UTC')
      AND coalesce(r.ended_at, now()) > (:'from'::date::timestamp AT TIME ZONE 'UTC')
  )
  SELECT 'run' AS kind, id::text AS run_id, NULL AS decided_ns, NULL AS intent, NULL AS side,
         NULL AS risk_verdict, NULL AS risk_reason,
         to_char(started_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS started_at,
         to_char(ended_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS ended_at,
         config::text AS config, NULL AS config_sha256
  FROM paper_runs
  UNION ALL
  SELECT 'decision', d.run_id::text, d.decided_ns::text, d.intent, d.side, d.risk_verdict, d.risk_reason,
         NULL, NULL, NULL, NULL
  FROM decisions d
  JOIN paper_runs r ON r.id = d.run_id
  WHERE d.decided_at >= (:'from'::date::timestamp AT TIME ZONE 'UTC') AND d.decided_at < ((:'to'::date + 1)::timestamp AT TIME ZONE 'UTC')
  ORDER BY 1 DESC, 2, 3
) TO STDOUT WITH (FORMAT csv, HEADER);
COMMIT;
SQL
)

echo "fetch-paper-decisions: querying kfs@$KFS_HOST for $FROM to $TO" >&2
printf '%s\n' "$QUERY" | tailscale ssh "kfs@$KFS_HOST" \
    "sudo -n kfs-compose exec -T postgres sh -c 'psql -X -q -U dashboard_app -d \"\$POSTGRES_DB\" -v from=$FROM -v to=$TO'" \
    >"$RAW" || die "the query on $KFS_HOST failed"

echo "fetch-paper-decisions: canonicalizing configs in the lab image" >&2
docker compose --profile lab build --quiet lab >&2
docker compose run --rm --no-deps -T --entrypoint python lab -m lab.paper_csv <"$RAW" >"$OUT.tmp" ||
    { rm -f "$OUT.tmp"; die "lab.paper_csv failed"; }
mv -f "$OUT.tmp" "$OUT"
echo "fetch-paper-decisions: wrote $OUT" >&2
