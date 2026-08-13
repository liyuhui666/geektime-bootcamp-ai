# Database Query Tool Backend

FastAPI backend for the Database Query Tool.

## Setup

```bash
# Install dependencies (Python 3.12 managed automatically by uv)
uv sync --extra dev

# Configure environment
cp .env.example .env   # then set OPENAI_API_KEY

# Apply database migrations
uv run alembic upgrade head

# Start the dev server
uv run uvicorn app.main:app --reload
```

The API runs at `http://localhost:8000`; interactive docs at `/docs`.

## Project Structure

- `app/main.py` — FastAPI application entry point
- `app/config.py` — Pydantic Settings configuration
- `app/database.py` — SQLite database setup
- `app/models/` — SQLModel entities and Pydantic schemas
- `app/adapters/` — Database adapters (PostgreSQL, MySQL) + registry
- `app/services/` — Business logic (query, metadata, nl2sql, validation)
- `app/api/v1/` — API route handlers
- `tests/` — pytest test suite
- `alembic/` — Database migrations
