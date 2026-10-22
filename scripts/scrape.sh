#!/usr/bin/env bash
# Google Maps scrape for one keyword: creates the job, polls, downloads, and saves the 8 lead fields to a CSV.
# Usage:   ./scripts/scrape.sh "<keyword>" <lat> <lon> [depth] [--proxies <list>] [--proxies-file <file>]
# Example: ./scripts/scrape.sh "coffee shops in Austin TX" 30.2672 -97.7431 5
# Proxies: ./scripts/scrape.sh "coffee shops in Austin TX" 30.2672 -97.7431 5 --proxies-file proxies.txt
#          ./scripts/scrape.sh "coffee shops in Austin TX" 30.2672 -97.7431 5 \
#            --proxies '["socks5://user:pass@host:1080", "http://host2:8080"]'
#   --proxies takes a comma- or space-separated list or a JSON-style array, and can be repeated.
#   The file has one proxy per line (blank lines and # comments skipped). Both can be combined.
#   Types: socks5://, socks5h://, http://, https:// (a bare host:port means http://), user:pass@ allowed.
#   With neither flag, SCRAPER_PROXIES from .env (comma-separated) is used. Each proxy is test-fetched
#   through Google Maps first, and only the working ones are sent to the engine, which rotates them.
set -euo pipefail

# Read SCRAPER_* from the kit's .env, the same file docker compose reads, so a remapped
# port needs one edit. Variables already exported in the shell win over the file.
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
if [ -f "$ENV_FILE" ]; then
  while IFS='=' read -r k v || [ -n "$k" ]; do
    case "$k" in SCRAPER_BASE_URL|SCRAPER_PORT|SCRAPER_API_KEY|SCRAPER_PROXIES) ;; *) continue ;; esac
    v="${v%$'\r'}"; v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"
    [ -n "${!k+x}" ] || export "$k=$v"
  done < "$ENV_FILE"
fi

BASE="${SCRAPER_BASE_URL:-http://localhost:${SCRAPER_PORT:-8080}}"
KEY="${SCRAPER_API_KEY:-}"
# NOTE: empty-array expansion needs the ${a[@]+...} guard — plain "${AUTH[@]}" is an
# unbound-variable error under 'set -u' on bash 3.2 (stock macOS), which is the
# common case here since local use needs no API key.
AUTH=(); [ -n "$KEY" ] && AUTH=(-H "X-API-Key: $KEY")

# Pull the proxy flags out wherever they sit; everything else stays positional.
PROXIES_FILE=""; PROXIES_ARG=""; POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --proxies-file)
      [ $# -ge 2 ] || { echo "✗ --proxies-file needs a path." >&2; exit 1; }
      PROXIES_FILE="$2"; shift 2 ;;
    --proxies-file=*) PROXIES_FILE="${1#*=}"; shift ;;
    --proxies)
      [ $# -ge 2 ] || { echo "✗ --proxies needs a list." >&2; exit 1; }
      PROXIES_ARG="$PROXIES_ARG,$2"; shift 2 ;;
    --proxies=*) PROXIES_ARG="$PROXIES_ARG,${1#*=}"; shift ;;
    *) POS+=("$1"); shift ;;
  esac
done
set -- ${POS[@]+"${POS[@]}"}

KEYWORD="${1:-}"; LAT="${2:-}"; LON="${3:-}"; DEPTH="${4:-5}"
if [ -z "$KEYWORD" ] || [ -z "$LAT" ] || [ -z "$LON" ]; then
  echo "Usage: $0 \"<keyword>\" <lat> <lon> [depth] [--proxies <list>] [--proxies-file <file>]" >&2
  echo "Example: $0 \"coffee shops in Austin TX\" 30.2672 -97.7431 5" >&2
  exit 1
fi

# Is the scraper up?
if ! curl -s -m 5 ${AUTH[@]+"${AUTH[@]}"} "$BASE/api/v1/jobs" >/dev/null 2>&1; then
  echo "✗ Scraper not reachable at $BASE — run 'docker compose up -d' first." >&2
  exit 1
