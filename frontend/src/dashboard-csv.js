// Pure CSV builders for the dashboard export (improvement 8).
// Kept free of React/DOM so they run under plain node.

function escapeCell(value) {
  if (value === null || value === undefined) return "";
  const s = String(value);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** Array-of-objects → CSV text ("" for empty input). Columns are the
 *  union of every row's keys so late-appearing fields are not lost. */
export function rowsToCsv(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return "";
  const cols = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const lines = [cols.join(",")];
  for (const row of rows) {
    lines.push(cols.map((c) => escapeCell(row[c])).join(","));
  }
  return lines.join("\n");
}

/** Whole /api/stats payload → sectioned CSV (one `# <name>` block per
 *  dataset; empty datasets are omitted). */
export function buildDashboardCsv(data) {
  const sections = [];
  const add = (title, rows) => {
    const body = rowsToCsv(rows);
    if (body) sections.push(`# ${title}\n${body}`);
  };
  add("daily_tokens", data?.tokens);
  add("daily_requests", data?.requests);
  add("daily_cache", data?.cache);
  add("daily_cost", data?.cost);
  add("cost_by_user", data?.costUsers);
  add("latency", data?.latency);
  add("models", data?.models);
  add("users", data?.users);
  add("services", data?.services);
  add("errors", data?.errors);
  return sections.length ? sections.join("\n\n") + "\n" : "";
}
