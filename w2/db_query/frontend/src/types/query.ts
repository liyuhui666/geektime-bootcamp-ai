/** Query execution types. */

export interface QueryColumn {
  name: string;
  dataType: string;
}

export interface QueryResult {
  columns: QueryColumn[];
  rows: Record<string, any>[];
  rowCount: number;
  executionTimeMs: number;
  sql: string;
}

export interface QueryInput {
  sql: string;
}

/** Supported export formats. Mirrors the backend ExporterRegistry. */
export type ExportFormat = "csv" | "json" | "ndjson";

export interface ExportRequest {
  sql: string;
  format: ExportFormat;
  /** JSON output style (only affects format=json). */
  jsonStyle?: "document" | "array";
  /** Whether to record the export run in query history (defaults to false). */
  saveHistory?: boolean;
}

export interface QueryHistoryEntry {
  id: number;
  databaseName: string;
  sqlText: string;
  executedAt: string;
  executionTimeMs?: number | null;
  rowCount?: number | null;
  success: boolean;
  errorMessage?: string | null;
  querySource: "manual" | "natural_language";
}
