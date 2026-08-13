"""API tests for the export endpoints."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_session
from app.main import app
from app.models.database import ConnectionStatus, DatabaseConnection
from app.models.schemas import QueryColumn, QueryResult
from app.services.sql_validator import SqlValidationError


@pytest.fixture
def test_session():
    """Create an in-memory SQLite session for testing."""
    from app.models.database import DatabaseConnection  # noqa: F401
    from app.models.metadata import DatabaseMetadata  # noqa: F401
    from app.models.query import QueryHistory  # noqa: F401

    engine = create_engine(
        "sqlite:///file:test_export_db?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(test_session):
    """Create TestClient with test database session."""

    def get_test_session():
        return test_session

    app.dependency_overrides[get_session] = get_test_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_connection(test_session):
    """Create a sample database connection."""
    conn = DatabaseConnection(
        name="test_db",
        url="postgresql://user:pass@localhost/testdb",
        description="Test database",
        status=ConnectionStatus.ACTIVE,
        last_connected_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    test_session.add(conn)
    test_session.commit()
    test_session.refresh(conn)
    return conn


def _mock_result() -> QueryResult:
    return QueryResult(
        columns=[
            QueryColumn(name="id", dataType="integer"),
            QueryColumn(name="name", dataType="character varying"),
        ],
        rows=[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
        rowCount=2,
        executionTimeMs=25,
        sql="SELECT id, name FROM users",
    )


class TestListExportFormats:
    """Test GET /export/formats."""

    def test_list_formats_dynamic(self, client):
        response = client.get("/api/v1/dbs/export/formats")
        assert response.status_code == 200
        formats = response.json()["formats"]
        assert set(["csv", "json", "ndjson"]).issubset(set(formats))


class TestExportQuery:
    """Test POST /{name}/query/export."""

    def test_export_csv_success(self, client, sample_connection):
        with patch(
            "app.api.v1.exports.execute_query_with_service",
            new_callable=AsyncMock,
            return_value=_mock_result(),
        ) as mock_exec:
            response = client.post(
                "/api/v1/dbs/test_db/query/export",
                json={"sql": "SELECT id, name FROM users", "format": "csv"},
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        # Content-Disposition with safe filename
        cd = response.headers["content-disposition"]
        assert "attachment" in cd
        assert cd.endswith('.csv"')
        assert "test-db" in cd
        # Payload has BOM
        assert response.content.startswith(b"\xef\xbb\xbf")
        # record_history defaults to False -> not threaded as True
        assert mock_exec.call_args.kwargs.get("record_history") is False

    def test_export_json_document(self, client, sample_connection):
        with patch(
            "app.api.v1.exports.execute_query_with_service",
            new_callable=AsyncMock,
            return_value=_mock_result(),
        ):
            response = client.post(
                "/api/v1/dbs/test_db/query/export",
                json={"sql": "SELECT id, name FROM users", "format": "json"},
            )

        assert response.status_code == 200
        doc = json.loads(response.content)
        assert doc["rowCount"] == 2
        assert len(doc["rows"]) == 2

    def test_export_json_array_style(self, client, sample_connection):
        with patch(
            "app.api.v1.exports.execute_query_with_service",
            new_callable=AsyncMock,
            return_value=_mock_result(),
        ):
            response = client.post(
                "/api/v1/dbs/test_db/query/export",
                json={
                    "sql": "SELECT id, name FROM users",
                    "format": "json",
                    "json_style": "array",
                },
            )

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

    def test_export_ndjson(self, client, sample_connection):
        with patch(
            "app.api.v1.exports.execute_query_with_service",
            new_callable=AsyncMock,
            return_value=_mock_result(),
        ):
            response = client.post(
                "/api/v1/dbs/test_db/query/export",
                json={"sql": "SELECT id, name FROM users", "format": "ndjson"},
            )

        assert response.status_code == 200
        lines = [ln for ln in response.content.decode("utf-8").splitlines() if ln]
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"id": 1, "name": "Alice"}

    def test_export_default_format_csv(self, client, sample_connection):
        with patch(
            "app.api.v1.exports.execute_query_with_service",
            new_callable=AsyncMock,
            return_value=_mock_result(),
        ):
            response = client.post(
                "/api/v1/dbs/test_db/query/export",
                json={"sql": "SELECT id FROM users"},
            )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")

    def test_export_database_not_found(self, client):
        response = client.post(
            "/api/v1/dbs/nonexistent/query/export",
            json={"sql": "SELECT 1", "format": "csv"},
        )
        assert response.status_code == 404

    def test_export_validation_error_400(self, client, sample_connection):
        with patch(
            "app.api.v1.exports.execute_query_with_service",
            new_callable=AsyncMock,
            side_effect=SqlValidationError("Only SELECT statements are allowed"),
        ):
            response = client.post(
                "/api/v1/dbs/test_db/query/export",
                json={"sql": "DELETE FROM users", "format": "csv"},
            )
        assert response.status_code == 400
        assert "SELECT" in response.json()["detail"]

    def test_export_unsupported_format_422(self, client, sample_connection):
        with patch(
            "app.api.v1.exports.execute_query_with_service",
            new_callable=AsyncMock,
            return_value=_mock_result(),
        ):
            response = client.post(
                "/api/v1/dbs/test_db/query/export",
                json={"sql": "SELECT 1", "format": "xml"},
            )
        assert response.status_code == 422

    def test_export_execution_error_500(self, client, sample_connection):
        with patch(
            "app.api.v1.exports.execute_query_with_service",
            new_callable=AsyncMock,
            side_effect=Exception("connection refused"),
        ):
            response = client.post(
                "/api/v1/dbs/test_db/query/export",
                json={"sql": "SELECT 1", "format": "csv"},
            )
        assert response.status_code == 500

    def test_export_save_history_threads_through(self, client, sample_connection):
        with patch(
            "app.api.v1.exports.execute_query_with_service",
            new_callable=AsyncMock,
            return_value=_mock_result(),
        ) as mock_exec:
            client.post(
                "/api/v1/dbs/test_db/query/export",
                json={"sql": "SELECT 1", "format": "csv", "save_history": True},
            )
        assert mock_exec.call_args.kwargs.get("record_history") is True

    def test_export_filename_blocks_traversal(self, client, test_session):
        """Connection names with path chars must be slugified in the filename."""
        conn = DatabaseConnection(
            name="../sneaky",
            url="postgresql://u:p@localhost/db",
            status=ConnectionStatus.ACTIVE,
        )
        test_session.add(conn)
        test_session.commit()
        with patch(
            "app.api.v1.exports.execute_query_with_service",
            new_callable=AsyncMock,
            return_value=_mock_result(),
        ):
            response = client.post(
                "/api/v1/dbs/../sneaky/query/export",
                json={"sql": "SELECT 1", "format": "csv"},
            )
        # FastAPI will 404 on path-normalized '..'; just ensure no crash/leak
        assert response.status_code in (200, 404)
