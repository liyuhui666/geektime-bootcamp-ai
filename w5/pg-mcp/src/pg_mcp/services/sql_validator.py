"""SQL Security Validator using SQLGlot.

This module provides SQL validation and security checking using SQLGlot parser.
It ensures that only safe, read-only queries are executed and blocks potentially
dangerous operations.
"""

import re
from typing import ClassVar

import sqlglot
from sqlglot import exp

from pg_mcp.config.policy import EffectivePolicy
from pg_mcp.models.errors import SecurityViolationError, SQLParseError
from pg_mcp.models.query import ValidationResult

# Statement keywords after which EXPLAIN option words end. EXPLAIN options
# appear as leading words ("EXPLAIN ANALYZE SELECT ...") or inside parens
# ("EXPLAIN (ANALYZE, BUFFERS) SELECT ..."). For the prefix form, leading
# words are consumed while they name a known EXPLAIN option; the first word
# that is not an option starts the inner statement (whatever it is — the
# recursive validation decides whether that statement is allowed).
_KNOWN_EXPLAIN_OPTIONS: frozenset[str] = frozenset(
    {
        "ANALYZE",
        "VERBOSE",
        "COSTS",
        "SETTINGS",
        "GENERIC_PLAN",
        "BUFFERS",
        "WAL",
        "TIMING",
        "SUMMARY",
        "FORMAT",
    }
)

_SYSTEM_CATALOG_SCHEMAS = frozenset({"pg_catalog", "information_schema"})


def _split_explain_options(text: str) -> tuple[set[str], str]:
    """Split EXPLAIN options from the inner statement.

    Handles both PostgreSQL syntaxes:
    - Prefix form:  "ANALYZE SELECT ..."          -> ({"ANALYZE"}, "SELECT ...")
    - Bracket form: "(ANALYZE, BUFFERS) SELECT ..." -> ({"ANALYZE", "BUFFERS"}, "SELECT ...")

    Args:
        text: Everything after the EXPLAIN keyword.

    Returns:
        tuple: (uppercased option names, remaining inner statement text).
    """
    stripped = text.strip()

    # Bracket option form: "OPTIONS" inner
    if stripped.startswith("("):
        depth = 0
        for i, ch in enumerate(stripped):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    options = {
                        part.strip().upper() for part in stripped[1:i].split(",") if part.strip()
                    }
                    return options, stripped[i + 1 :].strip()
        # Unbalanced paren: treat everything as the statement body; the
        # parser will reject it in the recursive validation pass.
        return set(), stripped

    # Prefix option form: consume leading words that name known options
    tokens = stripped.split()
    options: set[str] = set()
    index = 0
    while index < len(tokens) and tokens[index].upper() in _KNOWN_EXPLAIN_OPTIONS:
        options.add(tokens[index].upper())
        index += 1
    return options, " ".join(tokens[index:])


