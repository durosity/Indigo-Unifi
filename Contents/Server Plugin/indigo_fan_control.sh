#!/bin/bash
#
# Indigo UniFi Plugin - UNAS Fan Control
#
# Adapted from hoxxep/UNAS-Pro-fan-control (MIT) to support three named modes
# selectable via /etc/indigo_fan_control.conf. The plugin SSHs in to update
# that file and restart this service when the user changes the mode.
#
# Modes:
#   quiet    Lock PWM to a low constant value (quietest operation)
#   balance  Temperature-based linear curve (balanced acoustics + cooling)
#   cooling  Lock PWM to a high constant value (best cooling, loudest)
#   default  Exit cleanly and let UniFi's native fan controller run
#
# Run as `--service` to loop every 60s. Run without args for a one-shot
# diagnostic with logging.

set -euo pipefail

CONF_FILE="/etc/indigo_fan_control.conf"

# Defaults — overridden by reading the conf file at each loop iteration so
# config changes take effect on the next tick (within 60s) without needing a
# service restart.
DEFAULT_MODE="default"

# PWM levels for the three named modes. Values are 0-255 (raw PWM register).
# The QUIET value sits just above the firmware's minimum spin threshold so
# fans don't audibly cycle.
QUIET_PWM=39       # ~15% — quietest reliable operation
COOLING_PWM=220    # ~86% — strong cooling without going full noise

# Balance-mode curve parameters
CPU_TGT=50
CPU_MAX=70
HDD_TGT=32
HDD_MAX=50
BALANCE_MIN_PWM=80    # never go below this in balance mode

LOGGING=true
SERVICE=false
if [ "${1:-}" = "--service" ]; then
    # In service mode we DO log — output goes to systemd journal which is
    # essential for diagnosing why fan writes might not be taking effect.
    LOGGING=true
    SERVICE=true
fi

log_echo() {
    if $LOGGING; then
        echo "$@"
    fi
}

read_mode() {
    if [ -f "$CONF_FILE" ]; then
        # Read the first MODE= line, strip whitespace and quotes, lowercase.
        local raw
        raw=$(grep -E '^[[:space:]]*MODE[[:space:]]*=' "$CONF_FILE" 2>/dev/null | head -1 | sed -E 's/^[[:space:]]*MODE[[:space:]]*=[[:space:]]*//' | tr -d '"' | tr -d "'" | tr '[:upper:]' '[:lower:]' | xargs || true)
        if [ -n "${raw:-}" ]; then
            echo "$raw"
            return
        fi
    fi
    echo "$DEFAULT_MODE"
}

get_max_cpu_temp() {
    local cpu_devices=("hwmon/hwmon0/temp1_input" "hwmon/hwmon0/temp2_input" "hwmon/hwmon0/temp3_input" "thermal/thermal_zone0/temp")
    local CPU_TEMP=0
    for dev in "${cpu_devices[@]}"; do
        if [ -e "/sys/class/$dev" ]; then
            local temp
            temp=$(cat "/sys/class/$dev" 2>/dev/null || echo 0)
            temp=$((temp / 1000))
            if [[ "$temp" =~ ^[0-9]+$ ]] && [ "$temp" -gt "$CPU_TEMP" ]; then
                CPU_TEMP=$temp
            fi
        fi
    done
    echo "$CPU_TEMP"
}

get_max_hdd_temp() {
    local hdd_devices=(sda sdb sdc sdd sde sdf sdg sdh)
    local HDD_TEMP=0
    for dev in "${hdd_devices[@]}"; do
        if smartctl -a "/dev/$dev" &>/dev/null; then
            local temp
            temp=$(smartctl -a "/dev/$dev" 2>/dev/null | awk '/194 Temperature_Celsius/ {print $10}' | head -1)
            if [[ "$temp" =~ ^[0-9]+$ ]] && [ "$temp" -gt "$HDD_TEMP" ]; then
                HDD_TEMP=$temp
            fi
        fi
    done
    echo "$HDD_TEMP"
}

fan_curve() {
    local min=$1 actual=$2 max=$3
    awk -v min="$min" -v actual="$actual" -v max="$max" '
    BEGIN {
        if (actual <= min) ratio = 0
        else if (actual >= max) ratio = 1
        else ratio = (actual - min) / (max - min)
        if (ratio < 0) ratio = 0
        if (ratio > 1) ratio = 1
        printf "%d", ratio * 255
    }'
}

set_pwm() {
    local target=$1
    local fan_dir="/sys/class/hwmon/hwmon0"
    local found=0
    local readback=""
    for fan in pwm1 pwm2 pwm3 pwm4; do
        local fan_file="${fan_dir}/${fan}"
        if [ -e "$fan_file" ]; then
            found=1
            if ! echo "$target" > "$fan_file" 2>/dev/null; then
                log_echo "ERROR: write to $fan_file failed (permissions? read-only sysfs?)"
                continue
            fi
            # Read back to confirm the value stuck
            readback=$(cat "$fan_file" 2>/dev/null || echo "?")
            log_echo "$fan_file: wrote $target, reads $readback"
        fi
    done
    if [ "$found" = "0" ]; then
        log_echo "ERROR: no fan devices found at ${fan_dir}/pwm*"
        return 1
    fi
}

apply_once() {
    local mode
    mode=$(read_mode)
    log_echo "Mode: $mode"

    case "$mode" in
        quiet)
            set_pwm "$QUIET_PWM"
            ;;
        cooling)
            set_pwm "$COOLING_PWM"
            ;;
        balance)
            local cpu_t hdd_t hdd_fan cpu_fan fan_speed
            cpu_t=$(get_max_cpu_temp)
            hdd_t=$(get_max_hdd_temp)
            log_echo "CPU=${cpu_t}°C  HDD=${hdd_t}°C"
            cpu_fan=$(fan_curve "$CPU_TGT" "$cpu_t" "$CPU_MAX")
            hdd_fan=$(fan_curve "$HDD_TGT" "$hdd_t" "$HDD_MAX")
            fan_speed=$(( cpu_fan > hdd_fan ? cpu_fan : hdd_fan ))
            if [ "$fan_speed" -lt "$BALANCE_MIN_PWM" ]; then
                fan_speed=$BALANCE_MIN_PWM
            fi
            set_pwm "$fan_speed"
            ;;
        default|"")
            # Hand control back to UniFi's native fan controller. We achieve
            # this by writing PWM_AUTO_CONTROL_ENABLE to 1 if present, or just
            # by not writing PWM (the firmware controller will reassert).
            local fan_dir="/sys/class/hwmon/hwmon0"
            local restored=0
            for ctl in pwm1_enable pwm2_enable pwm3_enable pwm4_enable; do
                if [ -e "${fan_dir}/${ctl}" ]; then
                    echo 2 > "${fan_dir}/${ctl}" 2>/dev/null || true
                    restored=1
                fi
            done
            log_echo "Mode default — handed control back to firmware (restored=$restored)"
            ;;
        *)
            echo "WARNING: unknown mode '$mode', leaving fans alone" >&2
            ;;
    esac
}

if $SERVICE; then
    while true; do
        apply_once || true
        sleep 60
    done
else
    apply_once
fi
