"""Test fixtures.

The whole suite runs offline against a temporary SQLite database and the
captured real NHTSA fixtures in tests/fixtures/. No test performs a live
external request: HTTP layers are monkeypatched.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

FIXTURES = BACKEND_DIR / "tests" / "fixtures"
REAL_PDF = FIXTURES / "RCLRPT-26V111-4068.pdf"
REAL_API_JSON = FIXTURES / "nhtsa_26V111_api.json"

# Configure the app BEFORE importing it so the module-level engine binds to
# the temporary test database.
_TMP_DB_DIR = BACKEND_DIR / "data" / "test"
_TMP_DB_DIR.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB_DIR / 'test_routeshield.db'}"
os.environ["AUTO_REFRESH_RECALL"] = "false"
os.environ["ROUTESHIELD_MANAGER_USERNAME"] = "fleet.manager"
os.environ["ROUTESHIELD_MANAGER_PASSWORD"] = "TestPassword123!"
os.environ["ROUTESHIELD_AUTH_SECRET"] = "test-secret-key-not-for-production"
os.environ["ENVIRONMENT"] = "test"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture
def db():
    """A fresh, seeded database for each test."""
    from app import models
    from app.db import Base, SessionLocal, engine
    from app.seed import seed_all

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        seed_all(session)
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def client(db):
    """HTTP test client. Lifespan seeding is skipped because `db` already
    seeded the same database file."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture
def manager_headers(client):
    resp = client.post(
        "/api/auth/login",
        json={"username": "fleet.manager", "password": "TestPassword123!"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def real_pdf_bytes() -> bytes:
    return REAL_PDF.read_bytes()


@pytest.fixture
def real_api_payload() -> dict:
    return json.loads(REAL_API_JSON.read_text())


@pytest.fixture
def incident_id(client) -> int:
    resp = client.get("/api/incidents")
    assert resp.status_code == 200
    return resp.json()[0]["id"]