class SQLValidator:
    """SQL security validator using SQLGlot for parsing and validation.

    This validator ensures queries are safe by:
    - Allowing only SELECT statements
    - Blocking dangerous functions (pg_sleep, file operations, etc.)
    - Preventing access to blocked tables and columns (schema-qualified names
      supported for both)
    - Enforcing the per-database EXPLAIN / EXPLAIN ANALYZE policy, validating
      the statement *inside* EXPLAIN
    - Optionally rejecting pg_catalog / information_schema references
    - Rejecting multi-statement queries
    - Validating subquery safety

    The validator is stateless per query and driven entirely by its
    EffectivePolicy, so one instance per database is safe to share.
    """

    # Allowed statement types at the top level (including set operations)
    ALLOWED_STATEMENT_TYPES: ClassVar = {exp.Select, exp.Union, exp.Intersect, exp.Except}

    # Allowed top-level expressions (including CTEs)
    ALLOWED_TOP_LEVEL: ClassVar = {
        exp.Select,
        exp.Union,
        exp.Intersect,
        exp.Except,
        exp.With,
        exp.Subquery,
    }

    # Forbidden statement types
    FORBIDDEN_STATEMENT_TYPES: ClassVar = {
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.Grant,
        exp.Revoke,
        exp.Set,
        exp.Command,
        exp.Use,
        exp.Merge,
    }

    # Built-in dangerous PostgreSQL functions
    BUILTIN_DANGEROUS_FUNCTIONS: ClassVar = {
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_open",
        "pg_write_file",
        "pg_execute_sql",
        "copy_from",
        "copy_to",
    }

    def __init__(self, policy: EffectivePolicy) -> None:
        """Initialize SQL validator.

        Args:
            policy: The effective policy for this database (global security
                config merged with per-database overrides).
        """
        self.policy = policy
        self.blocked_tables = policy.blocked_tables
        self.blocked_columns = policy.blocked_columns
        self.allow_explain = policy.allow_explain

        # Combine built-in dangerous functions with configured blocked functions
        self.blocked_functions = self.BUILTIN_DANGEROUS_FUNCTIONS | policy.blocked_functions

    def validate(self, sql: str) -> tuple[bool, str | None]:
        """Validate SQL query for security compliance.

        Args:
            sql: SQL query string to validate.

        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is None.
        """
        try:
            self.validate_or_raise(sql)
            return (True, None)
        except (SecurityViolationError, SQLParseError) as e:
            return (False, str(e))

    def validate_or_raise(self, sql: str) -> None:
        """Validate SQL query and raise exception on violation.

        Args:
            sql: SQL query string to validate.

        Raises:
            SQLParseError: If SQL cannot be parsed.
            SecurityViolationError: If SQL violates security constraints.
        """
        # Check for empty or whitespace-only SQL
        if not sql or not sql.strip():
            raise SQLParseError("SQL query cannot be empty")

        # Parse SQL using SQLGlot
        try:
            parsed = sqlglot.parse(sql, read="postgres")
        except Exception as e:
            raise SQLParseError(f"Failed to parse SQL: {e}") from e

        # Check for multiple statements
        if len(parsed) > 1:
            raise SecurityViolationError(
                "Multiple statements not allowed. Only single SELECT queries are permitted."
            )

        if not parsed:
            raise SQLParseError("No valid SQL statement found")

        statement = parsed[0]

        # Check for null or empty statement (e.g., comment-only SQL)
        if statement is None or isinstance(statement, type(None)):
            raise SQLParseError("No valid SQL statement found")

        # Handle EXPLAIN statements (parsed as Command in sqlglot 28.5.0)
        if isinstance(statement, exp.Command):
            # Check if it's an EXPLAIN command
            cmd_name = str(statement.this).upper() if statement.this else ""
            if cmd_name == "EXPLAIN":
                self._validate_explain(statement, original_sql=sql)
                return
            # Other commands are not allowed
            raise SecurityViolationError(
                f"Command '{cmd_name}' is not allowed. Only SELECT queries are permitted."
            )

        # Note: top-level WITH ... SELECT parses as exp.Select (with a "with"
        # arg), never as a bare exp.With — CTE bodies are covered by
        # _check_subquery_safety's whole-tree scan below.
        main_query = statement

        # Perform security checks
        if error := self._check_statement_type(main_query):
            raise SecurityViolationError(error)

        if error := self._check_dangerous_functions(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_tables(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_columns(statement):
            raise SecurityViolationError(error)

        if error := self._check_system_catalogs(statement):
            raise SecurityViolationError(error)

        if error := self._check_subquery_safety(statement):
            raise SecurityViolationError(error)

    def _validate_explain(self, statement: exp.Command, original_sql: str) -> None:
        """Validate an EXPLAIN statement, including its inner query.

        EXPLAIN alone only shows a plan, but EXPLAIN ANALYZE actually
        executes the statement, so it has a separate (default-off) switch.
        The inner statement must pass ALL validation rules — EXPLAIN does not
        provide a way to smuggle a forbidden statement past this validator.

        Args:
            statement: The parsed exp.Command for the EXPLAIN.
            original_sql: The raw SQL, used as fallback for extracting the
                inner statement text (storage location varies by sqlglot
                version).

        Raises:
            SecurityViolationError: If EXPLAIN/ANALYZE not allowed or the
                inner statement violates any rule.
            SQLParseError: If the EXPLAIN has no inner query.
        """
        if not self.policy.allow_explain:
            raise SecurityViolationError("EXPLAIN statements are not allowed")

        inner = self._extract_explain_body(statement, original_sql)
        options, body = _split_explain_options(inner)

        if "ANALYZE" in options and not self.policy.allow_explain_analyze:
            raise SecurityViolationError("EXPLAIN ANALYZE is not allowed")

        inner = body.strip()
        if not inner:
            raise SQLParseError("EXPLAIN has no inner query")
        self.validate_or_raise(inner)

    @staticmethod
    def _extract_explain_body(statement: exp.Command, original_sql: str) -> str:
        """Get the text after the EXPLAIN keyword.

        sqlglot 28.5 parses EXPLAIN as exp.Command; where it stores the rest
        of the statement varies, so fall back to stripping the prefix from
        the original SQL.

        Args:
            statement: The parsed EXPLAIN command node.
            original_sql: The raw SQL text.

        Returns:
            str: Everything after the EXPLAIN keyword.
        """
        rest = statement.expression
        if rest is not None:
            if isinstance(rest, exp.Literal) and rest.is_string:
                # sqlglot stores the post-EXPLAIN text as a string literal;
                # .sql() would re-add surrounding quotes.
                text = str(rest.this)
            elif isinstance(rest, exp.Expression):
                text = rest.sql(dialect="postgres")
            else:
                text = str(rest)
            if text and text.strip():
                return text
        # Fallback: drop the leading EXPLAIN keyword from the raw SQL
        return re.sub(r"^\s*EXPLAIN\b", "", original_sql, count=1, flags=re.IGNORECASE)

    def validate_detail(self, sql: str) -> ValidationResult:
        """Validate SQL and return a structured result without raising.

        Convenience wrapper for callers that want the full ValidationResult
        model (including which blocked functions, if any, triggered rejection).

        Args:
            sql: SQL query string to validate.

        Returns:
            ValidationResult: Structured validation outcome. On failure,
                uses_blocked_functions carries the functions named in the
                violation message (empty for non-function violations).
        """
        try:
            self.validate_or_raise(sql)
        except (SecurityViolationError, SQLParseError) as e:
            message = str(e)
            # Recover the blocked function name (if any) from the violation
            # message, e.g. "Function 'pg_sleep' is blocked..."
            blocked = re.findall(r"Function '([\w.]+)' is blocked", message)
            return ValidationResult(
                is_valid=False,
                is_select=False,
                # Not confirmed either way on failure; is_safe already
                # returns False because is_valid is False.
                allows_data_modification=False,
                uses_blocked_functions=blocked,
                error_message=message,
            )

        return ValidationResult(
            is_valid=True,
            is_select=True,
            allows_data_modification=False,
            uses_blocked_functions=[],
            error_message=None,
        )

    def _check_statement_type(self, statement: exp.Expression) -> str | None:
        """Check if statement type is allowed.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Check for forbidden statement types
        for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
            if isinstance(statement, forbidden_type):
                stmt_name = forbidden_type.__name__.upper()
                return f"{stmt_name} statements are not allowed. Only SELECT queries are permitted."

        # Ensure statement is an allowed type (SELECT or set operations)
        if not isinstance(statement, tuple(self.ALLOWED_STATEMENT_TYPES)):
            stmt_type = type(statement).__name__
            return f"Statement type {stmt_type} is not allowed. Only SELECT queries are permitted."

        return None

    def _check_dangerous_functions(self, statement: exp.Expression) -> str | None:
        """Check for use of blocked/dangerous functions.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Find all function calls in the query
        for func in statement.find_all(exp.Func):
            func_name = func.name.lower() if func.name else ""

            if func_name in self.blocked_functions:
                return f"Function '{func_name}' is blocked for security reasons"

        return None

    def _check_blocked_tables(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked tables.

        Matching is schema-aware: a blocked entry containing "." matches
        "schema.table" exactly; a bare entry matches the table name in any
        schema.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_tables:
            return None

        # Find all table references
        for table in statement.find_all(exp.Table):
            table_name = table.name.lower() if table.name else ""
            # sqlglot: table.db is the schema part, table.catalog the database
            full_name = f"{table.db.lower()}.{table_name}" if table.db else table_name

            if table_name in self.blocked_tables or full_name in self.blocked_tables:
                return f"Access to table '{full_name}' is not allowed"

        return None

    def _check_blocked_columns(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked columns.

        Matching mirrors the table rule: bare entries match the column name
        anywhere; "table.column" entries match that qualification. An
        unqualified column is additionally checked against every table
        referenced by the statement (resolving aliases), so "users.password"
        in the blocklist also catches "SELECT password FROM users". This
        deliberately over-blocks when a column name is ambiguous across the
        statement's tables — for a blocklist, over-blocking is the safe
        direction.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_columns:
            return None

        # Collect every table name/alias so unqualified columns can be
        # checked against table-qualified blocklist entries.
        table_keys: set[str] = set()
        for table in statement.find_all(exp.Table):
            if table.name:
                table_keys.add(table.name.lower())
                if table.alias:
                    table_keys.add(str(table.alias).lower())

        # Find all column references
        for column in statement.find_all(exp.Column):
            column_name = column.name.lower() if column.name else ""

            # Check for exact match
            if column_name in self.blocked_columns:
                return f"Access to column '{column_name}' is not allowed"

            qualifiers = {column.table.lower()} if column.table else set()
            qualifiers |= table_keys
            for qualifier in sorted(qualifiers):
                qualified_name = f"{qualifier}.{column_name}"
                if qualified_name in self.blocked_columns:
                    return f"Access to column '{qualified_name}' is not allowed"

        return None

    def _check_system_catalogs(self, statement: exp.Expression) -> str | None:
        """Reject pg_catalog / information_schema references when enabled.

        Opt-in (EffectivePolicy.block_system_catalogs, default off): the
        documented example queries read information_schema metadata, and
        with a fixed search_path plus read-only transactions the exposure
        from metadata reads is limited.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.policy.block_system_catalogs:
            return None

        for table in statement.find_all(exp.Table):
            schema_name = table.db.lower() if table.db else ""
            if schema_name in _SYSTEM_CATALOG_SCHEMAS:
                return f"Access to system catalog '{schema_name}.{table.name}' is not allowed"

        return None

    def _check_subquery_safety(self, statement: exp.Expression) -> str | None:
        """Check that no nested statement violates the read-only constraint.

        Covers every nesting form, including data-modifying CTEs
        ("WITH d AS (DELETE ...) SELECT ...") and FROM-subqueries.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # A forbidden node anywhere in the tree (CTE bodies included) makes
        # the whole statement unsafe.
        for node in statement.walk():
            if isinstance(node, tuple(self.FORBIDDEN_STATEMENT_TYPES)):
                stmt_name = type(node).__name__.upper()
                return f"{stmt_name} statements are not allowed in any part of the query"

        # FROM-subqueries must contain a query (SELECT/UNION/...). Note UNION
        # is exp.Union, not exp.Select — exp.Query covers both.
        for subquery in statement.find_all(exp.Subquery):
            if subquery.this and not isinstance(subquery.this, exp.Query):
                return "Subqueries must contain only SELECT statements"

        return None

    def normalize_sql(self, sql: str) -> str:
        """Normalize SQL query to a canonical form.

        This removes extra whitespace, standardizes formatting, and makes
        queries easier to compare or cache.

        Args:
            sql: SQL query string to normalize.

        Returns:
            Normalized SQL string.

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            # Generate normalized SQL
            return parsed.sql(dialect="postgres", pretty=False)
        except Exception as e:
            raise SQLParseError(f"Failed to normalize SQL: {e}") from e

    def extract_tables(self, sql: str) -> list[str]:
        """Extract all table names referenced in the SQL query.

        Args:
            sql: SQL query string.

        Returns:
            List of table names (in lowercase).

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            tables = []

            for table in parsed.find_all(exp.Table):
                if table.name:
                    tables.append(table.name.lower())

            return sorted(set(tables))
        except Exception as e:
            raise SQLParseError(f"Failed to extract tables: {e}") from e
