# db-guardian Phase 2 (Clone) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clone a stored backup into another MySQL server (the staging VPS): a read-only preview that diffs the backup's schema against the target, and a key-protected background job that restores the backup, with permanent clone history.

**Architecture:** Builds on Phase 1. A new `clones/` feature package holds the key guard, the production-target guard, the pure schema diff, the `Clone` model, the runner and the API. Target I/O lives in `mysqlclient/` next to Phase 1's dump code: `target_schema()` (PyMySQL, TLS required) and `restore_gzip()` (streams `.sql.gz` → `mysql` client, TLS required, `DEFINER=` stripped). Clone jobs run on Phase 1's single-worker `WorkQueue`; the target password lives only in the job's closure.

**Tech Stack:** Same as Phase 1 (Python 3.12, FastAPI, SQLAlchemy, PyMySQL, MySQL 8.4 client in Docker). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-12-db-guardian-design.md` §7 (including §7.7) and §4 anti-goals.

## Global Constraints

- All artifacts in English; conventional commits; **no AI attribution / `Co-Authored-By` lines** (owner rule).
- Work on branch `feat/phase-2-clone` from `main`.
- Clone key: header `X-Clone-Key`, compared with `hmac.compare_digest`. Missing header → 401; wrong key → 403; `GUARDIAN_CLONE_KEY` unset → 503 `"clone disabled: GUARDIAN_CLONE_KEY is not set"`. Applies to preview and execute only.
- Target password: used in memory only, **never persisted, logged or returned**; error texts have it replaced by `***` (only when non-empty).
- Target connections require TLS: PyMySQL with TLS enabled and certificate verification off; `mysql` client with `ssl-mode=REQUIRED` in its option file.
- Target database must already exist → 422 `"target database '<name>' does not exist"`.
- Refuse targets matching a configured (non-deleted) source on host+port+database, aliases `localhost`, `127.0.0.1`, `::1`, `host.docker.internal` treated as one host, plus best-effort DNS resolution → 409.
- Tables only in the target are never touched (the dump only drops/creates its own tables).
- New env var: `GUARDIAN_CLONE_TIMEOUT_SECONDS` (default `14400`).
- Unit: `.venv/Scripts/python -m pytest -m "not integration" -q -W error`. Integration (WSL Docker, snap cannot read `/mnt/c`): `wsl -e bash -lc "rsync -a --delete --exclude .venv --exclude .git --exclude .superpowers --exclude .atl --exclude data /mnt/c/Users/danielnahun/Documents/git/personal/db-guardian/ ~/db-guardian-it/ && cd ~/db-guardian-it && docker compose -f docker-compose.test.yml run --rm --build tests; docker compose -f docker-compose.test.yml down -v"`.

## Review Focus

1. **Target user without `SET_USER_ID`/`SUPER`** restoring a full backup that has a view, trigger and procedure with `DEFINER=root@localhost`: must succeed (definers stripped); pinned in Task 4 integration.
2. **Binary logging on the target** (MySQL 8 default) with a non-SUPER user creating triggers/routines: may hit error 1419 (`log_bin_trust_function_creators`). Pinned by Task 4's integration test (the 8.0.46 container has binlog on). If it fails, the implementer rules: README documents `log_bin_trust_function_creators=1` on staging and the test container sets it explicitly — never silently.
3. **Target reachable but TLS unavailable/disabled**: connection must fail with a clear error, not fall back to plaintext; pinned in Task 3 (`ssl` required is asserted via `Ssl_cipher` non-empty).
4. **`mysql` dies mid-restore** (bad SQL, dropped connection, killed by timeout): clone `failed` with the stderr tail, worker survives, no partial state hidden; pinned in Task 4 (unit, fake client) and Task 6 (runner).
5. **Clone requested against production by an alias** (`127.0.0.1` vs source `localhost`, or the same DNS name): 409 before connecting; pinned in Task 1.

---

## File Structure

```
src/db_guardian/
  config.py                  # + clone_timeout_seconds
  container.py               # + target_reader, restorer, clone_runner
  app.py                     # + clones router; recovery also fails stale clones
  mysqlclient/
    connection.py            # (unchanged)
    dump.py                  # write_option_file(..., ssl_mode=None)
    metadata.py              # + TargetSchema, target_schema(conn)
    restore.py               # restore_gzip(), strip_definers()
  clones/
    __init__.py
    security.py              # require_clone_key dependency, redact()
    guard.py                 # matches_source(session, target) -> Source | None
    diff.py                  # normalize_create_sql, SchemaDiff, diff_schema, version_warnings
    schemas.py               # TargetIn, ClonePreviewOut, CloneOut, ClonePage
    models.py                # Clone
    runner.py                # CloneRunner
    service.py               # build_preview(), enqueue_clone()
    api.py                   # preview, execute, list, detail
