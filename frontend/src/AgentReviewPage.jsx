import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import echarts from "./echarts-lite";

const PALETTE = ["#5b8def", "#34d399", "#f87171", "#fbbf24", "#a78bfa", "#22d3ee"];

function fmtNum(n) {
  if (n == null) return "—";
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
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

export default function AgentReviewPage() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [stats, setStats] = useState(null);
  const [queries, setQueries] = useState([]);
  const [corrected, setCorrected] = useState(() => new Set());
  const [busyId, setBusyId] = useState(null);
  const dailyRef = useRef(null);
  const statusRef = useRef(null);
  const errorRef = useRef(null);

  // One-click correction loop: promote this query's SQL to a Q&A knowledge
  // pair so the same question class generates correctly next time. A
  // rejected record carries known-bad SQL, so the admin must supply the fix.
  const correct = useCallback(async (q) => {
    let sqlTemplate = "";
    if (q.status === "rejected") {
      const typed = window.prompt(
        `纠正「${q.question}」——输入正确的 SQL 以存入知识库`,
        q.sql || "",
      );
      if (typed === null) return;  // cancelled
      if (!typed.trim()) {
        setError("Rejected queries need a corrected SQL.");
        return;
      }
      sqlTemplate = typed.trim();
    }
    setBusyId(q.id);
    setError("");
    try {
      const resp = await fetch(`/gw/api/agent/queries/${q.id}/correction`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sql_template: sqlTemplate }),
      });
      if (resp.status === 401) throw new Error("Sign in as an administrator");
      if (resp.status === 403) throw new Error("Administrator access required");
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        throw new Error(detail.detail || `HTTP ${resp.status}`);
      }
      setCorrected((prev) => new Set(prev).add(q.id));
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusyId(null);
    }
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [statsResp, queriesResp] = await Promise.all([
        fetch("/gw/api/agent/stats", { credentials: "include" }),
        fetch("/gw/api/agent/queries?page_size=50", { credentials: "include" }),
      ]);
      if (statsResp.status === 401 || queriesResp.status === 401) {
        throw new Error("Sign in as an administrator");
      }
      if (statsResp.status === 403 || queriesResp.status === 403) {
        throw new Error("Administrator access required");
      }
      if (!statsResp.ok) throw new Error(`HTTP ${statsResp.status}`);
      if (!queriesResp.ok) throw new Error(`HTTP ${queriesResp.status}`);
      setStats(await statsResp.json());
      setQueries((await queriesResp.json()).items);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const dailyOption = useMemo(() => stats && {
    color: PALETTE,
    tooltip: { trigger: "axis" },
    legend: { data: ["Total", "Success", "Error", "Rejected"], top: 0 },
    grid: { left: 48, right: 16, top: 32, bottom: 28 },
    xAxis: { type: "category", data: stats.daily.map((d) => d.date) },
    yAxis: { type: "value" },
    series: [
      { name: "Total", type: "bar", data: stats.daily.map((d) => d.total) },
      { name: "Success", type: "line", data: stats.daily.map((d) => d.success) },
      { name: "Error", type: "line", data: stats.daily.map((d) => d.error) },
      { name: "Rejected", type: "line", data: stats.daily.map((d) => d.rejected) },
    ],
  }, [stats]);

  const statusOption = useMemo(() => stats && {
    color: PALETTE,
    tooltip: { trigger: "item" },
    series: [{
      type: "pie",
      radius: ["35%", "70%"],
      data: [
        { name: "Success", value: stats.success },
        { name: "Error", value: stats.error },
        { name: "Blocked", value: stats.blocked },
        { name: "Rejected", value: stats.rejected },
      ].filter((d) => d.value > 0),
    }],
  }, [stats]);

  const errorOption = useMemo(() => stats && stats.error_top.length && {
    color: PALETTE,
    tooltip: { trigger: "axis" },
    grid: { left: 48, right: 16, top: 16, bottom: 28 },
    xAxis: { type: "value" },
    yAxis: { type: "category", data: stats.error_top.map((e) => String(e.error).slice(0, 40)).reverse() },
    series: [{ type: "bar", data: stats.error_top.map((e) => e.count).reverse() }],
  }, [stats]);

  useChart(dailyRef, dailyOption);
  useChart(statusRef, statusOption);
  useChart(errorRef, errorOption);

  return (
    <main className="admin-shell">
      <header className="admin-hero">
        <div>
          <p className="settings-kicker">DATA AGENT / REVIEW</p>
          <h1>Agent usage review</h1>
          <p>How often the data agent succeeds, what it fails on, and how much it costs in tokens.</p>
        </div>
        <a className="agent-nav-link" href="/agent/knowledge">Knowledge →</a>
      </header>

      {error && <div className="form-error">{error}</div>}
      {loading && <p className="muted">Loading…</p>}
      {!loading && stats && (
        <>
          <section className="kpi-grid">
            <div className="kpi-card"><div className="kpi-value">{fmtNum(stats.total)}</div><div className="kpi-label">Total queries</div></div>
            <div className="kpi-card"><div className="kpi-value">{stats.success_rate ? `${(stats.success_rate * 100).toFixed(1)}%` : "—"}</div><div className="kpi-label">Success rate</div></div>
            <div className="kpi-card"><div className="kpi-value">{stats.avg_duration_ms != null ? `${fmtNum(stats.avg_duration_ms)} ms` : "—"}</div><div className="kpi-label">Avg duration</div></div>
            <div className="kpi-card"><div className="kpi-value">{fmtNum((stats.prompt_tokens || 0) + (stats.completion_tokens || 0))}</div><div className="kpi-label">Total tokens</div></div>
          </section>

          <section className="review-charts">
            <div className="review-chart-card"><h3>Daily queries</h3><div ref={dailyRef} style={{ height: 260 }} /></div>
            <div className="review-chart-card"><h3>Status distribution</h3><div ref={statusRef} style={{ height: 260 }} /></div>
            {errorOption && (
              <div className="review-chart-card review-chart-wide"><h3>Top errors</h3><div ref={errorRef} style={{ height: 260 }} /></div>
            )}
          </section>

          <section className="review-table-wrap">
            <h3>Recent queries</h3>
            <table className="review-table">
              <thead>
                <tr><th>Time</th><th>User</th><th>Question</th><th>Status</th><th>Rows</th><th>Duration</th><th>Tokens</th><th></th></tr>
              </thead>
              <tbody>
                {queries.map((q) => (
                  <tr key={q.id}>
                    <td className="muted">{String(q.timestamp).slice(0, 19).replace("T", " ")}</td>
                    <td>{q.user}</td>
                    <td className="review-question" title={q.sql || q.error || ""}>{q.question}</td>
                    <td><span className={`badge badge-${q.status}`}>{q.status}</span></td>
                    <td>{q.rows_returned ?? "—"}</td>
                    <td>{q.duration_ms != null ? `${q.duration_ms.toFixed(0)} ms` : "—"}</td>
                    <td>{q.prompt_tokens != null ? q.prompt_tokens + (q.completion_tokens || 0) : "—"}</td>
                    <td>
                      {corrected.has(q.id) ? (
                        <span className="badge badge-success">stored</span>
                      ) : q.sql ? (
                        <button
                          className="btn-mini"
                          onClick={() => correct(q)}
                          disabled={busyId === q.id}
                          title="Save this question → SQL pair as agent knowledge"
                        >
                          {busyId === q.id ? "…" : "纠错"}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {queries.length === 0 && <p className="muted">No agent queries recorded yet.</p>}
          </section>
        </>
      )}
    </main>
  );
}
