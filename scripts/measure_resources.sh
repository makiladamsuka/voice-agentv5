#!/usr/bin/env bash
# Sample CPU/RAM for start_robot.py (or any PID) over a duration.
set -euo pipefail

DURATION="${1:-60}"
INTERVAL="${2:-1}"
PATTERN="${3:-start_robot.py}"

pid="$(pgrep -f "$PATTERN" | head -1 || true)"
if [[ -z "$pid" ]]; then
  echo "No process matching '$PATTERN'" >&2
  exit 1
fi

echo "Sampling PID $pid ($PATTERN) every ${INTERVAL}s for ${DURATION}s"
echo "time,cpu_pct,rss_mb,vsz_mb,threads"
end=$((SECONDS + DURATION))
while [[ $SECONDS -lt $end ]]; do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Process exited" >&2
    break
  fi
  line="$(ps -p "$pid" -o %cpu=,rss=,vsz=,nlwp= 2>/dev/null || true)"
  if [[ -n "$line" ]]; then
    cpu="$(echo "$line" | awk '{print $1}')"
    rss_kb="$(echo "$line" | awk '{print $2}')"
    vsz_kb="$(echo "$line" | awk '{print $3}')"
    threads="$(echo "$line" | awk '{print $4}')"
    rss_mb="$(awk "BEGIN {printf \"%.1f\", $rss_kb/1024}")"
    vsz_mb="$(awk "BEGIN {printf \"%.1f\", $vsz_kb/1024}")"
    echo "$(date +%H:%M:%S),$cpu,$rss_mb,$vsz_mb,$threads"
  fi
  sleep "$INTERVAL"
done

echo "---"
echo "Load average: $(cut -d' ' -f1-3 /proc/loadavg)"
if [[ -r /proc/meminfo ]]; then
  awk '/MemTotal|MemAvailable|SwapTotal|SwapFree/ {print}' /proc/meminfo
fi
