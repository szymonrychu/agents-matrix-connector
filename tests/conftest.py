import httpx
import psycopg
import pytest
from agents_common.db.pool import open_pool
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient
from api_helpers import PACKAGE, PREFIX, make_settings

from agents_matrix_connector.api.app import create_api_app

pytest_plugins = ["agents_common.testing"]


@pytest.fixture
def url(migrated_db):
    return migrated_db(PACKAGE)


@pytest.fixture
async def conn(url):
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as c:
        yield c


@pytest.fixture
async def pool(url):
    p = await open_pool(url)
    yield p
    await p.close()


@pytest.fixture
def fake():
    return FakeOrchestrator()


@pytest.fixture
async def orchestrator(fake):
    transport = httpx.ASGITransport(app=fake.app)
    async with OrchestratorClient("http://orch", fake.token, transport=transport) as c:
        yield c


@pytest.fixture
async def api(pool, orchestrator):
    app = create_api_app(pool, orchestrator, make_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api" + PREFIX
    ) as c:
        yield c
