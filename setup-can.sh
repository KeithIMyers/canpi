#!/bin/bash
# setup-can.sh — Initialize CAN interfaces on the Raspberry Pi host.
# Run this BEFORE starting the Docker container.
# Typically called from a systemd service or /etc/rc.local.

set -e

echo "[CanPi] Setting up CAN interfaces..."

# Bring up real CAN interfaces.
# can0: 500 kbps (automotive/OBD default)
# can1: 250 kbps — the connected machine speaks J1939, which runs at 250k.
#       (At 500k this bus produced only error frames and zero valid packets.)
declare -A BITRATES=( [can0]=500000 [can1]=250000 )
for iface in can0 can1; do
    if ip link show "$iface" &>/dev/null; then
        rate="${BITRATES[$iface]}"
        ip link set "$iface" down 2>/dev/null || true
        ip link set dev "$iface" type can listen-only off 2>/dev/null || true
        ip link set dev "$iface" type can fd off 2>/dev/null || true
        ip link set "$iface" type can bitrate "$rate" fd off restart-ms 1000 berr-reporting on
        ip link set "$iface" up
        ip link set "$iface" txqueuelen 65536 2>/dev/null || true
        echo "[CanPi] $iface UP at classic $((rate / 1000))kbps"
    else
        echo "[CanPi] $iface not found (shield not connected?)"
    fi
done

# Create virtual CAN interface for testing
if ! ip link show can9 &>/dev/null; then
    modprobe vcan 2>/dev/null || true
    ip link add dev can9 type vcan
fi
ip link set can9 up
echo "[CanPi] can9 (virtual) UP"

echo "[CanPi] CAN setup complete."
