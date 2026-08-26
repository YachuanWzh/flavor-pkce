import { useState } from "react";

function parseSseBlock(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (dataLines.length === 0) return null;
  return { event, data: JSON.parse(dataLines.join("\n")) };
}

export default function AgentPage() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState("");
  const [streamedSql, setStreamedSql] = useState("");

  const ask = async (event) => {
    event.preventDefault();
    if (!question.trim() || loading) return;
    setLoading(true);
    setError("");
    setResult(null);
    setStatus("Connecting…");
    setStreamedSql("");
    try {
      // In production the gateway sits behind the /gw path prefix (Caddy);
      // in dev, vite proxies /gw to the gateway and strips the prefix.
      const resp = await fetch("/gw/api/agent/ask/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ question }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(
          data.detail || data.message || data.error || `HTTP ${resp.status}`,
        );
      }
      if (!resp.body) throw new Error("Streaming response is unavailable");

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let streamError = "";

      const applyEvent = ({ event: eventName, data }) => {
        if (eventName === "status") setStatus(data.message || "Working…");
        if (eventName === "delta") {
          setStreamedSql((current) => current + (data.text || ""));
        }
        if (eventName === "sql") setStreamedSql(data.sql || "");
        if (eventName === "result") setResult(data);
        if (eventName === "done") setStatus("Complete");
        if (eventName === "error") streamError = data.message || "Agent stream failed";
      };

      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        buffer = buffer.replace(/\r\n/g, "\n");
        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const parsed = parseSseBlock(block);
          if (parsed) applyEvent(parsed);
          if (streamError) break;
          boundary = buffer.indexOf("\n\n");
        }
        if (streamError) {
          await reader.cancel();
          throw new Error(streamError);
        }
        if (done) break;
      }

      if (buffer.trim()) {
        const parsed = parseSseBlock(buffer.trim());
        if (parsed) applyEvent(parsed);
      }
      if (streamError) throw new Error(streamError);
    } catch (cause) {
      setError(cause.message);
      setStatus("");
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

      {!result && (loading || streamedSql) && (
        <section className="agent-result agent-stream" aria-live="polite">
          <div className="agent-stream-head">
            <span className="agent-live-dot" aria-hidden="true" />
            <span>{status || "Streaming…"}</span>
          </div>
          <pre className="agent-sql">{streamedSql || "Waiting for the model…"}</pre>
        </section>
      )}

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
