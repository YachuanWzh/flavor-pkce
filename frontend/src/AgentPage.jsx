import { useEffect, useRef, useState } from "react";
import * as echarts from "echarts";

const PALETTE = ["#5b8def", "#34d399", "#f87171", "#fbbf24", "#a78bfa", "#22d3ee"];

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

function useChart(ref, option) {
  useEffect(() => {
    if (!ref.current || !option) return undefined;
    const chart = echarts.init(ref.current);
    chart.setOption(option);
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, [ref, option]);
}

const DATE_COL = /^(date|day|timestamp|.*_date)$/i;

function isNumericColumn(rows, col) {
  return rows.every((row) => {
    const v = row[col];
    if (v == null || v === "") return true;
    return Number.isFinite(Number(v));
  });
}

function isDateColumn(rows, col) {
  if (!DATE_COL.test(col)) return false;
  return rows.every((row) => {
    const v = row[col];
    if (v == null || v === "") return true;
    return /^\d{4}-\d{2}-\d{2}/.test(String(v));
  });
}

function buildChartOption(columns, rows) {
  if (!rows || rows.length === 0) return null;
  const numericCols = columns.filter((col) => isNumericColumn(rows, col));
  const dateCols = columns.filter((col) => isDateColumn(rows, col));
  const categoryCols = columns.filter(
    (col) => !numericCols.includes(col) && !dateCols.includes(col),
  );
  if (numericCols.length === 0 && dateCols.length === 0) return null;

  const base = {
    color: PALETTE,
    tooltip: { trigger: "axis" },
    grid: { left: 56, right: 20, top: 36, bottom: 36 },
  };

  // Time series: a date-ish column on the x-axis, numeric columns as series.
  if (dateCols.length > 0) {
    const x = dateCols[0];
    return {
      ...base,
      legend: { data: numericCols, top: 0 },
      xAxis: { type: "category", data: rows.map((r) => String(r[x] ?? "")) },
      yAxis: { type: "value" },
      series: numericCols.map((col) => ({
        name: col,
        type: "line",
        smooth: true,
        areaStyle: {},
        data: rows.map((r) => Number(r[col]) || 0),
      })),
    };
  }

  // Category + one numeric column: pie when few slices, bar otherwise.
  if (categoryCols.length > 0 && numericCols.length >= 1) {
    const cat = categoryCols[0];
    const num = numericCols[0];
    const labels = rows.map((r) => String(r[cat] ?? ""));
    if (labels.length <= 8) {
      return {
        ...base,
        tooltip: { trigger: "item" },
        series: [{
          name: num,
          type: "pie",
          radius: ["35%", "65%"],
          data: rows.map((r, i) => ({ name: labels[i], value: Number(r[num]) || 0 })),
        }],
      };
    }
    return {
      ...base,
      xAxis: { type: "category", data: labels },
      yAxis: { type: "value" },
      series: [{ name: num, type: "bar", data: rows.map((r) => Number(r[num]) || 0) }],
    };
  }

  // Only numeric columns: multi-series bar indexed by row.
  return {
    ...base,
    legend: { data: numericCols, top: 0 },
    xAxis: { type: "category", data: rows.map((_, i) => String(i + 1)) },
    yAxis: { type: "value" },
    series: numericCols.map((col) => ({
      name: col,
      type: "bar",
      data: rows.map((r) => Number(r[col]) || 0),
    })),
  };
}

export default function AgentPage() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState("");
  const [streamedSql, setStreamedSql] = useState("");
  const [view, setView] = useState("chart");
  const [turns, setTurns] = useState([]);
  const chartRef = useRef(null);

  const chartOption = result
    ? buildChartOption(result.columns, result.rows)
    : null;
  useChart(chartRef, chartOption);

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
      const history = turns.slice(-5).map((t) => ({
        question: t.question,
        sql: t.result?.sql,
      }));
      const resp = await fetch("/gw/api/agent/ask/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ question, history }),
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
      let finalResult = null;

      const applyEvent = ({ event: eventName, data }) => {
        if (eventName === "status") setStatus(data.message || "Working…");
        if (eventName === "delta") {
          setStreamedSql((current) => current + (data.text || ""));
        }
        if (eventName === "sql") setStreamedSql(data.sql || "");
        if (eventName === "result") {
          finalResult = data;
          setResult(data);
        }
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
      if (finalResult) {
        setTurns((prev) => [...prev, { question, result: finalResult }]);
      }
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

      {turns.length > 0 && (
        <section className="agent-history">
          <h2>Conversation</h2>
          {turns.map((turn, i) => (
            <div key={i} className="agent-turn">
              <div className="agent-turn-head">
                <span className="agent-turn-q">{turn.question}</span>
                <button
                  className="agent-turn-show"
                  onClick={() => {
                    setResult(turn.result);
                    setStreamedSql("");
                    setView("chart");
                    setStatus("Complete");
                  }}
                >
                  Show {turn.result?.rows?.length ?? 0} rows
                </button>
              </div>
              <pre className="agent-turn-sql">{turn.result?.sql}</pre>
            </div>
          ))}
        </section>
      )}

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
            <>
              {chartOption && (
                <div className="agent-view-toggle">
                  <button
                    className={view === "chart" ? "active" : ""}
                    onClick={() => setView("chart")}
                  >
                    Chart
                  </button>
                  <button
                    className={view === "table" ? "active" : ""}
                    onClick={() => setView("table")}
                  >
                    Table
                  </button>
                </div>
              )}
              {chartOption && view === "chart" ? (
                <div ref={chartRef} className="agent-chart" />
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
            </>
          )}
        </section>
      )}
    </main>
  );
}
