# RouteShield — Vultr Ubuntu VM Deployment Checklist

Operator-facing checklist for the public hackathon demonstration.
Detailed explanations live in [`DEPLOYMENT.md`](DEPLOYMENT.md); this file is the
run-order with a verification gate after every step. **Do not skip a gate.**

Legend: `[ ]` action · `✓ gate` = command/observation that must succeed before continuing.

---

## 0. Prerequisites (before touching the VM)

- [ ] Repository cloned locally and on the **release commit** (`git log -1`)
- [ ] Local verification green: backend tests **114 passed**, benchmark **40/40**
      deterministic, `npm ci && npm run build` clean
- [ ] A domain (or subdomain) you control, pointed at the VM IP — *optional but
      required for HTTPS*
- ✓ gate: `git status --porcelain` is empty (no uncommitted changes, no stray files)

## 1. Provision the VM

- [ ] Vultr → Deploy Server → **Cloud Compute**, Ubuntu **24.04 LTS x64**,
      ≥ 2 vCPU / 4 GB RAM (minimum 1 vCPU / 2 GB), SSH key added
- [ ] Vultr Firewall attached to the server:
      | Port | Policy |
      |---|---|
      | 22 (SSH) | allow, ideally restricted to your IP |
      | 80 / 443 | allow (host nginx → TLS) |
      | **8000** | **never opened** (backend is container-internal only) |
      | 8080 | **not opened** if step 7 (host nginx) is used; open only for a raw-IP demo |
- ✓ gate: `ssh root@<VM_IP>` works; `nmap -Pn -p 22,80,443,8000,8080 <VM_IP>` (from
  your laptop) shows 8000 closed/filtered

## 2. Install Docker

- [ ] `curl -fsSL https://get.docker.com | sh && systemctl enable --now docker`
- ✓ gate: `docker --version && docker compose version` both print versions

## 3. Get the code onto the VM

- [ ] `cd /opt && git clone <your-repo-url> routeshield && cd routeshield`
      (or `scp -r` the working tree — **never** scp `.env`)
- ✓ gate: `ls docker-compose.yml backend/Dockerfile frontend/Dockerfile
  backend/data/campaigns/26V111.reviewed.json
  backend/tests/fixtures/RCLRPT-26V111-4068.pdf` — all five exist

## 4. Configure secrets (never in images, source, or shell history)

- [ ] `cp .env.example .env` and fill in:
      ```
      ENVIRONMENT=production
      ROUTESHIELD_MANAGER_USERNAME=fleet.manager
      ROUTESHIELD_MANAGER_PASSWORD=<openssl rand -base64 24>
      ROUTESHIELD_AUTH_SECRET=<openssl rand -hex 32>
      HTTP_PORT=8080
      CORS_ORIGINS=https://demo.yourdomain.com        # only if serving cross-origin
      # optional LLM-assisted extraction:
      VULTR_INFERENCE_API_KEY=<your key>
      VULTR_INFERENCE_MODEL=<your deployed model>
      ```
- [ ] Confirm `.env` is untracked: `git status --porcelain .env` → empty output
- ✓ gate: `docker compose config >/dev/null` exits 0 **without** the
  "set ROUTESHIELD_MANAGER_PASSWORD in .env" error (the `:?` guards prove both
  secrets are present). Never paste the values into chat/tickets.

## 5. Build and launch

- [ ] `docker compose up -d --build`
- ✓ gate: `docker compose ps` → `backend` **healthy**, `frontend` **healthy**
  (frontend waits for `service_healthy` before starting)
- ✓ gate: `docker compose logs backend | grep -i "startup recall refresh"` shows
  the live-NHTSA attempt (success or honest fallback — both acceptable)

## 6. Verify the running application

- [ ] `curl -s http://127.0.0.1:8080/healthz` → `ok` (frontend nginx)
- [ ] `curl -s http://127.0.0.1:8080/api/health` → `"status":"ok"` (backend via proxy)
- [ ] `curl -s -o /dev/null -w '%{http_code}\n' http://<VM_IP>:8000/api/health`
      from **outside** the VM → connection refused/timeout (backend not published)
- [ ] Browser `http://<VM_IP>:8080`:
      - amber **SYNTHETIC DEMONSTRATION DATA** banner visible
      - evidence banner says “Live evidence…” **or** “Using manually reviewed
        campaign evidence.” — the UI must never pretend which occurred
      - manager login works with the `.env` credentials
- [ ] Full demo rehearsal: set VERIFICATION_REQUIRED policy → generate plan →
      **APPROVE RECOVERY PLAN** → routes reassigned → Evidence & Audit view shows
      append-only events → benchmark strip shows 40 samples / 0% false-confirmation
- [ ] Persistence: `docker compose down && docker compose up -d` → incident state,
      audit log and approvals survive (named volume `routeshield-data`)
- ✓ gate: all of the above pass. Any failure ⇒ fix before demo day; do not proceed.

## 7. HTTPS (required for a public URL)

- [ ] `apt install -y nginx certbot python3-certbot-nginx`
- [ ] Host nginx site proxying `demo.yourdomain.com` → `http://127.0.0.1:8080`
      (full config in DEPLOYMENT.md §7), then `nginx -t && systemctl reload nginx`
- [ ] `certbot --nginx -d demo.yourdomain.com`
- [ ] Close 8080 in the Vultr firewall (traffic now flows 443 → host nginx → 127.0.0.1:8080)
- ✓ gate: `https://demo.yourdomain.com` loads with a valid lock; HTTP redirects to
  HTTPS; backend already trusts proxy headers (`--proxy-headers`)

## 8. Demo-day operations

- [ ] Backup before the event:
      `docker run --rm -v routeshield_routeshield-data:/data -v $(pwd):/backup alpine tar czf /backup/data-backup.tgz -C /data .`
- [ ] Watch logs in a spare terminal: `docker compose logs -f backend`
- [ ] Factory reset (if a rehearsal corrupts demo state):
      `docker compose down && docker volume rm routeshield_routeshield-data && docker compose up -d --build`
      — seeding is idempotent and restores the 15-vehicle fleet, 6 routes and the
      26V111 incident automatically
- ✓ gate: reset rehearsal performed once end-to-end **before** the demo

---

## Known environment limitation (honest status)

Docker image build and `docker compose up` could **not** be executed in the
development sandbox (no Docker daemon). Everything statically verifiable was
verified there: compose YAML structure, `:?` secret guards, backend
non-exposure, volume persistence, healthchecks, Dockerfile context inputs, and
the production frontend build (`npm ci`, `tsc -b`, `vite build`, served and
exercised against the live API). Steps 5–6 above are the first runtime
execution of the containers — budget 15 minutes for them and treat their gates
as mandatory.