fi

# --- Proxies: read, validate, keep only the ones Google Maps answers through ---
# A list is split on commas and whitespace, with JSON array brackets and quotes stripped.
split_list() { printf '%s' "$1" | tr -d "[]\"'" | tr ', \t' '\n\n\n'; }
RAW_PROXIES=""
if [ -n "$PROXIES_ARG" ]; then RAW_PROXIES=$(split_list "$PROXIES_ARG"); fi
if [ -n "$PROXIES_FILE" ]; then
  [ -r "$PROXIES_FILE" ] || { echo "✗ Cannot read proxies file: $PROXIES_FILE" >&2; exit 1; }
  RAW_PROXIES="$RAW_PROXIES
$(cat "$PROXIES_FILE")"
fi
if [ -z "$PROXIES_ARG" ] && [ -z "$PROXIES_FILE" ]; then RAW_PROXIES=$(split_list "${SCRAPER_PROXIES:-}"); fi
# Trim, drop blanks and comments, default a bare host:port to http://, de-dupe keeping order.
CANDIDATES=$(printf '%s\n' "$RAW_PROXIES" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
  | grep -v -e '^$' -e '^#' | sed -e '/:\/\//!s#^#http://#' | awk '!seen[$0]++' || true)

mask() { printf '%s' "$1" | sed 's#://[^@/]*@#://***@#'; }  # hide user:pass before printing

# Prints "OK<TAB>proxy" when Google Maps loads through it, else "<reason><TAB>proxy".
# Same verdicts as proxy_verdict() in scrape.py.
check_proxy() {
  local r code connect
  r=$(curl -s -o /dev/null -w '%{http_code} %{http_connect} %{redirect_url}' -x "$1" \
      --connect-timeout 5 -m 12 "https://www.google.com/maps" 2>/dev/null) || true
  code="${r%% *}"; connect="${r#* }"; connect="${connect%% *}"
  case "$r" in
    *"/sorry/"*|429\ *) printf 'blocked by Google\t%s\n' "$1" ;;
    2??\ *|3??\ *) printf 'OK\t%s\n' "$1" ;;
    000\ 000*|000\ 200*|"") printf 'unreachable or timed out\t%s\n' "$1" ;;
    000\ *) printf 'proxy refused (HTTP %s)\t%s\n' "$connect" "$1" ;;
    *) printf 'HTTP %s\t%s\n' "$code" "$1" ;;
  esac
}

