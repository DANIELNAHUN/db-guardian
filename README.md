# db-guardian

Scheduled MySQL backups with a permanent history, retention, and signed download links.
You configure which databases to back up through an HTTP API. db-guardian runs `mysqldump`
on a cron schedule, records tables, row counts, size, and duration for every run, keeps the
newest N files, and serves them as streaming downloads through expiring URLs.

Design: [`docs/superpowers/specs/2026-11-12-db-guardian-design.md`](docs/superpowers/specs/2026-11-12-db-guardian-design.md).
Phase 1 (backups) is implemented. Phase 2 (clone into another server) is not.

## Quick path

1. Create a backup user on each MySQL server (see [Privileges](#mysql-privileges)).
2. `cp .env.example .env` and fill in `GUARDIAN_ENCRYPTION_KEY` and `GUARDIAN_SIGNING_KEY`
   (see [Environment variables](#environment-variables)).
3. `docker compose up -d --build`
4. Register a database:

   ```bash
   curl -X POST http://localhost:8000/sources -H 'Content-Type: application/json' -d '{
     "name": "intranet", "host": "192.168.1.20", "port": 3306,
     "user": "guardian", "password": "...", "database": "intranet",
     "tables": ["*"], "schedule": "0 3 * * *"
   }'
   ```

5. Check it: `POST /sources/{id}/test-connection`, then run one now with `POST /sources/{id}/backups`.
6. Watch the result in `GET /backups`.

Interactive API docs are at `http://localhost:8000/docs`.

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `GUARDIAN_ENCRYPTION_KEY` | required | Fernet key that encrypts stored source passwords. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Changing it makes stored passwords unreadable (re-enter them with `PATCH`). |
| `GUARDIAN_SIGNING_KEY` | required | HMAC key for download links, at least 32 characters. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `GUARDIAN_CLONE_KEY` | — | Reserved for phase 2 (clone). Not used yet. |
| `GUARDIAN_DISPLAY_TZ` | `UTC` | IANA timezone for cron schedules and API timestamps, e.g. `America/Lima`. |
| `GUARDIAN_CORS_ORIGINS` | empty | Comma-separated origins allowed from a browser (the intranet UI). |
| `GUARDIAN_DOWNLOAD_TTL_SECONDS` | `900` | Lifetime of signed download links. |
| `GUARDIAN_DEFAULT_RETENTION` | `3` | Successful backups kept when a source sets no retention. |
| `GUARDIAN_MISFIRE_GRACE_SECONDS` | `3600` | A run missed while the service was down is executed on startup if it is at most this late. |
| `GUARDIAN_DUMP_TIMEOUT_SECONDS` | `14400` | A dump running longer than this (e.g. after a silent network drop) is killed and marked failed. |
| `GUARDIAN_DATA_DIR` | `/data` | SQLite (`guardian.db`) and backup files (`backups/`). |

The service refuses to start when a required variable is missing or invalid, and names it.

## Configuring sources

| Field | Meaning |
|-------|---------|
| `tables` | `["*"]` = every table (plus routines, triggers, events). A list such as `["hist_sales", "hist_stock"]` = only those tables. `[]` is rejected. |
| `schedule` | Cron expression in `GUARDIAN_DISPLAY_TZ`: `minute hour day-of-month month day-of-week`. |
| `retention` | Successful backups kept (default 3). Older files are deleted; their history stays. |
| `enabled` | `false` stops the schedule without deleting anything. |
| `password` | Write-only. Never returned. Omit it in `PATCH` to keep the current one. |

Schedule examples:

| Cron | Runs |
|------|------|
| `0 3 * * *` | every day at 03:00 |
| `30 2 * * 1-5` | Monday to Friday at 02:30 |
| `0 1 * * 0` | Sundays at 01:00 |

Changes made with `PATCH /sources/{id}` reschedule immediately, without a restart.

## API at a glance

| Method & path | Purpose |
|---------------|---------|
| `GET/POST /sources`, `GET/PATCH/DELETE /sources/{id}` | Manage sources. `DELETE` keeps the history. |
| `POST /sources/{id}/test-connection` | Returns `{ok, server_version, error}`. |
| `POST /sources/{id}/backups` | Queue a backup now. `202` new, `200` already queued. |
| `GET /backups?source_id&status&date_from&date_to&limit&offset` | History, newest first. |
| `GET /backups/{id}` | One backup with tables and row counts. |
| `POST /backups/{id}/download-url` | Returns a signed URL valid for `GUARDIAN_DOWNLOAD_TTL_SECONDS`. |
| `GET /downloads/{id}?exp&sig` | Streams the `.sql.gz`. `403` bad/expired link, `410` file gone. |

Backups run one at a time. Status flows `queued → running → success | failed`.
Timestamps are returned in `GUARDIAN_DISPLAY_TZ`.

## Deployment notes

- **Where it runs:** the ETL machine (more RAM, and the large ETL dump stays local).
- **Reaching MySQL on the same host:** use `host.docker.internal` when MySQL publishes its
  port on the host, or attach the service to MySQL's Docker network and use its service name.
- **Reaching the intranet MySQL:** use its LAN IP.
- **Data:** SQLite and backup files live in `./data` (mounted at `/data`).
- **Memory:** capped at 512 MB so it cannot starve the database server.
- **Accepted risk:** ETL backups live on the same disk as the ETL database. Losing that
  disk loses both. Copying backups off-host is a possible future addition.

## MySQL privileges

Create a dedicated user per server. These grants are what the integration tests use.

```sql
CREATE USER 'guardian'@'%' IDENTIFIED BY '...';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES ON intranet.* TO 'guardian'@'%';
-- MySQL 8.0.20+: needed to dump stored procedures/functions
GRANT SHOW_ROUTINE ON *.* TO 'guardian'@'%';
-- MySQL 5.7 instead:
-- GRANT SELECT ON mysql.proc TO 'guardian'@'%';
```

`PROCESS` is not required (`--no-tablespaces` is used).

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[test]"
.venv/bin/python -m pytest -m "not integration" -q          # unit tests
docker compose -f docker-compose.test.yml run --rm --build tests   # MySQL 5.7.41 + 8.0.46
docker compose -f docker-compose.test.yml down -v
```
