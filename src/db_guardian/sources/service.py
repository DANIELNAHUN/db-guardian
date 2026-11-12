from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db_guardian.crypto import SecretBox
from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.sources.models import Source
from db_guardian.sources.schemas import SourceCreate, SourceUpdate
from db_guardian.timefmt import utcnow


class NotFound(LookupError):
    pass


class NameConflict(ValueError):
    pass


class SourceService:
    def __init__(self, session: Session, box: SecretBox, default_retention: int = 3) -> None:
        self._session = session
        self._box = box
        self._default_retention = default_retention

    def create(self, data: SourceCreate) -> Source:
        fields = data.model_dump(exclude={"password", "retention"})
        source = Source(
            **fields,
            password_enc=self._box.encrypt(data.password),
            retention=data.retention or self._default_retention,
        )
        self._session.add(source)
        self._commit()
        return source

    def update(self, source_id: int, data: SourceUpdate) -> Source:
        source = self.get(source_id)
        changes = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
        password = changes.pop("password", None)
        if password is not None:
            source.password_enc = self._box.encrypt(password)
        for key, value in changes.items():
            setattr(source, key, value)
        self._commit()
        return source

    def get(self, source_id: int) -> Source:
        source = self._session.get(Source, source_id)
        if source is None or source.deleted_at is not None:
            raise NotFound(f"source {source_id} not found")
        return source

    def list(self) -> list[Source]:
        stmt = select(Source).where(Source.deleted_at.is_(None)).order_by(Source.id)
        return list(self._session.scalars(stmt))

    def delete(self, source_id: int) -> None:
        source = self.get(source_id)
        source.deleted_at = utcnow()
        source.enabled = False
        self._commit()

    def connection(self, source: Source) -> ConnectionInfo:
        return ConnectionInfo(
            host=source.host,
            port=source.port,
            user=source.user,
            password=self._box.decrypt(source.password_enc),
            database=source.database,
        )

    def _commit(self) -> None:
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise NameConflict("a source with this name already exists") from exc