PROXY_FIELD=""
if [ -n "$CANDIDATES" ]; then
  while IFS= read -r p; do
    case "$p" in
      socks5://*|socks5h://*|http://*|https://*) ;;
      *) echo "✗ Proxy $(mask "$p") needs a scheme: socks5://, socks5h://, http:// or https://" >&2; exit 1 ;;
    esac
    case "$p" in *[\"\\[:space:]]*) echo "✗ Proxy $(mask "$p") contains a quote, backslash or space." >&2; exit 1 ;; esac
  done <<< "$CANDIDATES"

  N=$(printf '%s\n' "$CANDIDATES" | grep -c .)
  echo "▶ Checking $N proxies against Google Maps (20 at a time, up to 12s each)…"
  CHECK_DIR="$(mktemp -d)"; i=0
  while IFS= read -r p; do
    i=$((i + 1))
    check_proxy "$p" > "$CHECK_DIR/$(printf '%05d' "$i")" &
    if [ $((i % 20)) -eq 0 ]; then wait; fi
  done <<< "$CANDIDATES"
  wait
  RESULTS=$(cat "$CHECK_DIR"/*); rm -rf "$CHECK_DIR"

  WORKING=$(printf '%s\n' "$RESULTS" | awk -F'\t' '$1 == "OK" { print $2 }')
  while IFS="$(printf '\t')" read -r why p; do
    [ "$why" = OK ] || echo "  ✗ $(mask "$p"): $why"
  done <<< "$RESULTS"
  if [ -z "$WORKING" ]; then
    echo "✗ None of the $N proxies reached Google Maps. Fix the list, or drop --proxies/--proxies-file" >&2
    echo "  (and unset SCRAPER_PROXIES) to scrape from your own IP." >&2
    exit 1
  fi
  echo "  ✓ $(printf '%s\n' "$WORKING" | grep -c .) of $N working — the engine will rotate them"
  # The engine runs in Docker, where localhost is the container itself, not this machine.
  if printf '%s\n' "$WORKING" | grep -qE '://([^@/]*@)?(localhost|127\.)'; then
    echo "  ⚠️  localhost proxies pass this check but the Docker engine can't reach them;"
    echo "     use host.docker.internal in their place."
  fi
  PROXY_FIELD=",\"proxies\":[$(printf '%s\n' "$WORKING" | sed 's/.*/"&"/' | paste -sd, -)]"
fi

echo "▶ Creating job: \"$KEYWORD\" @ $LAT,$LON depth=$DEPTH (email extraction ON)"
# email:true makes the engine visit each business website for emails (the key lead field). A little slower.
BODY=$(printf '{"name":"scrape-cli","keywords":["%s"],"lang":"en","zoom":15,"lat":"%s","lon":"%s","fast_mode":false,"radius":10000,"depth":%s,"email":true,"max_time":600%s}' \
  "$KEYWORD" "$LAT" "$LON" "$DEPTH" "$PROXY_FIELD")
ID=$(curl -s -X POST "$BASE/api/v1/jobs" ${AUTH[@]+"${AUTH[@]}"} -H "Content-Type: application/json" -d "$BODY" \
     | grep -oE '[a-f0-9-]{36}' | head -1 || true)
[ -n "$ID" ] || { echo "✗ Failed to create job (check required fields)." >&2; exit 1; }
echo "  job id: $ID"

echo "▶ Polling (up to ~10 min)…"
for i in $(seq 1 60); do
  # '|| true' keeps the script alive under 'set -euo pipefail' when grep finds no match yet
  S=$(curl -s ${AUTH[@]+"${AUTH[@]}"} "$BASE/api/v1/jobs/$ID" | grep -oE '"Status":"[^"]*"' | head -1 | cut -d'"' -f4 || true)
  printf '\r  status: %-10s (attempt %d)' "${S:-?}" "$i"
  [ "$S" = ok ] && { echo; break; }
  [ "$S" = failed ] && { echo; echo "✗ Job failed." >&2; exit 1; }
  sleep 10
done
[ "$S" = ok ] || { echo; echo "✗ Timed out waiting for job." >&2; exit 1; }

OUT="results-$(echo "$KEYWORD" | tr ' /' '__' | tr -cd 'A-Za-z0-9_-').csv"
RAW="$(mktemp)"
echo "▶ Downloading…"
curl -s ${AUTH[@]+"${AUTH[@]}"} "$BASE/api/v1/jobs/$ID/download" -o "$RAW"

# Trim the raw 34-column CSV to the 8 lead fields (drops coordinates, IDs, hours, images).
if command -v python3 >/dev/null 2>&1; then
  python3 - "$RAW" "$OUT" <<'PY'
import csv, sys
LEAD = ["title", "phone", "emails", "website", "category", "address", "review_rating", "review_count"]
with open(sys.argv[1], newline='') as f:
    rows = list(csv.DictReader(f))
with open(sys.argv[2], "w", newline='') as f:
    w = csv.DictWriter(f, fieldnames=LEAD); w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in LEAD})
print(f"✓ Done — {len(rows)} leads saved to {sys.argv[2]}")
print("  (name, phone, email, website, category, address, rating, reviews)\n")
print("Preview (name | phone | email | website):")
for r in rows[:6]:
    print(f"  • {r.get('title','')} | {r.get('phone','')} | {r.get('emails','')} | {r.get('website','')}")
PY
  rm -f "$RAW"
else
  mv "$RAW" "$OUT"
  echo "✓ Done — raw CSV saved to $OUT (install python3 to auto-trim to lead fields only)"
fi