tests/unit/clones/...  tests/unit/mysqlclient/test_restore.py
tests/integration/test_target.py  tests/integration/test_clone_end_to_end.py
```

---

### Task 1: Clone key guard, production guard, target schema

**Files:**
- Create: `src/db_guardian/clones/{__init__,security,guard,schemas}.py`, `tests/unit/clones/test_security.py`, `tests/unit/clones/test_guard.py`
- Modify: `src/db_guardian/config.py` (add `clone_timeout_seconds: int = Field(default=14400, ge=1)`)

**Interfaces:**
- Consumes: `Settings.clone_key`, `Source`, `get_container`.
- Produces:
  - `def require_clone_key(x_clone_key: str | None = Header(default=None), c: Container = Depends(get_container)) -> None` — 503/401/403 as in Global Constraints.
  - `def redact(message: str, secret: str) -> str` — replaces `secret` with `***` only when `secret` is non-empty.
  - `class TargetIn(BaseModel)`: `host: str (min 1)`, `port: int = 3306 (1..65535)`, `user: str (min 1)`, `password: str`, `database: str (min 1)`; `def to_connection(self) -> ConnectionInfo`.
  - `def matches_source(session: Session, target: ConnectionInfo) -> Source | None` — compares against non-deleted sources (enabled or not); hosts equal after lowercasing, or both in the local-alias set, or their `socket.getaddrinfo` IP sets intersect (resolution errors → treated as no match); ports equal; database names equal.

- [ ] **Step 1: Write failing tests**

```python
# test_security.py — mount a tiny FastAPI app with one route depending on require_clone_key
def test_unset_key_503(...): settings.clone_key=None -> 503, detail contains "GUARDIAN_CLONE_KEY"
def test_missing_header_401(...); def test_wrong_key_403(...); def test_right_key_passes(...)  # 200
def test_redact():
    assert redact("Access denied pw=s3cret", "s3cret") == "Access denied pw=***"
    assert redact("Access denied", "") == "Access denied"

# test_guard.py — sources: intranet@localhost:3306/intranet, etl@192.168.1.30:3306/etl, deleted@10.0.0.9:3306/old
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "LOCALHOST", "host.docker.internal", "::1"])
def test_local_aliases_match(session, host): assert matches_source(session, target(host, 3306, "intranet")).name == "intranet"
def test_same_ip_matches(...)                 # 192.168.1.30 / etl
def test_dns_resolution_matches(monkeypatch)  # patch socket.getaddrinfo: "etl.lan" -> 192.168.1.30
def test_different_database_no_match(...)     # localhost / staging -> None
def test_different_port_no_match(...)         # localhost:3307 / intranet -> None
def test_deleted_source_ignored(...)          # 10.0.0.9 / old -> None
def test_dns_failure_is_no_match(monkeypatch) # getaddrinfo raises socket.gaierror -> None
```

- [ ] **Step 2: Run** `pytest tests/unit/clones -q` → FAIL (modules missing)
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** unit suite → PASS
- [ ] **Step 5: Commit** — `feat(clones): add clone key guard and production target guard`

---

### Task 2: Schema diff and version warnings (pure)

**Files:**
- Create: `src/db_guardian/clones/diff.py`, `tests/unit/clones/test_diff.py`

**Interfaces:**
- Consumes: `parse_version` (Phase 1 `dump.py`).
- Produces:
  - `def normalize_create_sql(sql: str) -> str` — removes the table option `AUTO_INCREMENT=<n>` and collapses runs of whitespace; nothing else.
  - `@dataclass class SchemaDiff: missing_in_target: list[str]; structure_differs: list[str]; only_in_target: list[str]; matching: list[str]` (each sorted).
  - `def diff_schema(backup: dict[str, str], target: dict[str, str]) -> SchemaDiff` — both map table name → `CREATE TABLE` text.
  - `def version_warnings(source_version: str, target_version: str, backup_sql: list[str]) -> list[str]` — warning when the target is older (major, minor) than the source; warning when any backup SQL contains `utf8mb4_0900_` and the target is MariaDB or < 8.0 (text names the collation and says the restore will likely fail).

- [ ] **Step 1: Write failing tests**

```python
def test_auto_increment_ignored():
    a = "CREATE TABLE `t` (\n  `id` int NOT NULL AUTO_INCREMENT\n) ENGINE=InnoDB AUTO_INCREMENT=51 DEFAULT CHARSET=utf8mb4"
    b = a.replace("AUTO_INCREMENT=51", "AUTO_INCREMENT=7")
    assert normalize_create_sql(a) == normalize_create_sql(b)
    assert "NOT NULL AUTO_INCREMENT" in normalize_create_sql(a)  # column attribute kept
