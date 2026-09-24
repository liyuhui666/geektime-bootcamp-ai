"""PostgreSQL schema introspection.

This module provides functionality to introspect PostgreSQL database schemas,
extracting comprehensive metadata about tables, columns, constraints, indexes,
and custom types.

Performance note: all metadata is fetched with a fixed number of set-based
catalog queries (one query per metadata kind, covering every user table at
once). Never reintroduce per-table/per-column round trips here - on a wide
database the serial round trips add up to minutes and block server startup
past MCP clients' initialize timeouts.
"""

from collections import defaultdict

from asyncpg import Pool
from asyncpg.connection import Connection

from pg_mcp.models.schema import (
    ColumnInfo,
    DatabaseSchema,
    EnumTypeInfo,
    ForeignKeyInfo,
    IndexInfo,
    TableInfo,
)

# Key used by the batch loaders below: (schema_name, table_name).
TableKey = tuple[str, str]


class SchemaIntrospector:
    """PostgreSQL schema introspection service.

    This class provides methods to extract complete schema metadata from
    a PostgreSQL database using system catalogs.

    Attributes:
        pool: Database connection pool.
        database_name: Name of the database being introspected.
    """

    def __init__(self, pool: Pool, database_name: str):
        """Initialize schema introspector.

        Args:
            pool: asyncpg connection pool.
            database_name: Name of the database to introspect.
        """
        self.pool = pool
        self.database_name = database_name

    async def introspect(self) -> DatabaseSchema:
        """Execute complete schema introspection.

        This method fetches all schema metadata including tables, views,
        columns, constraints, indexes, and custom types using a fixed
        number of set-based queries regardless of table count.

        Returns:
            DatabaseSchema: Complete database schema information.

        Example:
            >>> introspector = SchemaIntrospector(pool, "mydb")
            >>> schema = await introspector.introspect()
            >>> print(f"Found {len(schema.tables)} tables")
        """
        async with self.pool.acquire() as conn:
            # Get PostgreSQL version
            version_result = await conn.fetchval("SELECT version()")
            version = version_result.split(",")[0] if version_result else None

            tables = await self._get_tables(conn)
            views = await self._get_views(conn)
            enum_types = await self._get_enum_types(conn)

            # Batch-load per-table metadata (one query per kind, all tables
            # at once), then attach it in memory.
            all_tables = tables + views
            columns = await self._get_all_columns(conn)
            primary_keys = await self._get_all_primary_keys(conn)
            foreign_keys = await self._get_all_foreign_keys(conn)
            indexes = await self._get_all_indexes(conn)
            row_counts = await self._get_all_row_count_estimates(conn)

            for table in all_tables:
                key = (table.schema_name, table.table_name)
                table.columns = columns.get(key, [])
                primary_key_columns = primary_keys.get(key, set())
                for col in table.columns:
                    if col.name in primary_key_columns:
                        col.is_primary_key = True
                table.foreign_keys = foreign_keys.get(key, [])
                table.indexes = indexes.get(key, [])
                table.row_count_estimate = row_counts.get(key, 0)

            return DatabaseSchema(
                database_name=self.database_name,
                tables=all_tables,
                enum_types=enum_types,
                version=version,
            )

    async def _get_tables(self, conn: Connection) -> list[TableInfo]:
        """Get all user tables (excluding system tables).

        Args:
            conn: Database connection.

        Returns:
            list[TableInfo]: List of table information objects.
        """
        query = """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                obj_description(c.oid, 'pg_class') AS comment
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'  -- regular tables only
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            ORDER BY n.nspname, c.relname
        """

        rows = await conn.fetch(query)

        return [
            TableInfo(
                schema_name=row["schema_name"],
                table_name=row["table_name"],
                comment=row["comment"],
                row_count_estimate=None,
            )
            for row in rows
        ]

    async def _get_all_columns(self, conn: Connection) -> dict[TableKey, list[ColumnInfo]]:
        """Get column metadata for every user table/view in one query.

        Uniqueness is resolved in the same query (EXISTS against unique
        constraints), so no per-column round trip is needed.

        Args:
            conn: Database connection.

        Returns:
            dict: (schema_name, table_name) -> columns ordered by attnum.
        """
        query = """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                a.attname AS column_name,
                pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                NOT a.attnotnull AS is_nullable,
                pg_get_expr(ad.adbin, ad.adrelid) AS default_value,
                col_description(a.attrelid, a.attnum) AS comment,
                EXISTS(
                    SELECT 1
                    FROM pg_constraint con
                    WHERE con.conrelid = c.oid
                      AND con.contype = 'u'  -- unique constraint (PKs are 'p')
                      AND a.attnum = ANY(con.conkey)
                ) AS is_unique
            FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            LEFT JOIN pg_attrdef ad ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
            WHERE c.relkind IN ('r', 'v')  -- tables and views
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY n.nspname, c.relname, a.attnum
        """

        columns: dict[TableKey, list[ColumnInfo]] = defaultdict(list)
        for row in await conn.fetch(query):
            columns[(row["schema_name"], row["table_name"])].append(
                ColumnInfo(
                    name=row["column_name"],
                    data_type=row["data_type"],
                    is_nullable=row["is_nullable"],
                    default_value=row["default_value"],
                    is_unique=row["is_unique"],
                    comment=row["comment"],
                )
            )
        return dict(columns)

    async def _get_all_primary_keys(self, conn: Connection) -> dict[TableKey, set[str]]:
        """Get primary key column names for every user table in one query.

        Args:
            conn: Database connection.

        Returns:
            dict: (schema_name, table_name) -> set of PK column names.
        """
        query = """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                a.attname AS column_name
            FROM pg_index i
            JOIN pg_class c ON i.indrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
            WHERE i.indisprimary
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        """

        primary_keys: dict[TableKey, set[str]] = defaultdict(set)
        for row in await conn.fetch(query):
            primary_keys[(row["schema_name"], row["table_name"])].add(row["column_name"])
        return dict(primary_keys)

    async def _get_all_foreign_keys(self, conn: Connection) -> dict[TableKey, list[ForeignKeyInfo]]:
        """Get foreign key relationships for every user table in one query.

        Args:
            conn: Database connection.

        Returns:
            dict: (schema_name, table_name) -> list of ForeignKeyInfo.
        """
        query = """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                con.conname AS constraint_name,
                a.attname AS column_name,
                ref_c.relname AS referenced_table,
                ref_a.attname AS referenced_column
            FROM pg_constraint con
            JOIN pg_class c ON con.conrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            JOIN pg_attribute a
                ON a.attrelid = c.oid AND a.attnum = ANY(con.conkey)
            JOIN pg_class ref_c ON con.confrelid = ref_c.oid
            JOIN pg_attribute ref_a
                ON ref_a.attrelid = ref_c.oid
                AND ref_a.attnum = ANY(con.confkey)
            WHERE con.contype = 'f'  -- foreign key
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            ORDER BY n.nspname, c.relname, con.conname
        """

        foreign_keys: dict[TableKey, list[ForeignKeyInfo]] = defaultdict(list)
        for row in await conn.fetch(query):
            foreign_keys[(row["schema_name"], row["table_name"])].append(
                ForeignKeyInfo(
                    constraint_name=row["constraint_name"],
                    column_name=row["column_name"],
                    referenced_table=row["referenced_table"],
                    referenced_column=row["referenced_column"],
                )
            )
        return dict(foreign_keys)

    async def _get_all_indexes(self, conn: Connection) -> dict[TableKey, list[IndexInfo]]:
        """Get index information for every user table in one query.

        Args:
            conn: Database connection.

        Returns:
            dict: (schema_name, table_name) -> list of IndexInfo.
        """
        query = """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                i.relname AS index_name,
                idx.indisunique AS is_unique,
                am.amname AS index_type,
                ARRAY(
                    SELECT a.attname
                    FROM pg_attribute a
                    WHERE a.attrelid = idx.indrelid
                      AND a.attnum = ANY(idx.indkey)
                    ORDER BY array_position(idx.indkey, a.attnum)
                ) AS columns
            FROM pg_index idx
            JOIN pg_class i ON i.oid = idx.indexrelid
            JOIN pg_class c ON c.oid = idx.indrelid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            JOIN pg_am am ON i.relam = am.oid
            WHERE NOT idx.indisprimary  -- exclude primary key indexes
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            ORDER BY n.nspname, c.relname, i.relname
        """

        indexes: dict[TableKey, list[IndexInfo]] = defaultdict(list)
        for row in await conn.fetch(query):
            indexes[(row["schema_name"], row["table_name"])].append(
                IndexInfo(
                    name=row["index_name"],
                    columns=list(row["columns"]),
                    is_unique=row["is_unique"],
                    index_type=row["index_type"],
                )
            )
        return dict(indexes)

    async def _get_views(self, conn: Connection) -> list[TableInfo]:
        """Get all user views.

        Args:
            conn: Database connection.

        Returns:
            list[TableInfo]: List of view information objects.
        """
        query = """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                obj_description(c.oid, 'pg_class') AS comment
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'v'  -- views only
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY n.nspname, c.relname
        """

        rows = await conn.fetch(query)

        return [
            TableInfo(
                schema_name=row["schema_name"],
                table_name=row["table_name"],
                comment=row["comment"],
                row_count_estimate=None,
            )
            for row in rows
        ]

    async def _get_enum_types(self, conn: Connection) -> list[EnumTypeInfo]:
        """Get custom ENUM type definitions.

        Args:
            conn: Database connection.

        Returns:
            list[EnumTypeInfo]: List of enum type information objects.
        """
        query = """
            SELECT
                n.nspname AS schema_name,
                t.typname AS type_name,
                ARRAY(
                    SELECT e.enumlabel
                    FROM pg_enum e
                    WHERE e.enumtypid = t.oid
                    ORDER BY e.enumsortorder
                ) AS values
            FROM pg_type t
            JOIN pg_namespace n ON t.typnamespace = n.oid
            WHERE t.typtype = 'e'  -- enum types only
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY n.nspname, t.typname
        """

        rows = await conn.fetch(query)

        return [
            EnumTypeInfo(
                schema_name=row["schema_name"],
                type_name=row["type_name"],
                values=list(row["values"]),
            )
            for row in rows
        ]

    async def _get_all_row_count_estimates(self, conn: Connection) -> dict[TableKey, int]:
        """Get estimated row counts for every user table/view in one query.

        Uses PostgreSQL's statistics (reltuples) rather than COUNT(*).
        Negative estimates (never analyzed) are clamped to 0.

        Args:
            conn: Database connection.

        Returns:
            dict: (schema_name, table_name) -> estimated row count.
        """
        query = """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                GREATEST(c.reltuples::bigint, 0) AS estimate
            FROM pg_class c
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE c.relkind IN ('r', 'v')  -- tables and views
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        """

        estimates: dict[TableKey, int] = {}
        for row in await conn.fetch(query):
            estimates[(row["schema_name"], row["table_name"])] = int(row["estimate"])
        return estimates
