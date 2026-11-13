from pathlib import Path

import pytest

from db_guardian.backups.models import Backup, BackupStatus, BackupTable, BackupTrigger, FileState
from db_guardian.mysqlclient.metadata import TargetSchema
from db_guardian.sources.models import Source

KEY = {"X-Clone-Key": "test-clone-key"}
CUSTOMERS = "CREATE TABLE `customers` (\n  `id` int NOT NULL,\n  `name` varchar(100)\n) ENGINE=InnoDB"
CUSTOMERS_OLD = "CREATE TABLE `customers` (\n  `id` int NOT NULL,\n  `full_name` varchar(50)\n) ENGINE=InnoDB"
ORDERS = "CREATE TABLE `orders` (\n  `id` int NOT NULL\n) ENGINE=InnoDB AUTO_INCREMENT=6"
HIST = "CREATE TABLE `hist_sales` (\n  `id` int NOT NULL\n) ENGINE=InnoDB"
EXTRA = "CREATE TABLE `extra_only` (\n  `id` int NOT NULL\n) ENGINE=InnoDB"


class FakeReader:
    def __init__(self):
        self.calls = []
        self.error = None
        self.schema = TargetSchema(
            server_version="8.0.46",
            tables={"customers": CUSTOMERS_OLD, "orders": ORDERS.replace("=6", "=2"), "extra_only": EXTRA},
        )

    def __call__(self, conn):
        self.calls.append(conn)
        if self.error:
            raise self.error
        return self.schema


@pytest.fixture
def reader(container):
    fake = FakeReader()
    container.target_reader = fake
    container.clone_runner.target_reader = fake
    return fake


@pytest.fixture
def backup(container):
    path = container.settings.backups_dir / "1" / "b.sql.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"gz")
    with container.session_factory() as s:
        source = Source(name="intranet", host="localhost", port=3306, user="u", password_enc="x",
                        database="intranet", tables=["*"], schedule="0 3 * * *", retention=3)
        s.add(source)
        s.flush()
        b = Backup(source_id=source.id, source_name="intranet", trigger=BackupTrigger.MANUAL,
                   status=BackupStatus.SUCCESS, file_state=FileState.PRESENT, file_path=str(path),
                   server_version="8.0.46")
        b.tables = [BackupTable(name=n, row_count=1, create_sql=sql)
                    for n, sql in (("customers", CUSTOMERS), ("orders", ORDERS), ("hist_sales", HIST))]
        s.add(b)
        s.commit()
        return b
