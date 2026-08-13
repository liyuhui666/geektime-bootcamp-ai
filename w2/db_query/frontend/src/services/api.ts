/** Axios API client instance and domain API helpers. */

import axios from "axios";
import { ExportFormat } from "../types/query";

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

export const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    "Content-Type": "application/json",
  },
});

// Request interceptor
apiClient.interceptors.request.use(
  (config) => {
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// Response interceptor
apiClient.interceptors.response.use(
  (response) => {
    return response;
  },
  (error) => {
    // Handle common errors
    if (error.response) {
      const message =
        error.response.data?.detail || error.response.data?.error || "An error occurred";
      console.error("API Error:", message);
    }
    return Promise.reject(error);
  }
);

/**
 * Parse a filename from a Content-Disposition header.
 * Falls back to `fallback` if the header is missing or malformed.
 *
 * Handles both `filename="name.csv"` and `filename=name.csv`.
 */
export function parseFilename(
  contentDisposition: string | undefined,
  fallback: string
): string {
  if (!contentDisposition) return fallback;
  const match = contentDisposition.match(/filename="?([^";]+)"?/i);
  return match?.[1] ?? fallback;
}

/** Trigger a browser download for a Blob with the given filename. */
function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

/**
 * Extract a human-readable error message from a failed request.
 *
 * When `responseType: "blob"` is used (file downloads), error response bodies
 * arrive as Blobs containing JSON rather than parsed objects, so we read the
 * Blob text and parse it here.
 */
async function extractErrorMessage(err: unknown): Promise<string> {
  const axiosErr = err as { response?: { data?: any }; message?: string };
  const data = axiosErr?.response?.data;
  if (data instanceof Blob) {
    try {
      const text = await data.text();
      const json = JSON.parse(text);
      return json.detail || json.error || text || "Export failed";
    } catch {
      return "Export failed";
    }
  }
  return data?.detail || axiosErr?.message || "Export failed";
}

/**
 * Execute a query on the backend and download the result as a file.
 *
 * This re-runs the SQL server-side (see FEATURE_EXPORT.md §5.5) so the export
 * shares one serialization path with CLI/Agent callers (DRY). Supports CSV,
 * JSON, and NDJSON.
 *
 * @param databaseName Database connection name
 * @param sql          SQL SELECT query to export
 * @param format       Export format
 * @param options      Optional jsonStyle / saveHistory
 * @throws Error with the backend's detail message on failure (400/404/422/500)
 */
export async function exportQuery(
  databaseName: string,
  sql: string,
  format: ExportFormat,
  options?: { jsonStyle?: "document" | "array"; saveHistory?: boolean }
): Promise<void> {
  try {
    const res = await apiClient.post(
      `/api/v1/dbs/${databaseName}/query/export`,
      {
        sql,
        format,
        jsonStyle: options?.jsonStyle,
        saveHistory: options?.saveHistory,
      },
      { responseType: "blob" }
    );
    const filename = parseFilename(
      res.headers["content-disposition"],
      `export.${format}`
    );
    triggerDownload(res.data, filename);
  } catch (err) {
    throw new Error(await extractErrorMessage(err));
  }
}