def test_diff_categories():
    d = diff_schema({"a": A, "b": B, "c": C}, {"b": B, "c": C_CHANGED, "z": Z})
    assert (d.missing_in_target, d.structure_differs, d.only_in_target, d.matching) == (["a"], ["c"], ["z"], ["b"])
def test_same_versions_no_warnings(): assert version_warnings("8.0.46", "8.0.46", [UTF8_0900_SQL]) == []
def test_older_target_warns(): assert any("older" in w for w in version_warnings("8.0.46", "5.7.41", []))
def test_0900_collation_into_57_warns(): assert any("utf8mb4_0900_ai_ci" in w for w in version_warnings("8.0.46", "5.7.41", [UTF8_0900_SQL]))
def test_0900_collation_into_mariadb_warns(): ... target "10.11.6-MariaDB"
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(clones): diff backup schema against target schema`

---

### Task 3: Read the target schema over TLS

**Files:**
- Modify: `src/db_guardian/mysqlclient/metadata.py`, `docker-compose.test.yml` (nothing new needed unless a fixture requires it), `tests/integration/conftest.py`
- Create: `tests/integration/test_target.py`

**Interfaces:**
- Consumes: `ConnectionInfo`, `quote_identifier`.
- Produces:
  - `class UnknownDatabaseError(ValueError)` with `database: str`.
  - `@dataclass class TargetSchema: server_version: str; tables: dict[str, str]` (name → `SHOW CREATE TABLE` text, base tables only).
  - `def target_schema(conn: ConnectionInfo) -> TargetSchema` — connects with TLS required (PyMySQL TLS on, no cert verification), maps MySQL error 1049 to `UnknownDatabaseError`.
  - Type alias `TargetReader = Callable[[ConnectionInfo], TargetSchema]`.
  - Integration fixture `staging(sql)` in `tests/integration/conftest.py` (mysql80 only): as root creates a fresh database `staging_<8 hex>` with `customers(id INT PRIMARY KEY, full_name VARCHAR(50))` (different from the source) and `extra_only(id INT PRIMARY KEY)` holding 1 row; creates user `stager`@`%` (once, password `st@ge"r#1`) with `SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES, CREATE VIEW, SHOW VIEW, TRIGGER, CREATE ROUTINE, ALTER ROUTINE, EVENT, LOCK TABLES` on that database only; yields its `ConnectionInfo`; drops the database afterwards.

- [ ] **Step 1: Write failing integration tests** (mysql80 only)

```python
def test_reads_target_tables(staging):
    schema = target_schema(staging)
    assert set(schema.tables) == {"customers", "extra_only"}
    assert "full_name" in schema.tables["customers"] and schema.server_version.startswith("8.0")
def test_connection_uses_tls(staging):          # Review Focus 3: inside target_schema's connection helper
    assert target_session_cipher(staging) != ""  # helper in the same module: SHOW STATUS LIKE 'Ssl_cipher'
def test_unknown_database(staging):
    with pytest.raises(UnknownDatabaseError): target_schema(replace(staging, database="nope_db"))
```

  `target_session_cipher(conn) -> str` is a small public helper in `metadata.py` using the same TLS connect path.

