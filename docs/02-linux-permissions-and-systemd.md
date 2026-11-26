# 2. Linux File Permissions, Service Isolation & `systemd`

To operate securely on a home network without exposing raw host ports or running applications unnecessarily as `root`, services follow strict **Linux permission, user isolation, and `systemd` lifecycle practices**.

---

## 1. Non-Root User & File Ownership Practices

* **Dedicated Administration Account:** Services and Docker Compose stacks are owned and operated under the non-root user **`serveradmin`** rather than `root`.
* **Container Bind-Mount Ownership (`1000:1000`):**
  * The n8n container runs internally as the unprivileged `node` user (UID/GID `1000:1000`).
  * Host persistent storage (`/home/serveradmin/n8n/data`) is explicitly provisioned with `1000:1000` ownership so SQLite databases, encryption keys, and workflow data remain readable/writable only by the owning service account.
* **Strict Settings File Permissions:**
  * Enabled `N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=true` so the runtime enforces restrictive mode bits (`0600`) on configuration files containing encryption keys or credentials.
* **Tunnel Credential Isolation:**
  * Cloudflare Tunnel credentials (`/etc/cloudflared/*.json` and `/etc/cloudflared/config.yml`) remain protected in root-owned `/etc/cloudflared/`, isolated from application containers.

---

## 2. Attack-Surface Reduction (Loopback Binding + Reverse Proxy)

* **Zero Public Router Port Forwarding:** No ports (`22`, `80`, `443`, `3000`, `5678`, `8000`, `8088`) are opened on the router firewall.
* **Loopback-Only Container Ports:** Services such as n8n bind strictly to `127.0.0.1:5678:5678` instead of `0.0.0.0:5678`, ensuring they are reachable locally only via the host loopback and the authenticated `cloudflared` tunnel.
* **Proxy Header Validation (`N8N_PROXY_HOPS=1`):**
  * Behind Cloudflare Tunnel, incoming requests carry `X-Forwarded-For` headers. Without proxy hop awareness, Express rate-limiters fail with `ERR_ERL_UNEXPECTED_X_FORWARDED_FOR`.
  * Adding `N8N_PROXY_HOPS=1` safely trusts exactly 1 upstream reverse proxy hop (`cloudflared`) without opening header-spoofing exposure.

---

## 3. Boot Persistence & Service Orchestration (`systemd`)

Headless servers must recover cleanly after power outages without manual login:

* **`aurabase-dashboard.service` ([`configs/systemd/aurabase-dashboard.service`](../configs/systemd/aurabase-dashboard.service)):**
  * Runs the Python monitoring daemon under `User=serveradmin`, `Group=serveradmin`, `Nice=10` (low CPU scheduling priority), and `Restart=always`.
* **`n8n.service` ([`configs/systemd/n8n.service`](../configs/systemd/n8n.service)):**
  * Orders startup strictly after `network-online.target` and `docker.service`.
  * Executes `docker compose up -d` / `down` under `User=serveradmin` and `Group=serveradmin`.
* **Container Restart Policies:**
  * Production app containers (`Vite + Nginx`, `FastAPI`) use `restart: unless-stopped`.
  * Automation stack uses `restart: always`.
* **`cloudflared` Daemon:** Managed as a `systemd` service so encrypted outbound tunnel connections re-establish automatically once network connectivity is up.
