#!/usr/bin/env bash
# Headless Debian Home-Lab Verification Script
# Checks Wi-Fi interface IP/route, DNS resolution, systemd services, Docker, battery conservation, and thermals.

set -u

IFACE="${1:-wlp2s0}"

echo "=== 1. Network Interface & Routing ($IFACE) ==="
ip -br a show "$IFACE" 2>/dev/null || echo "Interface $IFACE not found"
ip route | head -n 10

echo ""
echo "=== 2. DNS Configuration & Connectivity ==="
grep -E '^(nameserver|search)' /etc/resolv.conf 2>/dev/null || echo "No nameservers in /etc/resolv.conf"
ping -c 2 -W 3 192.168.1.1 >/dev/null 2>&1 && echo "Gateway 192.168.1.1: OK" || echo "Gateway 192.168.1.1: FAILED"
ping -c 2 -W 3 1.1.1.1 >/dev/null 2>&1 && echo "WAN IP 1.1.1.1: OK" || echo "WAN IP 1.1.1.1: FAILED"
ping -c 2 -W 3 google.com >/dev/null 2>&1 && echo "DNS google.com: OK" || echo "DNS google.com: FAILED"

echo ""
echo "=== 3. Systemd Services ==="
for svc in NetworkManager docker cloudflared n8n.service tlp; do
    state=$(systemctl is-active "$svc" 2>/dev/null || echo "not-found")
    printf "  %-16s %s\n" "$svc" "$state"
done

echo ""
echo "=== 4. Running Containers ==="
if command -v docker >/dev/null 2>&1; then
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
else
    echo "Docker CLI not installed"
fi

echo ""
echo "=== 5. Hardware Conservation & Thermals ==="
CONSERVATION_FILE="/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00/conservation_mode"
if [ -r "$CONSERVATION_FILE" ]; then
    echo "TLP conservation_mode: $(cat "$CONSERVATION_FILE")"
fi
if command -v sensors >/dev/null 2>&1; then
    sensors
else
    echo "lm-sensors not installed"
fi
