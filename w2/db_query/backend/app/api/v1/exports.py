"""Query export API endpoints.

Provides a one-shot "execute query + export" endpoint and a dynamic format
list. The export reuses the read-only query path (validation + LIMIT 1000) so
there is no bypass of the read-only guarantee. By default exports are not
recorded in query history (see FEATURE_EXPORT.md §5.5).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlmodel import Session, select

from app.database import get_session
from app.models.database import DatabaseConnection
from app.models.schemas import ExportRequest
from app.services.export import export_service
from app.services.query_wrapper import execute_query_with_service
from app.services.sql_validator import SqlValidationError
from app.utils.filename import slugify, utc_timestamp

router = APIRouter(prefix="/api/v1/dbs", tags=["export"])


@router.get("/export/formats")
async def list_export_formats() -> dict[str, list[str]]:
    """Return the list of supported export formats.

    The list is sourced from ``ExporterRegistry`` at runtime, so registering a
    new exporter automatically extends this endpoint (no schema change).
    """
    return {"formats": export_service.supported_formats()}


@router.post("/{name}/query/export")
async def export_query(
    name: str,
    req: ExportRequest,
    session: Session = Depends(get_session),
) -> Response:
    """Execute a query and return the result as a downloadable file.

    Args:
        name: Database connection name
        req: Export request (sql, format, json_style, save_history)
        session: SQLite database session

    Returns:
        File response with appropriate Content-Type and Content-Disposition

    Raises:
        HTTPException: 404 if connection not found, 400 on SQL validation
            error, 422 on unsupported format, 500 on execution failure
    """
    # 1) Connection existence
    conn = session.exec(select(DatabaseConnection).where(DatabaseConnection.name == name)).first()
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database connection '{name}' not found",
        )

    # 2) Execute query (reuses read-only validation + LIMIT 1000; default no history)
    try:
        result = await execute_query_with_service(
            session,
            name,
            conn.db_type,
            conn.url,
            req.sql,
            record_history=req.save_history,
        )
    except SqlValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query execution failed: {str(e)}",
        )

    # 3) Serialize (unsupported format -> 422)
    try:
        payload, content_type, ext = export_service.export(
            result,
            req.format,
            options={"json_style": req.json_style},
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(e),
        )

    # 4) Safe filename + deliver
    filename = f"{slugify(name)}_{utc_timestamp()}.{ext}"
    return Response(
        content=payload,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
