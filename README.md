# Traefik-Clouflare-Tunnel-Auto


Automatically sync your **Traefik HTTP routers** to **Cloudflare Zero Trust Tunnel** and **DNS records**. This service reads router rules from Traefik, updates the tunnel ingress, and reconciles DNS so your domains resolve correctly.

> **Rule of thumb**
>
> * Routers on normal entrypoints → **CNAME** to `<TUNNEL_ID>.cfargotunnel.com` with `proxied: true`.
> * Routers on `local` or `office` entrypoints → **A** record to your **local IP** with `proxied: false` (no tunnel).

---

## Features

* Pull routers from **Traefik API** and detect domain hosts from `Host(...)` rules.
* Update **Cloudflare Zero Trust Tunnel** ingress (skip local/office domains).
* Reconcile **DNS records** per-domain (CNAME vs A) with idempotent updates.
* Exponential backoff on transient failures.
* Opt-in TLS router skipping (`SKIP_TLS_ROUTES`).

---

## Requirements

* Traefik with API/dashboard enabled (read-only is fine): typically `:8080`.
* A Cloudflare **API Token** with permissions: `Zone:Read`, `DNS:Read`, `DNS:Edit`, `Account:Cloudflare Tunnel:Read`, `Account:Cloudflare Tunnel:Edit`.
* An existing **Cloudflare Tunnel** (created via `cloudflared` or dashboard) and its **Tunnel ID**.

> **Note**: The service does **not** create tunnels; it only updates the configuration/ingress for an existing one and manages DNS records.

---

## Environment Variables (`.env`)

Create a `.env` file in the project root:

```env
# Cloudflare
CLOUDFLARE_API_TOKEN=your_cf_api_token
CLOUDFLARE_TUNNEL_ID=your_tunnel_id
# Optional (auto-resolved if omitted)
CLOUDFLARE_ACCOUNT_ID=

# Traefik API (http://<traefik-host>:8080)
TRAEFIK_API_ENDPOINT=http://traefik:8080
# Comma-separated entrypoints to monitor (must include the ones you actually use)
TRAEFIK_ENTRYPOINTS=web,websecure,local,office

# Where your Traefik (or upstream) is reachable **inside** your network.
# The host/IP portion will be used for A records on local/office domains.
TRAEFIK_SERVICE_ENDPOINT=http://192.168.1.10:8080

# General behavior
POLL_INTERVAL=10
SKIP_TLS_ROUTES=true
```

**Important**

* `TRAEFIK_SERVICE_ENDPOINT` should point to your internal edge (e.g., Traefik) and **must resolve to an IP** for local/office A records. The script extracts the host part automatically.
* Domains routed on entrypoints named exactly `local` or `office` are **excluded** from tunnel ingress and forced to A records with `proxied=false`.

---

## Quick Start with Docker Compose

### Option A — Build locally

`docker-compose.yml`:

```yaml
a version: '3.8'
services:
  traefik-cloudflare-tunnel-auto:
    build: .
    container_name: traefik-cloudflare-tunnel-auto
    restart: unless-stopped
    env_file: .env
    # If your code is in ./app and entry is sync.py, mount it (optional for dev):
    # volumes:
    #   - ./app:/app
    command: ["python", "sync.py"]
```

Then:

```bash
docker compose up -d
```

### Option B — Use a prebuilt image

```yaml
version: '3.8'
services:
  traefik-cloudflare-tunnel-auto:
    image: haemeto/traefik-cloudflare-tunnel-auto:latest
    container_name: traefik-cloudflare-tunnel-auto
    restart: unless-stopped
    env_file: .env
```

Start:

```bash
docker compose up -d
```

---

## Example Dockerfile

```dockerfile
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
 
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD ["python", "main.py"]

```

`requirements.txt` (example):

```
cloudflare>=3.0.0
python-dotenv>=1.0.1
requests>=2.32.0
httpx>=0.27.0
```

---

## How It Works

1. Poll Traefik API (`/api/http/routers`) at `POLL_INTERVAL`.
2. Parse routers with `Host(...)` rules and entrypoints.
3. Build/Update Zero Trust Tunnel *ingress* for **non-local/office** domains only.
4. Reconcile DNS per domain:

   * local/office → `A` → `<TRAEFIK_SERVICE_ENDPOINT host>` (`proxied=false`).
   * others → `CNAME` → `<TUNNEL_ID>.cfargotunnel.com` (`proxied=true`).
5. Repeat with jittered sleep to avoid thundering herd.

---

## Verifying

* **Tunnel ingress**: In Cloudflare Zero Trust → Networks → Tunnels → your tunnel → Configuration. You should see hostnames **excluding** local/office domains.
* **DNS**: In Cloudflare DNS → verify each domain record type/target and proxied flag.
* **Logs**: `docker compose logs -f traefik-cf-sync`.

---

## Troubleshooting

* **No routers found**

  * Ensure `TRAEFIK_API_ENDPOINT` is correct and reachable from the container.
  * Traefik API must be enabled. Default is `:8080`.
* **Wrong DNS record type**

  * Check the router’s entrypoints. Only `local`/`office` trigger A records.
  * Confirm `.env` value for `TRAEFIK_ENTRYPOINTS` includes the entrypoints you actually use.
* **A record uses a hostname**

  * `TRAEFIK_SERVICE_ENDPOINT` should resolve to an **IP**. The script extracts the host and uses it as-is.
* **Auth/permission errors**

  * Confirm API token scopes: `Zone:Read`, `DNS:Read`, `DNS:Edit`, `Account:Cloudflare Tunnel:Read/Edit`.
* **TLS routers skipped**

  * Set `SKIP_TLS_ROUTES=false` to include TLS-enabled routers in processing (default skips them).

---

## Security Notes

* Store tokens in `.env` and never commit `.env` to VCS.
* Use least-privilege Cloudflare tokens.
* Restrict Traefik API exposure to your private network.

---

## License

MIT

---

## Disclaimer

This tool updates existing Cloudflare Tunnel configuration and DNS records based on your Traefik routers. Always review logs and Cloudflare changes in lower environments before production rollout.
