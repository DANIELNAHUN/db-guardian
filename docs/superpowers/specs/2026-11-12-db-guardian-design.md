# db-guardian — Design

Date: 2026-11-12
Status: Draft, pending owner approval

## 1. Intent

The owner runs an intranet on a local machine and an ETL database on another
machine in the same LAN. They want a small backend service that:

1. Takes scheduled `mysqldump` backups of selected databases, configured at
   runtime (stored in SQLite, editable through an API).
2. Records a permanent history of every backup: when it ran, how long it took,
   size, tables, row count per table, and whether it failed and why. The
   history is meant to accumulate over time ("fill this project with
   information"), e.g. "On 2026-11-11 at 3:40 PM a 12 GB backup was created,
   with N tables and these row counts".
3. Serves backup files for download via streaming, through signed, expiring URLs.
4. (Phase 2) Clones a chosen backup into another MySQL server — in practice the
   staging VPS — so tests run against real production data. Before cloning it
   reports how the target differs from the backup.

A separate UI inside the intranet will consume this API to list backups and
drive clones. That UI is **not** part of this project.

## 2. Environment (facts from the owner)

| Role | Location | Engine |
|------|----------|--------|
| Intranet DB (backup source, full) | Intranet machine in the LAN (~4 GB RAM) | MySQL 8.0.46, ~194 MB |
| ETL DB (backup source, partial: historical tables only) | ETL machine in the LAN (~10 GB RAM) — **db-guardian runs here** | MySQL 5.7.41, ~20 GB total |
| Staging (clone target) | Separate VPS | MySQL 8.0.46 (same Docker image as the intranet) |

- Only the intranet is cloned to staging. The ETL is backup-only in practice,
  but the clone feature must not hard-code that.
- Backups will be scheduled during hours when nobody uses the systems.
- Everything runs locally / in the owner's network.

## 3. Tools (decided by the owner)

Python, Docker, APScheduler, FastAPI (minimal), `mysqldump`, SQLite.

## 4. Goals, non-goals, anti-goals

**Goals**
- Reliable scheduled backups per configured database, with runtime-editable config.
- Permanent, queryable history of backups and clones.
- Streaming download with signed, expiring URLs.
- Phase 2: preview + execute clone of a backup into a target server.

**Non-goals**
- No UI (the intranet will host it).
- No authentication on regular endpoints — LAN-only is enough (owner's decision).
- No failure notifications (mail, chat, webhooks). Failures are visible in the
  history only (owner's decision).
- No incremental/binlog backups, no point-in-time recovery.
- No cloud/offsite storage.
- No multi-node or high-availability setup.

**Anti-goals (would be a failure even if it "works")**
- A clone that touches the production databases. (See §7.6.)
- A failed or partial backup that silently replaces a good one (retention must
  never delete a good backup because of a failed one).
- An empty table selection being interpreted as "all tables" and silently
  dumping 20 GB.
- Passwords returned by any API response, or the clone target password being
  persisted.
- db-guardian starving the intranet of resources on the shared machine.

## 5. Phasing

- **Phase 1:** sources config, scheduler, backups + metadata, retention,
  history API, signed streaming downloads, Docker packaging.
- **Phase 2:** clone preview (diff report) and clone execution.

Phase 1 must already store the per-table `SHOW CREATE TABLE` output, because
Phase 2's diff depends on it (no temporary database, no `.sql` parsing).

## 6. Phase 1 — Backups

### 6.1 Source configuration (SQLite, editable via API)

Each backup source has:
- `name` (unique, human label)
- connection: `host`, `port`, `user`, `password`, `database`
- `tables`: a JSON list. `["*"]` means all tables. A non-empty list of names
  means only those tables. `[]` is **rejected** with a validation error.
  `"*"` mixed with names is rejected.
- `schedule`: a cron expression (e.g. `0 3 * * *`)
- `retention`: number of successful backups to keep (default 3)
- `enabled`: boolean

Rules:
- `password` is **write-only**: accepted on create/update, never returned.
  On update, omitting it keeps the current one.
- `password` is stored **encrypted** in SQLite, with a key from `.env`.
- Creating, updating, enabling, disabling or deleting a source updates the
  scheduler immediately, without a restart.
- Deleting a source keeps its backup history (the source is soft-deleted or
  history rows keep a copy of the source name — builder's choice, but history
  must survive).
- A "test connection" endpoint is useful for the UI (builder's call, cheap).

### 6.2 Scheduling

- APScheduler with cron triggers. SQLite source config is the single source of
  truth; jobs are rebuilt from it at startup (no separate APScheduler job store
  needed).
- **One job at a time** (backups and, in Phase 2, clones share one worker).
  If schedules overlap, the second waits in a queue.
- Missed runs (service was down at the scheduled time): run once on startup if
  within a grace window, coalescing multiple misses into one. *Designer's call;
  builder picks the grace window, suggested 1 hour.*
- Manual trigger endpoint: enqueue a backup of a source now.

### 6.3 Backup execution

- Runs `mysqldump` (MySQL 8.x client, see §8) piped through gzip into a
  `.sql.gz` file. Written to a temporary name and renamed only on success;
  partial files are removed on failure.
- Flags (*designer's call*, builder may adjust with reason):
  - `--single-transaction --quick` (InnoDB-consistent, no table locks, streaming)
  - `--set-gtid-purged=OFF` (so the dump restores into another server)
  - `--no-tablespaces` (avoids requiring the PROCESS privilege on 8.0.21+)
  - `--routines --triggers --events` when `tables == ["*"]`; triggers only for
    a table list
  - `--column-statistics=0` automatically when the source server is 5.7
    (detected via `SELECT VERSION()`), because the 8.x client fails otherwise
  - Default `--add-drop-table` stays on (Phase 2 relies on it)
- Passwords are passed to the subprocess without appearing in the process list
  or logs (e.g. env var or a temporary option file).

### 6.4 Metadata recorded per backup (SQLite)

Per backup:
- source (id + name snapshot), trigger (`scheduled` / `manual`)
- `started_at`, `finished_at`, `duration`
- status: `running` / `success` / `failed`, plus error message on failure
- file path, file size (compressed bytes)
- source server version
- file state: `present` / `deleted_by_retention`

Per table in the backup:
- table name
- exact row count (`COUNT(*)`, **not** `information_schema` estimates)
- `SHOW CREATE TABLE` output

Known limitation (accepted): row counts are taken on a separate connection, so
they may differ slightly from the dump's snapshot if writes happen during the
backup. Backups run in idle hours, so this is acceptable. Counting inside a
`START TRANSACTION WITH CONSISTENT SNAPSHOT` keeps counts self-consistent.

Validation at backup time: if a listed table does not exist in the source, the
backup **fails** with a clear message naming the missing table(s).

Startup recovery: any backup left in `running` at startup is marked `failed`
with an "interrupted" message and its partial file removed.

### 6.5 Retention

- Per source, keep the `retention` most recent **successful** backups whose
  file is present. Older successful backups get their file deleted and are
  marked `deleted_by_retention`.
- **History rows are never deleted.** Failed backups never count toward
  retention and never cause a good backup to be deleted.
- Retention runs after each successful backup.

### 6.6 Timestamps

- Stored in UTC. API returns ISO-8601 with offset, converted to a display
  timezone configured in `.env` (the owner's local time).

### 6.7 API (Phase 1, sketch — builder finalizes paths/shapes)

- Sources: list, get, create, update, delete, enable/disable, test connection.
- Backups: trigger manual; list (filter by source, status, date range;
  paginated); detail including per-table rows.
- Downloads:
  - `POST` create a signed URL for a backup → returns URL + expiry.
  - `GET` the signed URL → streams the `.sql.gz` file. Rejects missing,
    expired, tampered signatures, and backups whose file was deleted.
  - Signature: HMAC with a key from `.env`, covering backup id + expiry.
    Default TTL configurable (suggested 15 minutes).
- Optional but cheap: a stats/summary endpoint (per source: last backup,
  last success, size trend). *Builder's call.*
- CORS: allowed origins from `.env` (the intranet UI calls from the browser).
- No auth on these endpoints (owner's decision). No password ever in responses.

## 7. Phase 2 — Clone

### 7.1 Flow

Two separate steps, both requiring the clone key:

1. **Preview** (read-only on the target): given a backup id and target
   connection data, connect to the target and report the diff.
2. **Execute**: given the same data, run the clone as a background job and
   return a clone id; a status endpoint reports progress/result.

### 7.2 Clone key

- A secret that lives only in this server's `.env`.
- Sent in a request header; compared in constant time.
- Missing or wrong key → 401/403, nothing happens.
- The intranet UI must have the operator type the key; it must not be stored
  in the intranet's frontend code.

### 7.3 Payload

Target `host`, `port`, `user`, `password`, `database`. The password is used
only for that request/job and **never persisted**.

The target database must already exist; otherwise fail with a clear error.
*Designer's call.*

### 7.4 Preview report

Compares the backup's stored metadata (§6.4) with the target's live schema
(`SHOW TABLES` + `SHOW CREATE TABLE` + `SELECT VERSION()`):

- **Missing in target:** tables in the backup but not in the target, with
  their `CREATE TABLE` statement.
- **Structure differs:** tables in both with different definitions (comparison
  should normalize noise such as `AUTO_INCREMENT=` values).
- **Only in target:** tables in the target but not in the backup. These will be
  **left untouched** by the clone; reported for information.
- **Matching:** tables identical in structure.
- **Version check:** source vs target server versions, with a warning when the
  dump likely won't restore (e.g. 8.0 dump with `utf8mb4_0900_ai_ci` into 5.7
  or MariaDB).
- Fails if the backup file is no longer present.

### 7.5 Execution

- Restores the backup by streaming `gunzip` → `mysql` client into the target
  database. Because the dump has `DROP TABLE IF EXISTS` + `CREATE TABLE` per
  table, every table in the backup ends up identical to the backup in
  structure and data; tables only in the target are not touched.
- Recorded in SQLite: backup id, target host/port/database/user (no password),
  `started_at`, `finished_at`, duration, status, error, tables processed,
  and the preview summary at execution time.
- Runs through the single worker queue (§6.2).
- An interrupted clone leaves the target partially restored; re-running the
  clone fixes it (drop+create per table makes it idempotent).

### 7.6 Safety guard

- Refuse to clone into any target whose host+port+database matches a
  configured backup source. This prevents an accidental overwrite of
  production. *Designer's call.* Builder should resolve obvious aliases
  (`localhost`, `127.0.0.1`, `host.docker.internal`) when comparing.

### 7.7 Decisions added at Phase 2 planning (2026-11-12)

- The staging MySQL is reached **directly** over the network: the owner exposes
  its port on the VPS (no SSH tunnel). Recommended: firewall it to the owner's
  public IP.
- Every connection to a clone target **requires TLS** (`ssl-mode=REQUIRED`);
  MySQL 8 auto-generates server certificates, so no setup is needed. Server
  certificates are not verified (self-signed). *Designer's call.*
- `DEFINER=` clauses are stripped while restoring, so views, triggers and
  routines are recreated by the target user, which usually lacks
  `SET_USER_ID`/`SUPER`. *Designer's call.*
- If `GUARDIAN_CLONE_KEY` is not set, clone endpoints answer 503 ("clone
  disabled"); the rest of the service works.
- Clone history (list/detail) does not require the key: it holds no secrets.

## 8. Deployment

- Docker image: Python + MySQL 8.x client tools (`mysqldump`, `mysql`). The
  8.x client handles `caching_sha2_password` (MySQL 8 default) and dumps 5.7
  with `--column-statistics=0`.
- docker-compose on the **ETL machine** (owner's decision). Reasons: it has
  more RAM (~10 GB vs ~4 GB on the intranet machine), and the heavy ETL dump
  stays local instead of crossing the network. The intranet dump (~194 MB)
  travels over the LAN. Disk is not a constraint on either machine.
  - volume for the SQLite file and the backups directory (Docker data lives
    under `/var`, ~411 GB free at design time)
  - memory limit (suggested 512 MB) so it cannot starve the ETL MySQL
  - how the container reaches the ETL MySQL on the same host (host network,
    `host.docker.internal`, or the ETL's Docker network) — builder decides
    based on how the ETL is deployed and documents it. The intranet MySQL is
    reached over the LAN.
- **Accepted risk (owner's decision):** ETL backups live on the same disk as
  the ETL database. A disk failure on that machine loses both. Off-host
  copies are a possible future addition, out of scope now.
- Expected footprint: ~60–120 MB RAM idle; during a backup ~1 CPU core
  (gzip) and modest extra RAM. The main load falls on the source MySQL server.

### 8.1 Environment variables (`.env`)

- encryption key for stored source passwords
- HMAC key for signed URLs + default TTL
- clone key (Phase 2)
- display timezone
- CORS allowed origins
- paths for SQLite and the backups directory
- default retention

The service must refuse to start if a required secret is missing.

## 9. Left to the builder

- Project layout and internal architecture.
- Exact endpoint paths, request/response schemas, pagination style.
- SQLite schema details and migrations approach.
- Test strategy. Integration tests against real MySQL 5.7 and 8.0 containers
  are strongly suggested for the `mysqldump` flags, metadata capture and the
  clone path.
- Logging format.

## 10. Designer's calls to double-check

These were not stated by the owner; the owner should confirm or override:

1. Missed scheduled runs are executed once on startup within a grace window.
2. Full backups include routines, triggers and events.
3. A listed table missing in the source fails the backup (instead of skipping it).
4. Clone target database must already exist (not auto-created).
5. Clone refuses targets that match a configured backup source.
6. Backups and clones share a single worker queue.
7. Deleting a source keeps its history.
