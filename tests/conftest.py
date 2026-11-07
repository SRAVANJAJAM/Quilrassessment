import httpx
import pytest

from quilr_assessment.app import create_app
from quilr_assessment.auth import issue_demo_token
from quilr_assessment.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        jwt_secret="test-secret-that-is-at-least-32-bytes",
        tenant_keys={"test-key": "tenant-a", "other-key": "tenant-b"},
        database_path=str(tmp_path / "usage.db"),
        _env_file=None,
    )


@pytest.fixture
def tokens(settings):
    return {role: issue_demo_token(role, settings) for role in ("viewer", "admin")}


@pytest.fixture
def app_client(settings):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory(handler):
        app = create_app(settings, transport=httpx.MockTransport(handler))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                yield client, app

    return factory
