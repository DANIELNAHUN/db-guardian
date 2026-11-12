# db-guardian Phase 1 (Backups) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Dockerized FastAPI service that runs scheduled `mysqldump` backups configured in SQLite, records a permanent per-table history, applies retention, and serves backups through signed, expiring streaming downloads.

**Architecture:** One Python process: FastAPI for the API, APScheduler (cron triggers only) that enqueues work into a single-worker in-process queue, and a backup runner that reads metadata with PyMySQL and streams `mysqldump` stdout into a gzip file. SQLite (SQLAlchemy 2.0) is the single source of truth for configuration and history. Code is organized by feature (`sources/`, `backups/`, `downloads/`, `scheduling/`, `mysqlclient/`).

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, APScheduler 3.10.x, SQLAlchemy 2.0, PyMySQL (+ `cryptography` for `caching_sha2_password`), `cryptography` Fernet, pydantic-settings, tzdata, pytest, httpx. Docker image with the MySQL 8.4 LTS client (`mysqldump`, `mysql`) from repo.mysql.com.

**Spec:** `docs/superpowers/specs/2026-11-12-db-guardian-design.md` (§1–§6, §8, §10). Phase 2 (§7, clone) is a separate plan.

## Global Constraints

- All generated artifacts (code, identifiers, comments, docs, commit messages) in English.
- Conventional commits, no AI attribution / `Co-Authored-By` lines (owner's global rule).
- Work on branch `feat/phase-1-backups`, never on `main`.
- `tables`: `["*"]` = all tables; non-empty list of names = those tables; `[]` rejected; `"*"` mixed with names rejected.
- Source `password` is write-only (never in any response), stored Fernet-encrypted; omitted on update = keep current.
- `retention` default 3 (from `GUARDIAN_DEFAULT_RETENTION`), minimum 1.
- Only **successful** backups count toward retention; history rows are never deleted.
- Exact row counts via `COUNT(*)`, never `information_schema` estimates.
- One job at a time: a single worker thread runs every backup.
- Timestamps stored as UTC; API returns ISO-8601 with offset in `GUARDIAN_DISPLAY_TZ`.
- Service refuses to start when `GUARDIAN_ENCRYPTION_KEY` or `GUARDIAN_SIGNING_KEY` is missing or invalid.
- Passwords never appear in process arguments or logs.
- No auth on endpoints (spec §4); CORS origins from `GUARDIAN_CORS_ORIGINS`.

### Environment variables (prefix `GUARDIAN_`)

| Name | Default | Notes |
|------|---------|-------|
| `ENCRYPTION_KEY` | required | Fernet key |
| `SIGNING_KEY` | required | HMAC key, min 32 chars |
| `CLONE_KEY` | none | Phase 2, not read in Phase 1 |
| `DOWNLOAD_TTL_SECONDS` | `900` | |
| `DISPLAY_TZ` | `UTC` | IANA name, validated at startup |
| `CORS_ORIGINS` | empty | comma-separated |
| `DATA_DIR` | `/data` | SQLite at `DATA_DIR/guardian.db`, files at `DATA_DIR/backups/` |
| `DEFAULT_RETENTION` | `3` | |
| `MISFIRE_GRACE_SECONDS` | `3600` | spec §10.1 |

## Review Focus

1. **8.4 client dumping MySQL 5.7.41**: must succeed thanks to `--column-statistics=0`; pinned by the integration test in Task 5 running against both 5.7.41 and 8.0.46.
2. **Dump dies mid-stream** (disk full, killed, network drop to the intranet DB): `.tmp` file removed, backup `failed` with stderr tail, previous good backups untouched; pinned in Task 6.
3. **Password with special characters** (`#`, `"`, `\`, spaces, `=`) in the option file: must still authenticate; pinned in Task 4 (unit) and Task 5 (integration user with such a password).
4. **Manual trigger while the same source is already queued, plus overlapping schedules**: no duplicate queued backups, strictly one running job; pinned in Task 7.
5. **Signed URL whose file was removed by retention before download**: 410, never a 500 or a partial stream; pinned in Task 9.

---

## File Structure

```
pyproject.toml
Dockerfile
docker-compose.yml            # production (ETL machine)
docker-compose.test.yml       # MySQL 5.7.41 + 8.0.46 + test runner
.env.example
README.md
src/db_guardian/
  config.py                   # Settings
  crypto.py                   # SecretBox (Fernet)
  db.py                       # Base, engine, session factory, init_db
  timefmt.py                  # UTC <-> display tz
  container.py                # Container: wires settings, db, queue, scheduler, runner
  app.py                      # create_app(), lifespan, CORS, routers
  mysqlclient/
    connection.py             # ConnectionInfo
    metadata.py               # version, tables, row counts, SHOW CREATE TABLE
    dump.py                   # option file, mysqldump args, streaming to gzip
  sources/
    models.py  schemas.py  service.py  api.py
  backups/
    models.py  schemas.py  service.py  runner.py  retention.py  recovery.py  api.py
  scheduling/
    queue.py                  # WorkQueue (single worker, dedupe by key)
    scheduler.py              # GuardianScheduler, missed_fire_time()
  downloads/
    signing.py  api.py
tests/
  conftest.py                 # settings/container/client fixtures with fakes
  unit/...                    # mirrors src layout
  integration/                # marked @pytest.mark.integration, need MySQL + mysqldump
```

Test commands:
- Unit: `pytest -m "not integration" -q`
- Integration: `docker compose -f docker-compose.test.yml run --rm tests` (runs `pytest -m integration`)

---

### Task 1: Scaffold, settings, crypto, database, app factory

**Files:**
- Create: `pyproject.toml`, `src/db_guardian/{__init__,config,crypto,db,timefmt,container,app}.py`, `tests/conftest.py`, `tests/unit/test_config.py`, `tests/unit/test_crypto.py`, `tests/unit/test_timefmt.py`, `tests/unit/test_app.py`, `.gitignore` (add `.env`, `data/`, `.venv/`, `__pycache__/`)

**Interfaces:**
- Produces:
  - `class Settings(BaseSettings)` with fields from the env table above; properties `db_path: Path`, `backups_dir: Path`; `cors_origins: list[str]` parsed from comma-separated string.
  - `def load_settings() -> Settings` — raises `ConfigError` (subclass of `RuntimeError`) naming the missing/invalid variable.
  - `class SecretBox: __init__(key: str); encrypt(plain: str) -> str; decrypt(token: str) -> str`.
  - `class Base(DeclarativeBase)`; `make_engine(db_path: Path) -> Engine` (SQLite, `check_same_thread=False`, WAL); `make_session_factory(engine) -> sessionmaker[Session]`; `init_db(engine) -> None` (`Base.metadata.create_all`).
  - `utcnow() -> datetime` (naive UTC); `to_display(dt: datetime | None, tz: str) -> str | None` (naive treated as UTC, ISO-8601 with offset).
  - `@dataclass class Container` with `settings, engine, session_factory, box` (later tasks add fields); `build_container(settings: Settings, **overrides) -> Container`.
  - `create_app(container: Container, *, start_background: bool = True) -> FastAPI`; `GET /health -> {"status": "ok"}`.
  - Module-level `app` in `app.py` built from `load_settings()` lazily via a factory for `uvicorn db_guardian.app:build --factory`.

- [ ] **Step 1: Create branch** — `git checkout -b feat/phase-1-backups`

- [ ] **Step 2: Write failing tests**

```python
# tests/unit/test_config.py
def test_missing_encryption_key_fails(monkeypatch, tmp_path):
    monkeypatch.delenv("GUARDIAN_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("GUARDIAN_SIGNING_KEY", "x" * 32)
    with pytest.raises(ConfigError, match="GUARDIAN_ENCRYPTION_KEY"):
        load_settings()

def test_invalid_fernet_key_fails(...):      # "not-a-key" -> ConfigError naming ENCRYPTION_KEY
def test_short_signing_key_fails(...):       # 10 chars -> ConfigError naming SIGNING_KEY
def test_invalid_display_tz_fails(...):      # "Mars/Base" -> ConfigError naming DISPLAY_TZ
def test_defaults(...):                      # ttl 900, retention 3, grace 3600, cors [] ; "a,b" -> ["a","b"]

# tests/unit/test_crypto.py
def test_roundtrip_and_ciphertext_differs():
    box = SecretBox(Fernet.generate_key().decode())
    token = box.encrypt('p@ss "#\\ =')
    assert token != 'p@ss "#\\ =' and box.decrypt(token) == 'p@ss "#\\ ='

# tests/unit/test_timefmt.py
def test_to_display_converts_utc_to_tz():
    assert to_display(datetime(2026, 11, 11, 20, 40), "America/Lima") == "2026-11-11T15:40:00-05:00"
def test_to_display_none(): assert to_display(None, "UTC") is None

# tests/unit/test_app.py
def test_health(client): assert client.get("/health").json() == {"status": "ok"}
```

`tests/conftest.py` provides: `settings` (tmp `DATA_DIR`, generated keys), `container` (`build_container(settings)`), `client` (`TestClient(create_app(container, start_background=False))`).

- [ ] **Step 3: Run** `pytest -m "not integration" -q` → FAIL (imports missing)
- [ ] **Step 4: Implement** the interfaces above. `pyproject.toml`: package `db-guardian`, `src` layout, `requires-python = ">=3.12"`, pytest marker `integration`. CORS middleware added only when `cors_origins` is non-empty.
- [ ] **Step 5: Run** `pytest -m "not integration" -q` → PASS
- [ ] **Step 6: Commit** — `feat: scaffold service with settings, encryption and app factory`

---

### Task 2: Sources — model, validation, CRUD API (password write-only)

**Files:**
- Create: `src/db_guardian/mysqlclient/{__init__,connection}.py`, `src/db_guardian/sources/{__init__,models,schemas,service,api}.py`, `tests/unit/sources/test_schemas.py`, `tests/unit/sources/test_api.py`
- Modify: `src/db_guardian/app.py` (include router), `src/db_guardian/container.py`

**Interfaces:**
- Consumes: `Base`, `SecretBox`, `Container`, `utcnow`, `to_display`.
- Produces:
  - `@dataclass(frozen=True) class ConnectionInfo: host: str; port: int; user: str; password: str; database: str` with `__repr__` that masks `password`.
  - Model `Source` (table `sources`): `id, name (unique), host, port, user, password_enc, database, tables (JSON list), schedule, retention, enabled, created_at, updated_at, deleted_at (nullable)`.
  - `ALL_TABLES = "*"`; `def selected_tables(tables: list[str]) -> list[str] | None` — `None` means all.
  - Schemas: `SourceCreate`, `SourceUpdate` (all optional), `SourceOut` (no password field at all; includes `has_password: bool`, timestamps via `to_display`).
  - `class SourceService(session: Session, box: SecretBox)`: `create(data) -> Source`, `update(id, data) -> Source`, `get(id) -> Source` (raises `NotFound` for missing or soft-deleted), `list() -> list[Source]`, `delete(id) -> None` (soft: sets `deleted_at`), `connection(source: Source) -> ConnectionInfo`.
  - Router `/sources`: `GET`, `POST` (201), `GET /{id}`, `PATCH /{id}`, `DELETE /{id}` (204). Uniqueness conflict → 409. Not found → 404.
  - Hook for later tasks: `Container.on_source_changed: Callable[[Source | None, int], None]` called after create/update/delete (no-op default). Task 8 wires it to the scheduler.

- [ ] **Step 1: Write failing tests**

```python
# test_schemas.py
@pytest.mark.parametrize("tables", [[], ["*", "users"], ["  "]])
def test_invalid_tables_rejected(tables): with pytest.raises(ValidationError): SourceCreate(**valid(tables=tables))
def test_star_and_list_accepted(): SourceCreate(**valid(tables=["*"])); SourceCreate(**valid(tables=["hist_a"]))
def test_invalid_cron_rejected(): with pytest.raises(ValidationError): SourceCreate(**valid(schedule="99 * * * *"))
def test_retention_min_1(): with pytest.raises(ValidationError): SourceCreate(**valid(retention=0))
def test_retention_defaults_to_setting(client): assert client.post("/sources", json=valid_no_retention()).json()["retention"] == 3

# test_api.py
def test_password_never_returned(client):
    created = client.post("/sources", json=valid(password="secret")).json()
    for body in (created, client.get(f"/sources/{created['id']}").json(), client.get("/sources").json()[0]):
        assert "password" not in body and "secret" not in json.dumps(body)
def test_password_stored_encrypted(container, client): # raw row password_enc != "secret", box.decrypt(...) == "secret"
def test_patch_without_password_keeps_it(...):
def test_duplicate_name_409(...):
def test_delete_is_soft_and_hides_source(...):  # 204, GET -> 404, row still exists with deleted_at
```

- [ ] **Step 2: Run** `pytest tests/unit/sources -q` → FAIL
- [ ] **Step 3: Implement.** Cron validation via `apscheduler.triggers.cron.CronTrigger.from_crontab(value)`. Strip table names; reject empty strings.
- [ ] **Step 4: Run** `pytest -m "not integration" -q` → PASS
- [ ] **Step 5: Commit** — `feat(sources): add backup source configuration API`

---

### Task 3: Backup history model and read API

**Files:**
- Create: `src/db_guardian/backups/{__init__,models,schemas,api}.py`, `tests/unit/backups/test_api.py`
- Modify: `src/db_guardian/app.py`

**Interfaces:**
- Consumes: `Source`, `to_display`.
- Produces:
  - `class BackupStatus(StrEnum): QUEUED, RUNNING, SUCCESS, FAILED` (values lowercase). `QUEUED` is an addition to spec §6.4 so the UI can see pending work.
  - `class FileState(StrEnum): NONE, PRESENT, DELETED_BY_RETENTION`.
  - `class BackupTrigger(StrEnum): SCHEDULED, MANUAL`.
  - Model `Backup` (table `backups`): `id, source_id (FK), source_name (snapshot), trigger, status, error (nullable), created_at, started_at, finished_at, duration_seconds (float), file_path, file_size (int), server_version, file_state`.
  - Model `BackupTable` (table `backup_tables`): `id, backup_id (FK, indexed), name, row_count (BigInteger), create_sql (Text)`.
  - `GET /backups?source_id&status&date_from&date_to&limit=50&offset=0` → `{"items": [BackupOut], "total": int}`, newest first; `limit` max 200. `date_from`/`date_to` are ISO datetimes compared against `created_at`.
  - `GET /backups/{id}` → `BackupDetailOut` = `BackupOut` + `tables: [{name, row_count}]` + `table_count`, `total_rows`. `create_sql` is **not** exposed in Phase 1.
  - `BackupOut` includes `id, source_id, source_name, trigger, status, error, created_at, started_at, finished_at, duration_seconds, file_size, server_version, file_state` (timestamps via `to_display`).

- [ ] **Step 1: Write failing tests** (seed rows directly via session)

```python
def test_list_newest_first_and_total(client, seed): ...  # 3 rows -> ids descending, total 3
def test_filter_by_source_and_status(client, seed): ...
def test_detail_has_tables_and_totals(client, seed):
    body = client.get(f"/backups/{b.id}").json()
    assert body["table_count"] == 2 and body["total_rows"] == 15 and "create_sql" not in json.dumps(body)
def test_history_survives_source_soft_delete(client, seed): ... # backup still listed with source_name
def test_unknown_backup_404(client): ...
```

- [ ] **Step 2: Run** `pytest tests/unit/backups/test_api.py -q` → FAIL
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -m "not integration" -q` → PASS
- [ ] **Step 5: Commit** — `feat(backups): add backup history model and read API`

---

### Task 4: mysqldump command builder and option file

**Files:**
- Create: `src/db_guardian/mysqlclient/dump.py`, `tests/unit/mysqlclient/test_dump.py`

**Interfaces:**
- Consumes: `ConnectionInfo`.
- Produces:
  - `class DumpError(RuntimeError)` with `stderr_tail: str`.
  - `def parse_version(v: str) -> tuple[int, int]` (`"5.7.41-log"` → `(5, 7)`, `"8.0.46"` → `(8, 0)`).
  - `def write_option_file(conn: ConnectionInfo, directory: Path) -> Path` — `[client]` section with `user`, `password`, `host`, `port`; values double-quoted with `\` and `"` escaped; file mode 0600 where supported.
  - `def build_dump_args(conn: ConnectionInfo, tables: list[str] | None, server_version: str, option_file: Path) -> list[str]`.
  - `def dump_to_gzip(conn: ConnectionInfo, tables: list[str] | None, server_version: str, dest: Path) -> int` — writes option file into a private temp dir, runs `mysqldump`, streams stdout in 1 MiB chunks into `gzip.open(dest, "wb", compresslevel=6)`, returns bytes of `dest`; on non-zero exit raises `DumpError` with the last 2000 chars of stderr; always deletes the option file. Does **not** delete `dest` (runner owns cleanup).
  - Type alias `Dumper = Callable[[ConnectionInfo, list[str] | None, str, Path], int]`.

Exact argument rules (spec §6.3):
- `args[0] == "mysqldump"`, `args[1] == f"--defaults-extra-file={option_file}"` (must be first option).
- Always: `--single-transaction`, `--quick`, `--set-gtid-purged=OFF`, `--no-tablespaces`, `--add-drop-table`, `--default-character-set=utf8mb4`.
- `tables is None`: also `--routines`, `--triggers`, `--events`; then the database name.
- table list: `--triggers`, then database name, then each table name (in given order).
- `parse_version(server_version) < (8, 0)`: add `--column-statistics=0`.
- No argument contains the password.

- [ ] **Step 1: Write failing tests**

```python
def test_full_dump_args(): args = build(tables=None, version="8.0.46")
    assert args[:2] == ["mysqldump", f"--defaults-extra-file={opt}"]
    assert {"--routines", "--events", "--triggers", "--set-gtid-purged=OFF", "--no-tablespaces"} <= set(args)
    assert args[-1] == "intranet" and "--column-statistics=0" not in args
def test_table_list_args(): args = build(tables=["hist_a", "hist_b"], version="5.7.41-log")
    assert args[-3:] == ["etl", "hist_a", "hist_b"] and "--routines" not in args and "--column-statistics=0" in args
def test_password_not_in_args(): assert all("s3cr3t" not in a for a in build(...))
@pytest.mark.parametrize("pw", ['a#b', 'q"uote', 'back\\slash', 'with space', 'eq=ual'])
def test_option_file_roundtrips_password(tmp_path, pw):
    path = write_option_file(conn(pw), tmp_path)
    parser = configparser.ConfigParser(); parser.read(path)  # sanity: section exists
    assert unescape(parser["client"]["password"]) == pw       # unescape: strip quotes, undo \\ and \"
def test_dump_to_gzip_raises_and_removes_option_file(monkeypatch, tmp_path): # fake executable via PATH or patch subprocess.Popen returning exit 2 with stderr "Access denied"
```

- [ ] **Step 2: Run** `pytest tests/unit/mysqlclient/test_dump.py -q` → FAIL
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** — `feat(mysqlclient): build mysqldump commands and stream dumps to gzip`

---

### Task 5: Metadata reader + Docker test environment + integration tests

**Files:**
- Create: `src/db_guardian/mysqlclient/metadata.py`, `Dockerfile`, `docker-compose.test.yml`, `tests/integration/conftest.py`, `tests/integration/test_metadata.py`, `tests/integration/test_dump.py`, `tests/integration/fixtures/seed.sql`

**Interfaces:**
- Consumes: `ConnectionInfo`, `dump_to_gzip`.
- Produces:
  - `class MissingTablesError(ValueError)` with `missing: list[str]`; message names the tables.
  - `@dataclass class TableMeta: name: str; row_count: int; create_sql: str`.
  - `@dataclass class SnapshotMeta: server_version: str; tables: list[TableMeta]`.
  - `def server_version(conn: ConnectionInfo) -> str` (`SELECT VERSION()`).
  - `def capture_metadata(conn: ConnectionInfo, tables: list[str] | None) -> SnapshotMeta` — lists base tables via `SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'`; with a list, raises `MissingTablesError` for names not present (check happens **before** any query uses the names); inside `START TRANSACTION WITH CONSISTENT SNAPSHOT` runs `SELECT COUNT(*)` and `SHOW CREATE TABLE` per table, identifiers backtick-quoted with backticks doubled.
  - Type alias `MetadataReader = Callable[[ConnectionInfo, list[str] | None], SnapshotMeta]`.
  - `Dockerfile`: `python:3.12-slim-bookworm`; installs `mysql-community-client` from the MySQL APT repo (`mysql-8.4-lts`, key via `signed-by`); copies `src/` and installs the package; `CMD ["uvicorn", "db_guardian.app:build", "--factory", "--host", "0.0.0.0", "--port", "8000"]`. A `test` build stage also installs test deps and `tests/`.
  - `docker-compose.test.yml`: services `mysql57` (`mysql:5.7.41`), `mysql80` (`mysql:8.0.46`), both seeded with `seed.sql` and a user `guardian` whose password is `p#ss "w\\rd"` (Review Focus 3); `tests` service (Dockerfile `test` target) with env `IT_MYSQL57_HOST`, `IT_MYSQL80_HOST`, waits on healthchecks, runs `pytest -m integration -q`.
  - `seed.sql`: database `shop` with `customers` (3 rows), `orders` (5 rows, FK to customers), `hist_sales` (7 rows), one view `v_orders`, one trigger, one procedure.

- [ ] **Step 1: Write failing integration tests** (parametrized over `mysql57`, `mysql80`)

```python
def test_capture_all_tables(conn):
    meta = capture_metadata(conn, None)
    assert {t.name: t.row_count for t in meta.tables} == {"customers": 3, "orders": 5, "hist_sales": 7}
    assert all(t.create_sql.startswith("CREATE TABLE") for t in meta.tables)  # view excluded
def test_capture_selected(conn): assert [t.name for t in capture_metadata(conn, ["hist_sales"]).tables] == ["hist_sales"]
def test_missing_table_raises(conn):
    with pytest.raises(MissingTablesError) as e: capture_metadata(conn, ["hist_sales", "nope`; DROP"])
    assert e.value.missing == ["nope`; DROP"]
def test_dump_full_restores(conn, tmp_path):   # Review Focus 1 & 3
    size = dump_to_gzip(conn, None, server_version(conn), tmp_path / "d.sql.gz")
    assert size > 0
    restore into fresh db `restore_check` via `mysql` client; assert counts 3/5/7 and the view/trigger/procedure exist
def test_dump_table_list_only_contains_those(conn, tmp_path): # gunzip text has "CREATE TABLE `hist_sales`" and not "CREATE TABLE `orders`"
def test_wrong_password_raises_dump_error(conn, tmp_path): # DumpError, stderr_tail mentions "Access denied"
```

- [ ] **Step 2: Run** `docker compose -f docker-compose.test.yml run --rm tests` → FAIL (metadata module missing)
- [ ] **Step 3: Implement** `metadata.py`, finish Dockerfile.
- [ ] **Step 4: Run** the integration command → PASS on both 5.7.41 and 8.0.46. Also `pytest -m "not integration" -q` → PASS.
- [ ] **Step 5: Commit** — `feat(mysqlclient): capture table metadata and add MySQL integration tests`

---

### Task 6: Backup runner and retention

**Files:**
- Create: `src/db_guardian/backups/runner.py`, `src/db_guardian/backups/retention.py`, `tests/unit/backups/test_runner.py`, `tests/unit/backups/test_retention.py`
- Modify: `src/db_guardian/container.py` (add `dumper`, `metadata_reader`, `runner`)

**Interfaces:**
- Consumes: `Backup`, `BackupTable`, `BackupStatus`, `FileState`, `SourceService.connection`, `selected_tables`, `Dumper`, `MetadataReader`, `MissingTablesError`, `DumpError`.
- Produces:
  - `def apply_retention(session: Session, source_id: int, keep: int) -> list[int]` — among this source's `SUCCESS` backups with `file_state == PRESENT`, ordered by `finished_at` desc, keeps `keep`; for the rest deletes the file (missing file is fine) and sets `DELETED_BY_RETENTION`; returns affected ids. Never touches failed backups or deletes rows.
  - `class BackupRunner(session_factory, box: SecretBox, backups_dir: Path, dumper: Dumper, metadata_reader: MetadataReader)` with `run(backup_id: int) -> None`:
    1. load backup + source; set `RUNNING`, `started_at`.
    2. `metadata_reader(conn, selected_tables(source.tables))`.
    3. dest = `backups_dir / <source_id> / <YYYYmmddTHHMMSSZ>_<backup_id>.sql.gz`; dump to `dest.with_suffix(".tmp")`, then `os.replace` to dest.
    4. on success: store `BackupTable` rows, `server_version`, `file_path`, `file_size`, `file_state=PRESENT`, `SUCCESS`, `finished_at`, `duration_seconds`; then `apply_retention(session, source.id, source.retention)`.
    5. on any exception: remove `.tmp` and dest if present, `FAILED`, `error` = exception message (for `DumpError` include `stderr_tail`), `finished_at`, `duration_seconds`; never call retention. Never re-raise (worker must survive), but log with `logger.exception` without the password.

- [ ] **Step 1: Write failing tests** (fakes for dumper/metadata, real SQLite)

```python
def test_success_records_everything(runner, seed_source, fake_meta):
    b = run_one(); assert b.status == "success" and b.file_state == "present" and Path(b.file_path).exists()
    assert {(t.name, t.row_count) for t in b.tables} == {("hist_a", 10), ("hist_b", 20)} and b.duration_seconds >= 0
def test_dump_failure_midstream_cleans_up_and_keeps_previous(runner, ...):   # Review Focus 2
    good = run_one()                                       # success
    fake_dumper writes 100 bytes to dest then raises DumpError("boom", stderr_tail="No space left on device")
    bad = run_one()
    assert bad.status == "failed" and "No space left" in bad.error
    assert not any(p.suffix == ".tmp" for p in backups_dir.rglob("*")) and Path(good.file_path).exists()
def test_missing_tables_fails_with_names(...): # error contains "hist_x"
def test_retention_keeps_n_successful(session, tmp_path):
    5 successes + 2 failures for source, keep=3 -> 2 oldest successes DELETED_BY_RETENTION, files gone, 7 rows still present
def test_retention_ignores_other_sources(...):
def test_retention_tolerates_already_missing_file(...):
```

- [ ] **Step 2: Run** `pytest tests/unit/backups -q` → FAIL
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -m "not integration" -q` → PASS
- [ ] **Step 5: Commit** — `feat(backups): run backups with metadata capture and retention`

---

### Task 7: Single-worker queue, manual trigger, startup recovery, test connection

**Files:**
- Create: `src/db_guardian/scheduling/{__init__,queue}.py`, `src/db_guardian/backups/service.py`, `src/db_guardian/backups/recovery.py`, `tests/unit/scheduling/test_queue.py`, `tests/unit/backups/test_service.py`, `tests/unit/backups/test_recovery.py`
- Modify: `src/db_guardian/sources/api.py`, `src/db_guardian/container.py`, `src/db_guardian/app.py` (lifespan starts/stops queue when `start_background`)

**Interfaces:**
- Consumes: `BackupRunner.run`, `Backup`, `metadata.server_version`.
- Produces:
  - `class WorkQueue: start() -> None; stop(timeout: float = 30) -> None; submit(key: str, fn: Callable[[], None]) -> bool; join() -> None` (blocks until empty, for tests). One daemon worker thread. `submit` returns `False` if a job with the same `key` is pending (not yet started). A job raising does not stop the worker.
  - `def enqueue_backup(container: Container, source_id: int, trigger: BackupTrigger) -> tuple[Backup, bool]` — if a `QUEUED` backup exists for the source, returns `(it, False)`; otherwise creates a `QUEUED` row and submits key `f"backup:{source_id}"` running `runner.run(id)`, returns `(new, True)`. Disabled or deleted sources raise `SourceUnavailable` (scheduled path skips; API → 409).
  - `def recover_interrupted(session_factory, backups_dir: Path) -> int` — marks every `QUEUED`/`RUNNING` backup `FAILED` with error `"interrupted: service stopped before completion"`, deletes stray `*.tmp` under `backups_dir`; returns count. Called in lifespan **before** the queue starts.
  - `POST /sources/{id}/backups` → 202 `BackupOut` when newly queued, 200 with the existing one when already queued.
  - `POST /sources/{id}/test-connection` → 200 `{"ok": true, "server_version": "..."}` or 200 `{"ok": false, "error": "..."}` (error text without password).

- [ ] **Step 1: Write failing tests**

```python
def test_queue_runs_one_at_a_time():   # two jobs record (start,end) timestamps; intervals do not overlap
def test_queue_dedupes_pending_key():  # block worker with an Event; submit("k") True, submit("k") False; release
def test_queue_survives_job_exception():
def test_manual_trigger_twice_returns_same_queued(client, blocked_queue):   # Review Focus 4
    r1 = client.post(f"/sources/{s}/backups"); r2 = client.post(f"/sources/{s}/backups")
    assert r1.status_code == 202 and r2.status_code == 200 and r1.json()["id"] == r2.json()["id"]
def test_trigger_disabled_source_409(client): ...
def test_recovery_marks_interrupted_and_removes_tmp(session_factory, tmp_path): ...
def test_test_connection_reports_error_without_password(client, failing_metadata): ...
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -m "not integration" -q` → PASS
- [ ] **Step 5: Commit** — `feat(backups): add single-worker queue, manual trigger and startup recovery`

---

### Task 8: Scheduler — cron jobs, live resync, missed-run catch-up

**Files:**
- Create: `src/db_guardian/scheduling/scheduler.py`, `tests/unit/scheduling/test_scheduler.py`
- Modify: `src/db_guardian/container.py` (wire `on_source_changed` → `scheduler.sync_source`), `src/db_guardian/app.py` (lifespan: recover → queue.start → scheduler.start → catch-up)

**Interfaces:**
- Consumes: `enqueue_backup`, `Source`, `Backup`, `Settings.misfire_grace_seconds`.
- Produces:
  - `def missed_fire_time(schedule: str, tz: str, last_started_at: datetime | None, now: datetime, grace_seconds: int) -> datetime | None` — all datetimes naive UTC; cron evaluated in `tz`. Returns the latest fire time in `(now - grace, now]` if no backup started at or after it; else `None`.
  - `class GuardianScheduler(container: Container)`: `start() -> None` (adds a job per enabled, non-deleted source), `shutdown() -> None`, `sync_source(source: Source | None, source_id: int) -> None` (add/replace job id `f"source:{id}"`, or remove when `None`, disabled or deleted), `catch_up(now: datetime) -> list[int]` (enqueues one `SCHEDULED` backup per source with a missed fire time; returns source ids).
  - APScheduler `BackgroundScheduler(timezone=settings.display_tz)`; jobs use `CronTrigger.from_crontab(schedule, timezone=display_tz)` so `0 3 * * *` means 03:00 local; `coalesce=True`, `misfire_grace_time=settings.misfire_grace_seconds`; the job only calls `enqueue_backup(..., SCHEDULED)` and swallows `SourceUnavailable`.

- [ ] **Step 1: Write failing tests**

```python
def test_missed_within_grace():
    assert missed_fire_time("0 3 * * *", "America/Lima", last_started_at=None, now=utc(2026,11,12,8,30), grace_seconds=3600) == utc(2026,11,12,8,0)
def test_not_missed_when_backup_started_after_fire(): ... -> None
def test_not_missed_outside_grace(): now 10:30 UTC -> None
def test_sync_adds_replaces_removes_job(scheduler): # create -> job exists with new cron; disable -> removed; delete -> removed
def test_api_update_reschedules_without_restart(client, scheduler): PATCH schedule -> scheduler job trigger reflects new cron
def test_catch_up_enqueues_once_per_source(...)
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -m "not integration" -q` → PASS
- [ ] **Step 5: Commit** — `feat(scheduling): schedule backups from source config with missed-run catch-up`

---

### Task 9: Signed, expiring streaming downloads

**Files:**
- Create: `src/db_guardian/downloads/{__init__,signing,api}.py`, `tests/unit/downloads/test_signing.py`, `tests/unit/downloads/test_api.py`
- Modify: `src/db_guardian/app.py`

**Interfaces:**
- Consumes: `Backup`, `FileState`, `Settings.signing_key`, `Settings.download_ttl_seconds`.
- Produces:
  - `def sign(backup_id: int, expires_at: int, key: str) -> str` — hex HMAC-SHA256 of `f"{backup_id}:{expires_at}"`.
  - `def verify(backup_id: int, expires_at: int, sig: str, key: str, now: int) -> bool` — `hmac.compare_digest`, false when `now > expires_at`.
  - `POST /backups/{id}/download-url` → `{"url": "/downloads/{id}?exp=<unix>&sig=<hex>", "expires_at": "<display iso>"}`; 404 unknown; 410 when `file_state != PRESENT` or status not `SUCCESS`.
  - `GET /downloads/{id}?exp&sig` → streams the file (`FileResponse`, `media_type="application/gzip"`, `filename=<source_name>_<YYYYmmdd_HHMMSS>.sql.gz`); 403 invalid/expired signature (checked first, before any DB lookup); 404 unknown backup; 410 if not `PRESENT` **or the file is missing on disk**.

- [ ] **Step 1: Write failing tests**

```python
def test_roundtrip_download(client, success_backup):
    url = client.post(f"/backups/{b.id}/download-url").json()["url"]
    r = client.get(url); assert r.status_code == 200 and r.content == file_bytes
    assert r.headers["content-disposition"].endswith('.sql.gz"')
def test_tampered_sig_403(...); def test_other_backup_id_with_valid_sig_403(...)
def test_expired_403(client, freeze_now): ...           # now = exp + 1
def test_retention_deleted_after_signing_410(client, success_backup):   # Review Focus 5
    url = sign_url(); mark DELETED_BY_RETENTION and remove file; assert client.get(url).status_code == 410
def test_file_missing_on_disk_410(...)
def test_cannot_sign_failed_backup_410(...)
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -m "not integration" -q` → PASS
- [ ] **Step 5: Commit** — `feat(downloads): serve backups through signed expiring URLs`

---

### Task 10: Production packaging and docs

**Files:**
- Create: `docker-compose.yml`, `.env.example`, `README.md`, `tests/integration/test_end_to_end.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `docker-compose.yml`: service `db-guardian` built from `Dockerfile` (runtime target), `env_file: .env`, volume `./data:/data`, `ports: "8000:8000"`, `mem_limit: 512m`, `restart: unless-stopped`, `extra_hosts: ["host.docker.internal:host-gateway"]` (to reach the ETL MySQL on the same host).
  - `.env.example` listing every variable from the env table with comments, plus the one-liner to generate a Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  - `README.md`: purpose, deployment on the ETL machine, how to reach the ETL MySQL (`host.docker.internal` or the ETL's Docker network) and the intranet MySQL (LAN IP), required MySQL privileges for the backup user (`SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES` + `PROCESS` not needed thanks to `--no-tablespaces`), example source payloads for intranet (`["*"]`) and ETL (historical table list), running tests, and the accepted risk (ETL backups on the same disk).

- [ ] **Step 1: Write failing end-to-end integration test** — boots `create_app` with real runner against `mysql80`, `POST /sources` (tables `["*"]`), `POST /sources/{id}/backups`, waits for the queue, asserts `GET /backups/{id}` is `success` with 3 tables and rows 3/5/7, gets a signed URL and downloads a gzip that decompresses to text containing `CREATE TABLE \`customers\``.
- [ ] **Step 2: Run** `docker compose -f docker-compose.test.yml run --rm tests` → FAIL
- [ ] **Step 3: Fix any wiring gaps; write compose, `.env.example`, README.**
- [ ] **Step 4: Run** integration command → PASS; `pytest -m "not integration" -q` → PASS; `docker compose config` → valid.
- [ ] **Step 5: Commit** — `feat: add production compose, env template and README`
