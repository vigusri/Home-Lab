# 4. Service Monitoring, Benchmarks & Automation

Running services continuously on older laptop hardware requires active visibility into **thermals, memory consumption, container health, and network latency**.

---

## 1. Service & Hardware Monitoring

* **Real-Time Web Dashboard (`localhost:8088`):**
  * Lightweight Python monitoring service (`/opt/aurabase-dashboard/server.py`) exposed privately over the Cloudflare Tunnel.
  * Displays real-time hardware sensors, thermal readings, memory budget, and live Docker container states (`multi-channel-extractor`, `n8n`).
* **CLI Health & Log Verification:**
  * [`scripts/health-check.sh`](../scripts/health-check.sh) automates routine checks across network routing, DNS, `systemd` units (`cloudflared`, `n8n.service`, `docker`), container status, and hardware sensors.
  * Service logs audited via `journalctl -u cloudflared -n 50` and `docker compose logs --tail=50`.

---

## 2. Verified Performance & Thermal Baselines

### Local Web Stack Load Test (`ab -n 500 -c 10`)
Tested against the containerized Vite + Nginx (`:3000`) and FastAPI (`:8000`) application stack:

* **Throughput:** **`2,081.46` requests/sec**
* **Reliability:** **`0` failed requests** out of 500 concurrent requests
* **Mean Batch Time:** **`4.8 ms`**
* **Time To First Byte (TTFB):**
  * Frontend (Vite + Nginx): **`1.1 ms`**
  * Backend (FastAPI): **`2.9 ms`**

### End-to-End Public Tunnel Latency (`curl`)
* **Cold Public Fetch via Cloudflare Tunnel:** **`~637 ms`** total (of which **`~91 ms`** is internal server stack processing; remainder is TLS + Cloudflare Edge round-trip).

### Thermal Baseline (`lm-sensors`)
* **Steady-State Temperatures:** **`55.0°C`** across CPU (`k10temp`) and GPU (`amdgpu`), safely below the `70°C` high-temperature threshold for 24/7 headless operation.

---

## 3. Programmatic Automation Client (MCP over HTTPS)

To inspect and trigger workflows from a workstation without opening the full web UI, [`scripts/n8n_mcp_client.py`](../scripts/n8n_mcp_client.py) communicates with n8n's Streamable HTTP JSON-RPC 2.0 MCP endpoint (`/mcp-server/http`) using Bearer token authentication read from environment variables:

```bash
export N8N_MCP_URL="https://n8n.yourdomain.com/mcp-server/http"
export N8N_BEARER_TOKEN="your_token_here"

# List workflows
python3 scripts/n8n_mcp_client.py

# Search workflows or node catalog
python3 scripts/n8n_mcp_client.py search --query webhook
python3 scripts/n8n_mcp_client.py nodes slack

# Inspect or run a workflow
python3 scripts/n8n_mcp_client.py get <workflow-id>
python3 scripts/n8n_mcp_client.py run <workflow-id> --chat "Health check"
```
