from fastapi import Request

from db_guardian.container import Container


def get_container(request: Request) -> Container:
    return request.app.state.container
