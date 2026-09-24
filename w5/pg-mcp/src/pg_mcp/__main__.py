"""Main entry point for PostgreSQL MCP Server.

This module provides the CLI entry point for running the MCP server.
Two transports are supported, selected via MCP_TRANSPORT:

- stdio (default): JSON-RPC over stdin/stdout, for MCP clients that spawn
  this process locally (Claude Code/Desktop, MCP Inspector).
- http: streamable-HTTP endpoint at /mcp, for remote MCP clients.
  Binds to MCP_HTTP_HOST (default 127.0.0.1; set 0.0.0.0 to accept remote
  connections) and MCP_HTTP_PORT (default 8000). Set MCP_HTTP_TOKEN to
  require authentication on every request: clients may present the token
  via an "Authorization: Bearer <token>" header, or clients that cannot
  set custom headers may append "?access_token=<token>" to the URL.
  Without MCP_HTTP_TOKEN the endpoint trusts network-level access control.
"""

import hmac
import os
from pathlib import Path
from urllib.parse import parse_qs

import anyio
import uvicorn
from dotenv import load_dotenv
from starlette.types import ASGIApp, Receive, Scope, Send

from pg_mcp.server import mcp


class BearerTokenMiddleware:
    """ASGI middleware enforcing a static bearer token on the HTTP endpoint.

    Accepts the token from either location:

    - ``Authorization: Bearer <token>`` header (scheme matching is
      case-insensitive per RFC 7235)
    - ``?access_token=<token>`` query parameter, for MCP clients that
      cannot set custom request headers. Note the token then appears in
      access logs - prefer the header when the client supports it.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    def _authorized(self, scope: Scope) -> bool:
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:].strip(), self.token):
            return True
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        return any(hmac.compare_digest(v, self.token) for v in query.get("access_token", []))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self._authorized(scope):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


def main() -> None:
    """Main entry point for the PostgreSQL MCP Server.

    The server lifecycle is managed through the lifespan context manager,
    which handles:
    - Configuration loading
    - Database connection pool creation
    - Schema cache initialization
    - Service component setup
    - Graceful shutdown

    Example:
        Run the server (stdio, for local MCP clients):
        >>> python -m pg_mcp

        Run with environment variables:
        >>> DATABASE_HOST=localhost DATABASE_NAME=mydb python -m pg_mcp

        Run as a network service (for remote MCP clients):
        >>> MCP_TRANSPORT=http MCP_HTTP_TOKEN=secret python -m pg_mcp
    """
    # Load .env into the process environment before any Settings is built.
    # The nested BaseSettings classes (OpenAIConfig, DatabaseConfig, ...) each
    # use their own env_prefix and do NOT inherit Settings' env_file=".env",
    # so this is what makes the documented .env workflow actually work.
    # Resolved from this file's location so the launch CWD doesn't matter.
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        anyio.run(mcp.run_stdio_async)
    elif transport == "http":
        host = os.environ.get("MCP_HTTP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_HTTP_PORT", "8000"))
        token = os.environ.get("MCP_HTTP_TOKEN", "").strip()

        # The official SDK's FastMCP has no run_http_async. Build the ASGI app
        # ourselves and serve it with uvicorn. Lifespan note: the Starlette
        # app returned by streamable_http_app() only runs the session manager;
        # the pg_mcp lifespan runs inside the low-level MCP server when a
        # client session starts (see mcp.server.streamable_http_manager), so
        # no extra lifespan composition is needed here.
        http_app = mcp.streamable_http_app()
        app = BearerTokenMiddleware(http_app, token) if token else http_app
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
        anyio.run(server.serve)
    else:
        raise SystemExit(f"Unsupported MCP_TRANSPORT: {transport!r} (expected 'stdio' or 'http')")


if __name__ == "__main__":
    main()
