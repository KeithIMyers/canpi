#!/bin/bash
# setup-can.sh — Initialize CAN interfaces on the Raspberry Pi host.
# Run this BEFORE starting the Docker container.
# Typically called from a systemd service or /etc/rc.local.

set -e

echo "[CanPi] Setting up CAN interfaces..."

# Bring up real CAN interfaces (500kbps default)
for iface in can0 can1; do
    if ip link show "$iface" &>/dev/null; then
        ip link set "$iface" down 2>/dev/null || true
        ip link set "$iface" type can bitrate 500000
        ip link set "$iface" up
        echo "[CanPi] $iface UP at 500kbps"
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