- [ ] **Step 2: Run** integration command → FAIL (import errors)
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** integration → PASS; unit suite → PASS
- [ ] **Step 5: Commit** — `feat(mysqlclient): read target schema over TLS`

---

### Task 4: Restore a backup into a target

**Files:**
- Create: `src/db_guardian/mysqlclient/restore.py`, `tests/unit/mysqlclient/test_restore.py`, `tests/integration/test_restore.py`
- Modify: `src/db_guardian/mysqlclient/dump.py` (`write_option_file(conn, directory, ssl_mode: str | None = None)` adds `ssl-mode=<value>` when given; existing tests keep passing)

**Interfaces:**
- Consumes: `write_option_file`, `DUMP_WORKDIR_PREFIX` (reuse the same prefix so Phase 1's startup sweep also removes leftover restore option files), `DumpError`.
- Produces:
  - `class RestoreError(RuntimeError)` with `stderr_tail: str` (same shape as `DumpError`).
  - `def strip_definers(line: bytes) -> bytes` — removes `DEFINER=`<user>`@`<host>`` (backtick-quoted, any content without backticks) wherever it appears in the line; lines without `DEFINER` returned unchanged (fast path).
  - `def restore_gzip(conn: ConnectionInfo, source: Path, *, on_progress: Callable[[float], None] | None = None, timeout_seconds: float | None = None) -> None` — runs `mysql --defaults-extra-file=<opt with ssl-mode=REQUIRED> --default-character-set=utf8mb4 <database>`, reads `source` with `gzip.open` line by line, writes `strip_definers(line)` to stdin, calls `on_progress(compressed_bytes_read / file_size)` at most once per 1%, closes stdin, waits; non-zero exit, broken pipe or timeout → `RestoreError` with stderr tail (timeout message: `"mysql restore timed out after <n>s and was killed"`). stderr to a temp file; process killed on any exception; option file deleted in `finally`. Same `_spawn` seam pattern as `dump.py` (module-level `_spawn`, `MYSQL = "mysql"`).
  - Type alias `Restorer = Callable[..., None]` matching `restore_gzip`.

- [ ] **Step 1: Write failing tests**

```python
# unit
def test_strip_definers():
    line = b"/*!50013 DEFINER=`root`@`localhost` SQL SECURITY DEFINER */ VIEW `v` AS select 1;\n"
    assert strip_definers(line) == b"/*!50013  SQL SECURITY DEFINER */ VIEW `v` AS select 1;\n"
    assert strip_definers(b"CREATE DEFINER=`app`@`%` PROCEDURE `p`()") == b"CREATE  PROCEDURE `p`()"
    assert strip_definers(b"INSERT INTO t VALUES ('DEFINER is text');") == b"INSERT INTO t VALUES ('DEFINER is text');"
def test_option_file_ssl_mode(tmp_path): "ssl-mode=REQUIRED" line present only when ssl_mode given
def test_restore_feeds_stripped_sql_and_reports_progress(fake_mysql)   # fake client (python script) copies stdin to a file; assert content has no DEFINER, progress last value == 1.0, values non-decreasing
def test_restore_failure_raises_with_stderr(fake_mysql)               # fake exits 1 with "ERROR 1064 (42000) at line 3"
def test_restore_timeout_kills(fake_mysql)                            # fake sleeps; timeout_seconds=1 -> RestoreError "timed out after 1s"
def test_restore_removes_option_file(...)

# integration (mysql80): backup of shop via dump_to_gzip, restore into staging as stager
def test_full_restore_as_unprivileged_user(conn80, staging, sql, tmp_path):   # Review Focus 1 & 2
    dump_to_gzip(conn80, None, server_version(conn80), tmp_path / "b.sql.gz")
    restore_gzip(staging, tmp_path / "b.sql.gz")
    assert counts in staging == {"customers": 3, "orders": 5, "hist_sales": 7}
    assert "full_name" not in SHOW CREATE TABLE customers           # recreated from backup
    assert SELECT COUNT(*) FROM extra_only == 1                       # untouched
    assert view v_orders, trigger trg_orders_total, procedure count_orders exist in staging
def test_restore_bad_credentials_raises(staging, tmp_path): ... RestoreError mentioning "Access denied"
```

  `conn80` = the `conn` fixture restricted to mysql80 (add a fixture). If the integration test hits error 1419, apply the Review Focus 2 rule and ledger it.

- [ ] **Step 2: Run** unit + integration → FAIL
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** — `feat(mysqlclient): restore gzip backups into a target over TLS`

---

### Task 5: Clone preview endpoint

**Files:**
- Create: `src/db_guardian/clones/service.py`, `src/db_guardian/clones/api.py`, `tests/unit/clones/test_preview_api.py`
- Modify: `src/db_guardian/clones/schemas.py`, `src/db_guardian/container.py` (`target_reader: TargetReader = target_schema` kwarg/field), `src/db_guardian/app.py` (include router)

**Interfaces:**
- Consumes: `require_clone_key`, `TargetIn`, `matches_source`, `diff_schema`, `version_warnings`, `TargetReader`, `UnknownDatabaseError`, `Backup`/`BackupTable`/`FileState`, `redact`.
- Produces:
  - `class ClonePreviewOut(BaseModel)`: `backup_id: int; source_version: str | None; target_version: str; missing_in_target: list[MissingTableOut{name, create_sql}]; structure_differs: list[DifferingTableOut{name, backup_create_sql, target_create_sql}]; only_in_target: list[str]; matching: list[str]; warnings: list[str]`.
  - `class CloneRejected(Exception)` with `status_code: int` and `detail: str`.
  - `def build_preview(container: Container, backup_id: int, target: ConnectionInfo) -> ClonePreviewOut` — order: backup exists (404) → `SUCCESS` + `PRESENT` + file on disk (410) → `matches_source` (409 `"target matches backup source '<name>'; refusing to clone into production"`) → `target_reader` (`UnknownDatabaseError` → 422; any other error → 502 with redacted message) → diff + warnings. Raises `CloneRejected`.
  - `POST /backups/{backup_id}/clone-preview` (body `TargetIn`, depends on `require_clone_key`) → 200 `ClonePreviewOut`.

- [ ] **Step 1: Write failing tests** (fake `target_reader` on the container; backup seeded with 3 `BackupTable` rows and a file)

```python
def test_preview_requires_key(client): 401 without header (with clone_key set in settings fixture)
def test_preview_report(client): fake target {"customers": DIFFERENT, "extra_only": X, "orders": SAME_AS_BACKUP}
    body: missing_in_target == [{"name": "hist_sales", "create_sql": <backup sql>}], structure_differs names == ["customers"],
          only_in_target == ["extra_only"], matching == ["orders"], warnings == [], target_version == "8.0.46"
def test_preview_refuses_production_alias(client): source at localhost/intranet; target 127.0.0.1/intranet -> 409; reader never called
def test_preview_backup_file_gone_410(client)
def test_preview_unknown_target_db_422(client)
def test_preview_target_error_502_redacted(client): reader raises OSError("... password 'pw-123' ...") -> 502, "pw-123" not in body
def test_preview_writes_nothing(client, container): no Clone rows, no files changed
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** unit suite → PASS.
- [ ] **Step 5: Commit** — `feat(clones): add clone preview endpoint`

---

### Task 6: Clone model, runner, execute and history API, recovery

**Files:**
- Create: `src/db_guardian/clones/models.py`, `src/db_guardian/clones/runner.py`, `tests/unit/clones/test_runner.py`, `tests/unit/clones/test_clone_api.py`
- Modify: `src/db_guardian/models.py`, `src/db_guardian/clones/{service,api,schemas}.py`, `src/db_guardian/container.py` (`restorer` kwarg/field default `partial(restore_gzip, timeout_seconds=settings.clone_timeout_seconds)`, `clone_runner`), `src/db_guardian/backups/recovery.py` (also fail stale clones), `src/db_guardian/app.py`

**Interfaces:**
- Consumes: `build_preview`, `CloneRejected`, `Restorer`, `WorkQueue.submit`, `INTERRUPTED`, `redact`.
- Produces:
  - `class CloneStatus(StrEnum)`: `QUEUED, RUNNING, SUCCESS, FAILED` (lowercase values).
  - Model `Clone` (table `clones`): `id, backup_id (FK backups.id, indexed), source_name, target_host, target_port, target_database, target_user, status, error, created_at, started_at, finished_at, duration_seconds, progress (float 0..1, default 0), tables (JSON list[str]), preview (JSON: counts {missing_in_target, structure_differs, only_in_target, matching} + warnings)`. No password column.
  - `class CloneRunner(session_factory, target_reader: TargetReader, restorer: Restorer)` with `run(clone_id: int, target: ConnectionInfo) -> None`: set `RUNNING`/`started_at`; recompute diff vs live target and store `preview`; `restorer(target, Path(backup.file_path), on_progress=...)` persisting `progress` when it advances ≥ 0.05 (and 1.0 at the end); success → `tables` = backup table names, `SUCCESS`; any exception → `FAILED`, `error` = redacted message (+ stderr tail); never raises.
  - `def enqueue_clone(container: Container, backup_id: int, target: ConnectionInfo) -> Clone` — runs `build_preview` first (same rejections, so failures surface synchronously), creates a `QUEUED` row, submits key `f"clone:{clone_id}"` with a closure holding `target` (password only in memory).
  - `POST /backups/{backup_id}/clones` (key required) → 202 `CloneOut`.
  - `GET /clones?backup_id&status&limit=50(max 200)&offset=0` → `{"items": [CloneOut], "total"}`, newest first; `GET /clones/{id}` → `CloneOut`. No key required.
  - `CloneOut`: all `Clone` columns except none secret; timestamps via `to_display`.
  - `recover_interrupted` also marks `QUEUED`/`RUNNING` clones `FAILED` with `INTERRUPTED` (clones are not caught up: the password is gone). Returns the total count.

- [ ] **Step 1: Write failing tests**

```python
# runner (fakes)
def test_clone_success_records(...): status success, progress == 1.0, tables == backup table names, preview counts stored, target password nowhere in the row (serialize all columns)
def test_clone_restore_failure(...): restorer raises RestoreError("mysql exited with code 1", "ERROR 1064 ...") -> failed, "1064" in error, worker survives (run returns)
def test_clone_progress_persisted(...): restorer calls on_progress(0.01..1.0 in 0.01 steps) -> progress column observed >= 0.05 increments (record via listener) and final 1.0
# api
def test_execute_requires_key(...); def test_execute_returns_202_queued(...): status "queued", target_host/database echoed, no "password" key in body
def test_execute_rejections_are_synchronous(...): production alias -> 409, no Clone row created
def test_execute_runs_on_worker(...): start queue, join -> clone success via fake restorer
def test_history_list_and_detail(...): newest first, filter by backup_id, no key needed
def test_recovery_fails_stale_clones(...): queued+running clones -> failed INTERRUPTED
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** unit suite → PASS.
- [ ] **Step 5: Commit** — `feat(clones): run clones in the background with history`

---

### Task 7: End-to-end clone, docs

**Files:**
- Create: `tests/integration/test_clone_end_to_end.py`
- Modify: `README.md` (Clone section, target privileges, TLS/firewall note, `GUARDIAN_CLONE_KEY`, `GUARDIAN_CLONE_TIMEOUT_SECONDS`), `.env.example` (add `GUARDIAN_CLONE_TIMEOUT_SECONDS=14400`, clarify `GUARDIAN_CLONE_KEY`)

**Interfaces:**
- Consumes: everything above.
- Produces: an integration test that drives the whole flow through the API against mysql80: create source `shop` (`["*"]`), back it up, `POST /backups/{id}/clone-preview` with header → `missing_in_target` contains `orders` and `hist_sales`, `structure_differs` == `["customers"]`, `only_in_target` == `["extra_only"]`; `POST /backups/{id}/clones` → 202; `queue.join()`; `GET /clones/{id}` → `success`, `progress` 1.0; staging now has rows 3/5/7 and `extra_only` still has 1 row; a second preview reports `missing_in_target == []`, `structure_differs == []`.

- [ ] **Step 1: Write the test.** **Step 2: Run** integration → expect FAIL only if wiring gaps exist (record honestly if it passes first time).
- [ ] **Step 3: Fix gaps; write README section** (how to expose the VPS port safely, `stager` grants from Task 3, `log_bin_trust_function_creators` note if Task 4 ruled it necessary).
- [ ] **Step 4: Run** integration + unit → PASS.
- [ ] **Step 5: Commit** — `feat(clones): end-to-end clone test and documentation`
