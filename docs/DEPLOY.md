# Deploying the bcrc platform

Production: Dokploy VPS, Cloudflare-proxied `bcrc.rrayes.com.br` (TLS at the
CF edge, plain HTTP to the origin), Dokploy-managed Postgres, Resend email,
Stripe billing. Operations after deploy: [RUNBOOK.md](RUNBOOK.md).

## Environment variables

| Variable | Required (prod) | Value / notes |
| --- | --- | --- |
| `PLATFORM_ENV` | baked into image | `production` — refuses boot if DB/base-URL/email are missing or still dev-mode |
| `PLATFORM_DATABASE_URL` | yes | `postgresql://bcrc:<pw>@<internal-host>:5432/bcrc` (Dokploy internal hostname) |
| `PLATFORM_BASE_URL` | yes | `https://bcrc.rrayes.com.br` — used in magic-link URLs |
| `PLATFORM_EMAIL_MODE` | yes | `resend` (`dev` only logs links; prod refuses it) |
| `RESEND_API_KEY` | yes | from resend.com; boot fails fast if missing |
| `PLATFORM_EMAIL_FROM` | yes | `Better Claude RC <login@bcrc.rrayes.com.br>` — domain must be DKIM-verified in Resend |
| `STRIPE_SECRET_KEY` | yes | test mode at launch (`sk_test_…`) |
| `STRIPE_PRICE_ID` | yes | $5/mo price (`price_…`) |
| `STRIPE_WEBHOOK_SECRET` | yes | from the webhook endpoint (`whsec_…`) |
| `PLATFORM_METRICS_TOKEN` | recommended | gates `/metrics` behind `Authorization: Bearer` |
| `PLATFORM_AUTO_MIGRATE` | no | default applies `platform/migrations/*.sql` at boot; `0` to skip |
| `PLATFORM_FAKE_BILLING` / `BCRC_RELAY_TAP` | **never in prod** | test seams |

## Dokploy setup (once)

1. Project `better-claude-rc`; add a **Postgres 17** service (db/user `bcrc`,
   generated password).
2. Application `bcrc-platform`:
   - Source: GitHub `rafa-rrayes/better-claude-rc`, branch `main`,
     auto-deploy on push.
   - Build: type **Dockerfile**, path `platform/Dockerfile`, context `.`.
   - **Update strategy: stop-then-start.** Never rolling: the relay Router
     is in-memory; two live containers = phones and agents split across
     processes that can't see each other.
   - Env: the table above.
   - Domain: `bcrc.rrayes.com.br` → container port 8500, HTTPS **off** at
     the origin (Cloudflare terminates TLS).
3. Nightly backup Schedule on the postgres service → RUNBOOK §Backups.

The container runs uvicorn with `--proxy-headers --forwarded-allow-ips "*"`
(Traefik is the only way in) and a 10s graceful shutdown; SIGTERM drains
every relay leg with close 1012 so clients reconnect instantly.

## Cloudflare

- Proxied A record `bcrc` → the VPS IP (orange cloud — also hides the origin).
- SSL mode "Flexible" or "Full" to match the other apps on this box.
- **Rocket Loader OFF** for this zone/host (it rewrites `<script>` tags; the
  CSP forbids the injected inline shim and the PWA breaks).
- Cache rule: bypass cache for `bcrc.rrayes.com.br/*`. The app also sends
  `Cache-Control: no-cache` on non-API paths as belt-and-suspenders.
- WebSockets: on by default; no config needed for `/agent/ws` + `/client/ws`.

## Resend

1. Create the API key.
2. Verify the sending domain (add Resend's DKIM/SPF records in Cloudflare
   DNS — DNS-only, not proxied).
3. `PLATFORM_EMAIL_FROM` must use that domain, e.g.
   `Better Claude RC <login@bcrc.rrayes.com.br>`.
4. Failures show up as `bcrc_emails_sent_total{status="error"}` and
   `resend refused` log lines; the API response to users stays a generic
   200 (no enumeration).

## Stripe (test mode at launch)

1. Product "Better Claude RC Pro", recurring $5/month → `STRIPE_PRICE_ID`.
2. After the domain is live: Developers → Webhooks → endpoint
   `https://bcrc.rrayes.com.br/api/stripe/webhook` with events
   `checkout.session.completed`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `invoice.payment_failed`
   → `STRIPE_WEBHOOK_SECRET`.
3. Verify with a test-card checkout: plan flips to `pro` on
   `/api/me`, webhook idempotency visible in app logs (duplicate deliveries
   log as already-processed).

## Releasing the agent package

The platform **self-hosts** the agent wheel: `platform/Dockerfile` runs
`uv build --package better-claude-rc --wheel` and the app serves it at
`/dist/<wheel>` for the `curl -fsSL <base>/i/<token> | bash` installer
(GET `/i/{token}` in `routes_machines.py`). A deploy is therefore also a
release — no PyPI step is required.

For a source checkout (dev), build the wheel once so `/i/` works locally:

```sh
rm -rf dist
uv build --package better-claude-rc --wheel   # never bare `uv build`
uvx --from dist/better_claude_rc-*.whl better-claude-rc --help   # sanity
```

The repo is a uv workspace with a **private** platform member. Never publish
or serve `bcrc-platform` (it carries `Private :: Do Not Upload`; the `/dist`
route only ever serves the agent wheel). Publishing to PyPI
(`uv publish --token "$PYPI_TOKEN"`) is optional and unrelated to enrollment.

## Local production-image verification

```sh
# 1. throwaway postgres (prints its DSN)
uv run python scripts/pgtmp.py

# 2. build + run the real image, overriding the prod guard for local email
docker build -f platform/Dockerfile -t bcrc-platform .
docker run --rm -p 8500:8500 \
  -e PLATFORM_ENV=dev \
  -e PLATFORM_DATABASE_URL="<dsn from pgtmp, host.docker.internal>" \
  -e PLATFORM_BASE_URL=http://localhost:8500 \
  bcrc-platform

# 3. smoke it
uv run python scripts/smoke_platform.py http://localhost:8500

# 4. SIGTERM drain drill: with a client connected, `docker kill -s TERM …`
#    → client sees close 1012, container exits < 10s, log shows the drain.
```
