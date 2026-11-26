# 3. Network & DNS Troubleshooting Case Study (Post-Reboot Error 1033)

This incident report documents a real outage investigated and resolved on the Debian 13 homeserver, covering **router DHCP logs, Linux network manager conflicts, IP/subnet mismatches, and DNS resolution failures**.

---

## 1. Incident Symptoms

After rebooting the headless Lenovo E41 server:
1. External requests to the hosted web endpoints returned **Cloudflare Error 1033** (Argo Tunnel unreachable / HTTP `530`).
2. Diagnostic checks run directly on the server showed a confusing split state:
   * `ping 192.168.1.1` (local router gateway) → **`0% packet loss`** (local link alive)
   * `ping 1.1.1.1` (external IP) → **`100% packet loss`** (no outbound WAN routing)
   * `ping google.com` → **`Temporary failure in name resolution`** (broken DNS)

---

## 2. Investigation & Evidence Gathering

Using core Linux CLI networking tools alongside router DHCP status/logs:

```bash
ip a show wlp2s0          # Inspected assigned interface addresses
ip route                  # Checked default gateway & metric conflicts
cat /etc/network/interfaces
nmcli device status       # Checked NetworkManager state vs ifupdown
cat /etc/resolv.conf      # Verified active DNS nameservers
```

### Root Causes Identified (3 Compounding Issues)

| Layer | Misconfiguration Observed | Impact |
| :--- | :--- | :--- |
| **1. Dual Network Manager Conflict** | Both legacy `ifupdown` (`/etc/network/interfaces`) and `NetworkManager` were trying to control the Wi-Fi interface (`wlp2s0`) simultaneously. | Race conditions on boot left routing and interface state inconsistent. |
| **2. Subnet & IP Collision** | `/etc/network/interfaces` contained legacy `192.168.2.x` static configurations (and later a static `192.168.1.44`), whereas the active router operates on `192.168.1.1` and assigns DHCP leases on `192.168.1.x`. | Default route / outbound WAN traffic broke or clashed with DHCP leases tracked by the router. |
| **3. Missing DNS Resolver** | Without `resolvconf` or `NetworkManager` DHCP managing the interface cleanly, `/etc/resolv.conf` had no valid upstream nameservers. | `cloudflared` and system packages failed all hostname lookups (`Temporary failure in name resolution`). |

---

## 3. Resolution & Final Network Architecture

1. **Eliminated the `ifupdown` Conflict:** Removed `wlp2s0` and all static IP definitions from `/etc/network/interfaces`, leaving only the loopback interface ([`configs/network/interfaces.clean`](../configs/network/interfaces.clean)):
   ```text
   auto lo
   iface lo inet loopback
   ```
2. **Standardized on `NetworkManager` + Router DHCP:**
   * Allowed `NetworkManager` sole ownership of `wlp2s0` with `connection.autoconnect yes` so Wi-Fi connects automatically during early boot (before user login).
   * Router (`192.168.1.1`) dynamically provisions the IP address, default gateway, and working DNS nameservers into `/etc/resolv.conf`.

---

## 4. Verification Checklist (Post-Fix)

```bash
# 1. Confirm single clean 192.168.1.x IP and default route via 192.168.1.1
ip a show wlp2s0 && ip route

# 2. Confirm both IP routing and DNS name resolution
ping -c 3 google.com

# 3. Confirm Cloudflare Tunnel reconnected and containers are healthy
sudo systemctl status cloudflared --no-pager
docker ps
```

**Outcome:** Outbound WAN routing and DNS resolution recovered reliably across reboots, restoring `cloudflared` tunnel connectivity and eliminating Error 1033.
