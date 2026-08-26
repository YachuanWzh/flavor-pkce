import { useState } from "react";

export default function AgentPage() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const ask = async (event) => {
    event.preventDefault();
    if (!question.trim() || loading) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      // In production the gateway sits behind the /gw path prefix (Caddy);
      // in dev, vite proxies /gw to the gateway and strips the prefix.
      const resp = await fetch("/gw/api/agent/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ question }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(
        data.detail || data.message || data.error || `HTTP ${resp.status}`,
      );
      setResult(data);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="admin-shell">
      <header className="admin-hero">
        <div>
          <p className="settings-kicker">DATA AGENT / NL→SQL</p>
          <h1>Ask questions about your audit log.</h1>
          <p>Natural language to read-only SQL, executed against the gateway audit database. Every query is gated by the administrator token and capped in size.</p>
        </div>
      </header>

      <form onSubmit={ask} className="agent-form">
        <input
          className="agent-input"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. How many requests per user in the last 7 days?"
          disabled={loading}
        />
        <button type="submit" className="btn-primary agent-submit" disabled={loading || !question.trim()}>
          {loading ? "Asking…" : "Ask"}
        </button>
      </form>

      {error && <div className="error-msg">{error}</div>}

      {result && (
        <section className="agent-result">
          <h2>Generated SQL</h2>
          <pre className="agent-sql">{result.sql}</pre>
          {result.truncated && <p className="agent-hint">Results were truncated to the row limit.</p>}
          {result.rows.length === 0 ? (
            <p className="agent-hint">No rows returned.</p>
          ) : (
            <div className="agent-table-wrap">
              <table className="agent-table">
                <thead>
                  <tr>{result.columns.map((col) => <th key={col}>{col}</th>)}</tr>
                </thead>
                <tbody>
                  {result.rows.map((row, i) => (
                    <tr key={i}>
                      {result.columns.map((col) => <td key={col}>{String(row[col] ?? "")}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </main>
  );
}
