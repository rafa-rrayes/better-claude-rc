# Runbook — bcrc platform on Dokploy

Operational procedures for a self-hosted platform at `bcrc.example.com`
(Dokploy VPS, app `bcrc-platform`, Postgres service `bcrc-db`). Deployment
setup lives in [DEPLOY.md](DEPLOY.md).

## Backups

### Nightly dump (Dokploy Schedule)

The backup script is `scripts/pg_backup.sh` in this repo. It runs *inside*
the postgres container: `pg_dump -Fc` into the data volume + N-day rotation.

Set it up once in the Dokploy UI:

1. Postgres service → **Schedules** → Create.
2. Schedule type: run inside the service container; shell `sh`.
3. Cron: `0 6 * * *` (06:00 UTC, a quiet hour).
4. Command: paste the contents of `scripts/pg_backup.sh` (or mount it and run
   `sh /path/pg_backup.sh`).
5. Optional env: `KEEP_DAYS=14`, `BACKUP_DIR=/var/lib/postgresql/data/backups`.

Run it manually once (Schedules → Run now) and confirm the log line
`backup ok: …`.

### Off-box copies (recommended)

Dumps in the data volume don't survive losing the VPS disk. Dokploy has a
native S3-compatible backup destination: **Settings → S3 Destinations** →
add bucket credentials, then on the postgres service enable **Backups** with
that destination and a daily cron. Any S3-compatible store works
(Backblaze B2, Cloudflare R2, AWS).

## Restore

### Restore drill (run quarterly — a backup you never restored is a hope,
not a backup)

Inside the postgres container (Dokploy → postgres service → Terminal):

```sh
LATEST=$(ls -t /var/lib/postgresql/data/backups/bcrc-*.dump | head -1)
createdb -U bcrc bcrc_drill
pg_restore -U bcrc -d bcrc_drill --no-owner "$LATEST"
psql -U bcrc -d bcrc_drill -c \
  "SELECT (SELECT count(*) FROM users)     AS users,
          (SELECT count(*) FROM machines)  AS machines,
          (SELECT count(*) FROM audit_log) AS audit_rows,
          (SELECT max(created_at) FROM audit_log) AS newest_audit;"
dropdb -U bcrc bcrc_drill
```

Sanity: counts non-zero (on a live system) and `newest_audit` close to the
dump timestamp. If `pg_restore` errors or counts look wrong, treat the
backup pipeline as broken and fix it *now*.

### Full restore (data loss / migration to a new box)

1. **Stop the app first** so nothing writes mid-restore: Dokploy →
   `bcrc-platform` → Stop.
2. In the postgres container, with the chosen dump:
   ```sh
   dropdb -U bcrc bcrc && createdb -U bcrc bcrc
   pg_restore -U bcrc -d bcrc --no-owner /path/to/bcrc-<stamp>.dump
   ```
   (`-Fc` dumps include schema + data; migrations table comes along, so the
   app won't re-run old migrations.)
3. Start `bcrc-platform`; check `/readyz` returns `{"ok":true,"db":true}`.
4. `uv run python scripts/smoke_platform.py https://bcrc.example.com`.
5. Expected fallout of restoring an older dump: sessions/machines created
   after the dump are gone — users re-login (magic link) and re-enroll.

## Deploys & rollback

- Push to `main` → auto-deploy (stop-then-start: ~seconds of downtime by
  design; two live relays would split the in-memory Router). Agents
  reconnect with backoff; phones replay via `attach{since_seq}`.
- Rollback: Dokploy → `bcrc-platform` → Deployments → previous build →
  Rollback. DB migrations are additive/plain SQL; rolling the app back one
  version is safe without touching the DB.
- Logs: Dokploy → `bcrc-platform` → Logs. Shutdown drain logs
  `drain: closing N live link(s)`.

## Health & monitoring

- `GET /healthz` — process up. `GET /readyz` — DB reachable (503 when not).
- `GET /metrics` (Bearer `PLATFORM_METRICS_TOKEN`) — Prometheus series:
  `bcrc_agents_connected`, `bcrc_clients_connected`, `bcrc_channels_open`,
  `bcrc_frames_relayed_total`, `bcrc_ws_closes_total{leg,code}`,
  `bcrc_quota_denials_total`, `bcrc_stripe_events_total`,
  `bcrc_emails_sent_total{status}`.
- Watch for: `bcrc_emails_sent_total{status="error"}` climbing (Resend/DNS
  problem — logins break silently for users), `ws_closes{code="4408"}`
  (slow clients), `readyz` flapping (DB pressure).

## Incident quick refs

- **Users can't log in, site up** → check `bcrc_emails_sent_total{status="error"}`
  and app logs for `resend refused` (expired API key / DKIM domain issue).
- **Phones can't reach machines, HTTP fine** → check Cloudflare (WS must be
  proxied), then `bcrc_agents_connected` (0 = agents can't connect — check
  relay logs for auth refusals).
- **After a deploy nothing reconnects** → confirm only ONE container runs
  (stop-then-start, never rolling); split-Router symptoms are phones seeing
  `machine_offline` while the agent believes it is connected.
