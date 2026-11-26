# 1. System & Hardware Tuning (Debian 13 on Lenovo E41)

Running a 24/7 headless server on repurposed consumer laptop hardware requires deliberate tuning around **thermal longevity, slow disk I/O, limited memory, and battery safety**.

---

## Host Baseline
* **Hardware:** Lenovo E41 laptop (`4 GB` RAM total / `3.19 GB` available at workload baseline)
* **Storage:** `210 GB` free disk space, measured throughput **`32.7 MB/s`**
* **Battery State:** Degraded (`12,030 mWh` / `48,000 mWh` = **`25.1%`** design capacity)
* **Operating System:** **Debian 13 (Trixie)** headless server (`serveradmin` user, timezone `Asia/Kolkata`)

---

## 1. Battery Conservation via Kernel ACPI (`tlp`)

Keeping a degraded Li-ion laptop battery continuously trickle-charging at 100% creates unnecessary heat and swelling risk.

* **Packages installed:** `tlp` and `tlp-rdw`
* **Configuration ([`configs/tlp/tlp-conservation.conf`](../configs/tlp/tlp-conservation.conf)):**
  ```ini
  STOP_CHARGE_THRESH_BAT0=1
  ```
* **Verification on Hardware (`ideapad_acpi` driver):**
  * `/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00/conservation_mode` → **`1` (on)**
  * `BAT1 power1` → **`0.00 W`** (confirms pure AC wall-power bypass without charging the battery)

---

## 2. Disk I/O & Virtual Memory Tuning (`sysctl`)

Because the drive's sustained throughput is **`32.7 MB/s`**, large default Linux dirty-page writeback bursts can block interactive container requests and SSH sessions.

* **Configuration ([`configs/sysctl/99-homelab-tuning.conf`](../configs/sysctl/99-homelab-tuning.conf)):**
  ```ini
  # Flush dirty pages earlier in smaller batches to prevent I/O stalls
  vm.dirty_background_ratio = 5
  vm.dirty_ratio = 10

  # Prefer keeping active service memory in RAM over aggressive swapping
  vm.swappiness = 10
  ```
* **Effect:** Prevents sudden multi-hundred-megabyte write stalls on the slow drive and keeps containerized APIs responsive.

---

## 3. Low-Memory Container Architecture (`< 4 GB RAM`)

To run multiple web services + workflow automation without hitting Out-Of-Memory (OOM) kills:
1. **Embedded SQLite over External PostgreSQL:** Running n8n with embedded SQLite instead of a standalone PostgreSQL container saves **~150–250 MB RAM** and eliminates continuous background WAL/checkpoint disk writes.
2. **Explicit Node.js Heap Cap:** Set `NODE_OPTIONS=--max-old-space-size=512` in [`configs/docker/n8n-compose.yml`](../configs/docker/n8n-compose.yml) so the automation runtime stays strictly under 512 MB.
3. **Automatic Log & Execution Pruning:** Enabled `EXECUTIONS_DATA_PRUNE=true`, `EXECUTIONS_DATA_MAX_AGE=168` (7 days), and `EXECUTIONS_DATA_PRUNE_MAX_COUNT=50000` to keep SQLite database size bounded on disk.
