#!/usr/bin/env bash
set -eu

min_perf_pct="${BDAG_CPU_MIN_PERF_PCT:-40}"

if command -v powerprofilesctl >/dev/null 2>&1; then
  powerprofilesctl set performance >/dev/null 2>&1 || true
fi

if [ -d /sys/devices/system/cpu/intel_pstate ]; then
  [ -w /sys/devices/system/cpu/intel_pstate/no_turbo ] && echo 0 > /sys/devices/system/cpu/intel_pstate/no_turbo
  [ -w /sys/devices/system/cpu/intel_pstate/max_perf_pct ] && echo 100 > /sys/devices/system/cpu/intel_pstate/max_perf_pct
  [ -w /sys/devices/system/cpu/intel_pstate/min_perf_pct ] && echo "$min_perf_pct" > /sys/devices/system/cpu/intel_pstate/min_perf_pct
fi

for preference in /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do
  [ -w "$preference" ] && echo performance > "$preference"
done
