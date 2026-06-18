#!/usr/bin/env bash
set -eu

interval="${BDAG_CPU_GUARD_INTERVAL_SECONDS:-30}"
log_file="${BDAG_CPU_GUARD_LOG:-/home/jeremy/blockdag-asic-pool/ops/runtime/logs/cpu-thermal-guard.log}"
last_policy=""

mkdir -p "$(dirname "$log_file")"

find_cpu_temp_file() {
  for type_file in /sys/class/thermal/thermal_zone*/type; do
    [ -r "$type_file" ] || continue
    type="$(cat "$type_file")"
    case "$type" in
      x86_pkg_temp|Package*|TCPU|B0D4)
        temp_file="${type_file%/type}/temp"
        [ -r "$temp_file" ] && printf '%s\n' "$temp_file" && return 0
        ;;
    esac
  done
  for temp_file in /sys/class/thermal/thermal_zone*/temp; do
    [ -r "$temp_file" ] && printf '%s\n' "$temp_file" && return 0
  done
  return 1
}

write_if_possible() {
  path="$1"
  value="$2"
  [ -w "$path" ] && printf '%s\n' "$value" > "$path"
}

set_epp_performance() {
  for preference in /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do
    write_if_possible "$preference" performance
  done
}

apply_policy() {
  temp_millic="$1"
  temp_c=$((temp_millic / 1000))

  if [ "$temp_millic" -ge 92000 ]; then
    policy="critical"
    min_perf=10
    max_perf=80
  elif [ "$temp_millic" -ge 85000 ]; then
    policy="hot"
    min_perf=15
    max_perf=100
  elif [ "$temp_millic" -ge 78000 ]; then
    policy="warm"
    min_perf=25
    max_perf=100
  elif [ "$temp_millic" -ge 70000 ]; then
    policy="balanced"
    min_perf=35
    max_perf=100
  else
    policy="boost"
    min_perf=45
    max_perf=100
  fi

  if [ -d /sys/devices/system/cpu/intel_pstate ]; then
    write_if_possible /sys/devices/system/cpu/intel_pstate/no_turbo 0
    write_if_possible /sys/devices/system/cpu/intel_pstate/max_perf_pct "$max_perf"
    write_if_possible /sys/devices/system/cpu/intel_pstate/min_perf_pct "$min_perf"
  fi
  set_epp_performance

  current_policy="${policy}:${min_perf}:${max_perf}"
  if [ "$current_policy" != "$last_policy" ]; then
    printf '[%s] temp_c=%s policy=%s min_perf_pct=%s max_perf_pct=%s\n' \
      "$(date --iso-8601=seconds)" "$temp_c" "$policy" "$min_perf" "$max_perf" >> "$log_file"
    last_policy="$current_policy"
  fi
}

temp_file="$(find_cpu_temp_file)"
printf '[%s] cpu thermal guard started temp_file=%s interval=%ss\n' \
  "$(date --iso-8601=seconds)" "$temp_file" "$interval" >> "$log_file"

while true; do
  temp_millic="$(cat "$temp_file")"
  apply_policy "$temp_millic"
  sleep "$interval"
done
