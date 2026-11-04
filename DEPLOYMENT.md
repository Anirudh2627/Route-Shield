# Deploying RouteShield on a Vultr Cloud Compute VM

Target: one VM, Docker Compose, nginx reverse proxy, persistent SQLite volume, health checks.

> **Status disclosure:** the Docker assets in this repository were written and
> configuration-verified, but `docker compose up` was **not executed** during development
> (no Docker daemon available in the build sandbox). Follow the verification checklist at the
> bottom on first launch. The application itself (backend + frontend + full demo flow) was
> verified running directly on the host.

---

## 1. Provision the VM

Vultr console → **Deploy Server**:

| Setting | Recommendation |
|---|---|
| Type | Cloud Compute (Regular or High Frequency) |
| Plan | 2 vCPU / 4 GB RAM / 80 GB NVMe (minimum 1 vCPU / 2 GB) |
| OS | Ubuntu 24.04 LTS x64 |
| Location | your region |
| SSH key | add your public key |

Firewall: allow inbound **22** (SSH), **80**, **443**. The app itself listens on **8080**
(host) and is proxied by the system nginx below; do not open 8080 publicly if you use nginx.

## 2. Install Docker

```bash
ssh root@<VM_IP>
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker --version && docker compose version
```

## 3. Get the code

```bash
cd /opt
git clone <your-repo-url> routeshield    # or: scp -r ./routeshield root@<VM_IP>:/opt/
cd routeshield
```

## 4. Configure secrets (never in images or source)

```bash
cp .env.example .env
nano .env
```

Required for production:

```dotenv
ENVIRONMENT=production
ROUTESHIELD_MANAGER_USERNAME=fleet.manager
ROUTESHIELD_MANAGER_PASSWORD=<strong-random-password>       # generate: openssl rand -base64 24
ROUTESHIELD_AUTH_SECRET=<strong-random-secret>              # generate: openssl rand -hex 32
HTTP_PORT=8080
AUTO_REFRESH_RECALL=true
ALLOW_OFFLINE_FALLBACK=true
# Optional LLM-assisted extraction:
VULTR_INFERENCE_API_KEY=<your key>
VULTR_INFERENCE_BASE_URL=https://api.vultrinference.com/v1
VULTR_INFERENCE_MODEL=<your deployed model>
```

Generate secrets:

```bash
openssl rand -base64 24   # manager password
openssl rand -hex 32      # auth secret
```

`docker compose` refuses to start if `ROUTESHIELD_MANAGER_PASSWORD` or
`ROUTESHIELD_AUTH_SECRET` is missing (`:?` interpolation guards in the compose file).

## 5. Build and launch

```bash
docker compose up -d --build
docker compose ps          # both services should reach "healthy"
```

What happens on first boot (automatic):

1. `backend` creates `/data/routeshield.db` inside the **named volume**
   `routeshield-data` (survives rebuilds; back it up with
   `docker run --rm -v routeshield_routeshield-data:/data -v $(pwd):/backup alpine tar czf /backup/data-backup.tgz -C /data .`).
2. Tables are created and seeding runs idempotently: synthetic fleet (15 vehicles),
   6 routes, the 26V111 incident with the **manually reviewed fixture** evidence and an
   initial deterministic assessment. Re-running never duplicates records.
3. A background thread attempts the **live NHTSA refresh** (campaign API + official Part 573
   PDF). Success ⇒ banner switches to “Live evidence”; failure ⇒ the app keeps running on the
   fixture and says so explicitly.

## 6. Verify

```bash
curl -s http://127.0.0.1:8080/healthz            # -> ok          (frontend nginx)
curl -s http://127.0.0.1:8080/api/health | jq    # -> status ok   (backend via proxy)
docker compose logs backend | grep -i "startup recall refresh"
```

Open `http://<VM_IP>:8080` — you should see the amber synthetic-data banner and the
Incident Command screen. Sign in with the manager credentials from `.env`.

## 7. TLS + domain (recommended for public access)

```bash
apt install -y nginx certbot python3-certbot-nginx
```

`/etc/nginx/sites-available/routeshield`:

```nginx
server {
    server_name demo.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/routeshield /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d demo.yourdomain.com     # free Let's Encrypt TLS
```

Then browse `https://demo.yourdomain.com`. Backend already runs with
`--proxy-headers --forwarded-allow-ips *`.

## 8. Database initialization / migration notes

* **Initialization is automatic and idempotent** at container start (`init_db()` +
  `seed_all()`); no manual migration step is required for the MVP.
* To start completely fresh: `docker compose down && docker volume rm routeshield_routeshield-data && docker compose up -d --build`.
* **PostgreSQL swap** (no code changes): set
  `DATABASE_URL=postgresql+psycopg://user:pass@db-host:5432/routeshield`, add `psycopg[binary]`
  to `backend/requirements.txt`, and (recommended) run Alembic migrations for schema evolution.
  The ORM uses only portable types (`JSON`, `DateTime(timezone=True)`, `String`, `Integer`,
  `Boolean`, `Date`, `Float`, `Text`).

## 9. Operations

```bash
docker compose logs -f backend        # live logs (audit, extraction, refresh)
docker compose restart backend        # restart a single service
docker compose down                   # stop (data volume persists)
docker compose up -d --build          # deploy a new version
```

Health endpoints:

* Backend: `GET /api/health` → `{status, database, llm_configured, ...}` (container
  HEALTHCHECK hits it every 30 s).
* Frontend: `GET /healthz` → `ok` (nginx container HEALTHCHECK).

## First-launch verification checklist

- [ ] `docker compose ps` → both services `healthy`
- [ ] `curl http://127.0.0.1:8080/api/health` → `"status":"ok"`
- [ ] UI shows the amber **SYNTHETIC DEMONSTRATION DATA** banner
- [ ] Evidence banner shows either “Live evidence…” (NHTSA reachable) or
      “Using manually reviewed campaign evidence.” (offline fallback) — both are valid;
      the UI never pretends which one occurred
- [ ] Manager login works with the `.env` credentials
- [ ] Full demo: policy → generate plan → **APPROVE RECOVERY PLAN** → audit events appear
- [ ] `docker compose down && docker compose up -d` → incident/audit state persisted (volume)
