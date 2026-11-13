# db-guardian

Scheduled MySQL backups with a permanent history, retention, and signed download links.
You configure which databases to back up through an HTTP API. db-guardian runs `mysqldump`
on a cron schedule, records tables, row counts, size, and duration for every run, keeps the
newest N files, and serves them as streaming downloads through expiring URLs.

Design: [`docs/superpowers/specs/2026-11-12-db-guardian-design.md`](docs/superpowers/specs/2026-11-12-db-guardian-design.md).
Phase 1 (backups) and phase 2 (clone a backup into another server) are implemented.

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
| `GUARDIAN_CLONE_KEY` | — | Secret required in the `X-Clone-Key` header to preview or start a clone. Unset = clone endpoints answer 503. Generate like the signing key. |
| `GUARDIAN_CLONE_TIMEOUT_SECONDS` | `14400` | A restore running longer than this is killed and the clone marked failed. |
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
| `POST /backups/{id}/clone-preview` | Diff the backup against a target (needs `X-Clone-Key`). |
| `POST /backups/{id}/clones` | Start a clone into a target (needs `X-Clone-Key`). |
| `GET /clones`, `GET /clones/{id}` | Clone history, status and progress. |

Backups run one at a time. Status flows `queued → running → success | failed`.
Timestamps are returned in `GUARDIAN_DISPLAY_TZ`.

## Cloning a backup into staging

Two steps, both with the header `X-Clone-Key: <GUARDIAN_CLONE_KEY>` and the target in the body:

```json
{ "host": "staging.example.com", "port": 3306, "user": "stager", "password": "...", "database": "intranet" }
```

1. **Preview** — `POST /backups/{id}/clone-preview`. Changes nothing. Returns tables missing in the
   target (with their `CREATE TABLE`), tables whose structure differs, tables that only exist in the
   target, matching tables, and version warnings.
2. **Clone** — `POST /backups/{id}/clones` → `202` with the clone id. Follow it with `GET /clones/{id}`
   (`status`, `progress` from 0 to 1, `error`). History: `GET /clones?backup_id&status`.

What a clone does:

- Every table in the backup is dropped and recreated in the target with the backup's structure and
  data. **Tables that only exist in the target are left untouched.**
- `DEFINER=` clauses are removed, so views, triggers and routines are recreated by the target user.
- Re-running a clone is safe; an interrupted one (for example a restart) is marked failed and must
  be started again, because the target password is never stored.
- It refuses any target that points at a configured backup source (`409`), so production cannot be
  overwritten by mistake.

| Response | Meaning |
|----------|---------|
| `401` / `403` | Missing / wrong `X-Clone-Key`. |
| `409` | The target is one of your backup sources. |
| `410` | The backup file was removed by retention. |
| `422` | The target database does not exist or the user cannot access it. |
| `502` | The target could not be reached (network, credentials, TLS). Message included. |
| `503` | `GUARDIAN_CLONE_KEY` is not set. |

### Preparing the staging server

- **Network:** expose MySQL's port on the VPS **only to your network's public IP** (firewall rule).
- **TLS:** every clone connection requires TLS. MySQL 8 enables it with self-signed certificates by
  default; nothing to configure. A server without TLS is refused.
- **Binary logging:** MySQL 8 has it on, and then a user without `SUPER` cannot create triggers or
  functions (error 1419). Set this on staging (in `my.cnf` under `[mysqld]`, or
  `SET PERSIST log_bin_trust_function_creators = 1;` as root):

  ```ini
  log_bin_trust_function_creators = 1
  ```

- **User:** grant the clone user rights on the target database only:

  ```sql
  CREATE USER 'stager'@'%' IDENTIFIED BY '...';
  GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES,
        CREATE VIEW, SHOW VIEW, TRIGGER, CREATE ROUTINE, ALTER ROUTINE, EVENT, LOCK TABLES
     ON intranet.* TO 'stager'@'%';
  ```

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
