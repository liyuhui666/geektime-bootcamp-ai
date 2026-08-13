/** Parse the natural-language input box into a structured intent.
 *
 * The NL box doubles as a "smart command bar": it accepts a natural-language
 * question, a raw SQL statement, or a SQL + export instruction such as
 *   `SELECT * FROM users LIMIT 1000; 导出数据文件为csv格式`
 *
 * This module extracts three things without calling any backend:
 *  - `sql`        : a raw SQL statement if the input contains one (else undefined)
 *  - `question`   : a cleaned natural-language question (export instruction stripped)
 *                   — only meaningful when no `sql` was found
 *  - `format`     : the requested export format, when stated
 *  - `wantsExport`: true when the input mentions exporting / downloading
 */

import type { ExportFormat } from "../types/query";

export interface ParsedCommand {
  /** A raw SQL statement extracted from the input, if present. */
  sql?: string;
  /** Cleaned natural-language question (command stripped), for NL2SQL. */
  question?: string;
  /** Detected export format (only set when explicitly stated). */
  format?: ExportFormat;
  /** Whether the user asked to export / download the result. */
  wantsExport: boolean;
}

const FORMAT_RE = /\b(csv|json|ndjson)\b/i;

// Export-style verbs in Chinese and English, plus an optional trailing
// connector (为/成/至/到). Used to detect intent and to split the SQL/question
// away from the trailing instruction like "导出为 csv".
const COMMAND_RE =
  /(导出|下载|保存|存|输出|export(?:\s+(?:为|to|as))?)\s*(?:为|成|至|到)?/i;

// A SQL statement: from SELECT/WITH up to a ";", a command keyword, or end.
const SQL_RE =
  /\b(SELECT|WITH)\b[\s\S]*?(?=;|\s+(?:导出|下载|保存|存为|存成|输出|export)|$)/i;

export function parseNaturalCommand(raw: string): ParsedCommand {
  const text = raw.trim();
  if (!text) return { wantsExport: false };

  const formatMatch = text.match(FORMAT_RE);
  const format = formatMatch?.[1]?.toLowerCase() as ExportFormat | undefined;
  const wantsExport = !!formatMatch || COMMAND_RE.test(text);

  // 1) Raw SQL present? Extract and hand it back verbatim (never re-generate).
  const sqlMatch = text.match(SQL_RE);
  if (sqlMatch) {
    const sql = sqlMatch[0].replace(/;+\s*$/, "").trim();
    return { sql, format, wantsExport };
  }

  // 2) No SQL → treat as a natural-language question, with the export
  //    instruction AND the format token stripped so NL2SQL doesn't receive
  //    "导出为csv" / "export to json" noise.
  const question = text
    .replace(COMMAND_RE, " ")
    .replace(FORMAT_RE, " ")
    .replace(/[,，、\s]+$/, "")
    .replace(/\s+/g, " ")
    .trim();
  return { question: question || text, format, wantsExport };
}
