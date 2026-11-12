from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Response, status

from db_guardian.container import Container
from db_guardian.deps import get_container
from db_guardian.sources.models import Source
from db_guardian.sources.schemas import SourceCreate, SourceOut, SourceUpdate
from db_guardian.sources.service import NameConflict, NotFound, SourceService
from db_guardian.timefmt import to_display

router = APIRouter(prefix="/sources", tags=["sources"])


def get_service(container: Container = Depends(get_container)) -> Iterator[SourceService]:
    with container.session_factory() as session:
        yield SourceService(session, container.box, container.settings.default_retention)


def to_out(source: Source, tz: str) -> SourceOut:
    return SourceOut(
        id=source.id,
        name=source.name,
        host=source.host,
        port=source.port,
        user=source.user,
        database=source.database,
        tables=source.tables,
        schedule=source.schedule,
        retention=source.retention,
        enabled=source.enabled,
        has_password=bool(source.password_enc),
        created_at=to_display(source.created_at, tz),
        updated_at=to_display(source.updated_at, tz),
    )


def not_found(exc: NotFound) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


def conflict(exc: NameConflict) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("", response_model=list[SourceOut])
def list_sources(
    service: SourceService = Depends(get_service), c: Container = Depends(get_container)
):
    return [to_out(s, c.settings.display_tz) for s in service.list()]


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
def create_source(
    data: SourceCreate,
    service: SourceService = Depends(get_service),
    c: Container = Depends(get_container),
):
    try:
        source = service.create(data)
    except NameConflict as exc:
        raise conflict(exc) from exc
    c.on_source_changed(source, source.id)
    return to_out(source, c.settings.display_tz)


@router.get("/{source_id}", response_model=SourceOut)
def get_source(
    source_id: int,
    service: SourceService = Depends(get_service),
    c: Container = Depends(get_container),
):
    try:
        return to_out(service.get(source_id), c.settings.display_tz)
    except NotFound as exc:
        raise not_found(exc) from exc


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(
    source_id: int,
    data: SourceUpdate,
    service: SourceService = Depends(get_service),
    c: Container = Depends(get_container),
):
    try:
        source = service.update(source_id, data)
    except NotFound as exc:
        raise not_found(exc) from exc
    except NameConflict as exc:
        raise conflict(exc) from exc
    c.on_source_changed(source, source.id)
    return to_out(source, c.settings.display_tz)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(
    source_id: int,
    service: SourceService = Depends(get_service),
    c: Container = Depends(get_container),
):
    try:
        service.delete(source_id)
    except NotFound as exc:
        raise not_found(exc) from exc
    c.on_source_changed(None, source_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
