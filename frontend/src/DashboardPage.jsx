import { useCallback, useEffect, useRef, useState } from "react";
import * as echarts from "echarts";
import AgentChat from "./AgentChat";

const PALETTE = ["#5b8def", "#34d399", "#f87171", "#fbbf24", "#a78bfa", "#22d3ee"];

async function fetchStats(path) {
  // In production the gateway sits behind the /gw path prefix (Caddy); in
  // dev, vite proxies /gw to the gateway and strips the prefix.
  const resp = await fetch(`/gw/api/stats/${path}`, { credentials: "include" });
  if (resp.status === 401) throw new Error("Sign in as an administrator");
  if (resp.status === 403) throw new Error("Administrator access required");
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return (await resp.json()).items;
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

function fmtNum(n) {
  if (n == null) return "—";
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

function fmtUsd(n) {
  if (n == null) return "—";
  if (n === 0) return "$0";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function seriesBase() {
  return {
    color: PALETTE,
    tooltip: { trigger: "axis" },
    grid: { left: 52, right: 16, top: 32, bottom: 28 },
  };
}

function days(tokens) {
  return tokens.map((r) => r.date);
}

export default function DashboardPage() {
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [data, setData] = useState(null);
  const [chatOpen, setChatOpen] = useState(false);

  const tokensRef = useRef(null);
  const requestsRef = useRef(null);
  const latencyRef = useRef(null);
  const modelsRef = useRef(null);
  const usersRef = useRef(null);
  const cacheRef = useRef(null);
  const costRef = useRef(null);
  const costUsersRef = useRef(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams();
      if (startDate) params.set("start_date", startDate);
      if (endDate) params.set("end_date", endDate);
      const qs = params.toString();
      const suffix = qs ? `?${qs}` : "";
      const extra = qs ? `&${qs}` : "";
      const [tokens, requests, models, users, cache, cost, costUsers] = await Promise.all([
        fetchStats(`tokens${suffix}`),
        fetchStats(`requests${suffix}`),
        fetchStats(`models?limit=10${extra}`),
        fetchStats(`tokens?group_by=user${extra}`),
        fetchStats(`cache${suffix}`),
        fetchStats(`cost${suffix}`),
        fetchStats(`cost?group_by=user${extra}`),
      ]);
      setData({ tokens, requests, models, users, cache, cost, costUsers });
    } catch (cause) {
      setError(cause.message);
    } finally {
      setLoading(false);
    }
  }, [startDate, endDate]);

  useEffect(() => { load(); }, [load]);

  const tokenOption = data && {
    ...seriesBase(),
    legend: { data: ["Prompt", "Completion"], top: 0 },
    xAxis: { type: "category", data: days(data.tokens) },
    yAxis: { type: "value" },
    series: [
      { name: "Prompt", type: "bar", stack: "t", data: data.tokens.map((r) => r.prompt_tokens) },
      { name: "Completion", type: "bar", stack: "t", data: data.tokens.map((r) => r.completion_tokens) },
    ],
  };

  const requestOption = data && {
    ...seriesBase(),
    legend: { data: ["Requests", "Errors"], top: 0 },
    xAxis: { type: "category", data: days(data.requests) },
    yAxis: { type: "value" },
    series: [
      { name: "Requests", type: "bar", data: data.requests.map((r) => r.requests) },
      { name: "Errors", type: "bar", data: data.requests.map((r) => r.errors) },
    ],
  };

  const latencyOption = data && {
    ...seriesBase(),
    xAxis: { type: "category", data: days(data.requests) },
    yAxis: { type: "value", name: "ms" },
    series: [{ name: "Avg latency", type: "line", smooth: true, areaStyle: {}, data: data.requests.map((r) => r.avg_duration_ms) }],
  };

  const modelsOption = data && {
    ...seriesBase(),
    tooltip: { trigger: "item" },
    grid: { left: 16, right: 60, top: 8, bottom: 8 },
    xAxis: { type: "value" },
    yAxis: { type: "category", data: data.models.map((r) => r.model) },
    series: [{ name: "Total tokens", type: "bar", data: data.models.map((r) => r.total_tokens) }],
  };

  const usersOption = data && {
    ...seriesBase(),
    tooltip: { trigger: "item" },
    grid: { left: 16, right: 60, top: 8, bottom: 8 },
    xAxis: { type: "value" },
    yAxis: { type: "category", data: data.users.map((r) => r.user) },
    series: [{ name: "Total tokens", type: "bar", data: data.users.map((r) => r.prompt_tokens + r.completion_tokens) }],
  };

  const cacheOption = data && {
    ...seriesBase(),
    xAxis: { type: "category", data: days(data.cache) },
    yAxis: { type: "value", max: 1, name: "hit ratio" },
    series: [{ name: "Cache hit ratio", type: "line", smooth: true, data: data.cache.map((r) => r.hit_ratio) }],
  };

  const costOption = data && {
    ...seriesBase(),
    xAxis: { type: "category", data: data.cost.map((r) => r.date) },
    yAxis: { type: "value", name: "USD" },
    series: [{ name: "Estimated cost", type: "line", smooth: true, areaStyle: {}, data: data.cost.map((r) => r.cost) }],
  };

  const costUsersOption = data && {
    ...seriesBase(),
    tooltip: { trigger: "item" },
    grid: { left: 16, right: 60, top: 8, bottom: 8 },
    xAxis: { type: "value" },
    yAxis: { type: "category", data: data.costUsers.map((r) => r.user) },
    series: [{ name: "Estimated cost (USD)", type: "bar", data: data.costUsers.map((r) => r.cost) }],
  };

  useChart(tokensRef, tokenOption);
  useChart(requestsRef, requestOption);
  useChart(latencyRef, latencyOption);
  useChart(modelsRef, modelsOption);
  useChart(usersRef, usersOption);
  useChart(cacheRef, cacheOption);
  useChart(costRef, costOption);
  useChart(costUsersRef, costUsersOption);

  const totalRequests = data?.requests.reduce((a, r) => a + r.requests, 0) ?? 0;
  const totalErrors = data?.requests.reduce((a, r) => a + r.errors, 0) ?? 0;
  const totalTokens = (data?.tokens.reduce((a, r) => a + r.prompt_tokens + r.completion_tokens, 0)) ?? 0;
  const totalCost = data?.cost.reduce((a, r) => a + r.cost, 0) ?? 0;

  return (
    <main className="admin-shell">
      <header className="admin-hero">
        <div>
          <p className="settings-kicker">OBSERVABILITY / DASHBOARD</p>
          <h1>Audit analytics at a glance.</h1>
          <p>Token usage, request volume, errors, latency, top models and cache efficiency from the gateway audit log.</p>
        </div>
        <div className="dashboard-filters">
          <label>Start
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          </label>
          <label>End
            <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          </label>
        </div>
      </header>

      {error && <div className="error-msg">{error}</div>}
      {loading && <div className="loading">Loading dashboard…</div>}

      {data && !loading && (
        <>
          <section className="dashboard-cards">
            <div className="dashboard-card"><span className="label">Requests</span><strong>{fmtNum(totalRequests)}</strong><small>{fmtNum(totalErrors)} errors ({totalRequests ? (100 * totalErrors / totalRequests).toFixed(1) : "0"}%)</small></div>
            <div className="dashboard-card"><span className="label">Total tokens</span><strong>{fmtNum(totalTokens)}</strong><small>prompt + completion</small></div>
            <div className="dashboard-card"><span className="label">Avg latency</span><strong>{data.requests.length ? (data.requests.reduce((a, r) => a + r.avg_duration_ms * r.requests, 0) / totalRequests).toFixed(0) : "—"} ms</strong><small>duration weighted</small></div>
            <div className="dashboard-card"><span className="label">Top model</span><strong>{data.models[0]?.model ?? "—"}</strong><small>{fmtNum(data.models[0]?.total_tokens ?? 0)} tokens</small></div>
            <div className="dashboard-card"><span className="label">Estimated cost</span><strong>{fmtUsd(totalCost)}</strong><small>{data.costUsers[0] ? `${data.costUsers[0].user} leads at ${fmtUsd(data.costUsers[0].cost)}` : "no priced models"}</small></div>
          </section>

          <div className="dashboard-grid">
            <div className="dashboard-panel wide"><h2>Token usage</h2><div ref={tokensRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Requests & errors</h2><div ref={requestsRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Average latency</h2><div ref={latencyRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Cache hit ratio</h2><div ref={cacheRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Top models</h2><div ref={modelsRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Top users</h2><div ref={usersRef} className="chart" /></div>
            <div className="dashboard-panel wide"><h2>Estimated cost over time</h2><div ref={costRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Cost by user</h2><div ref={costUsersRef} className="chart" /></div>
          </div>
        </>
      )}

      {/* Floating data-agent chatbot (requirement 5) */}
      <button
        className="chatbot-fab"
        onClick={() => setChatOpen((open) => !open)}
        aria-label={chatOpen ? "Close data agent chat" : "Open data agent chat"}
      >
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <rect x="4" y="8" width="16" height="12" rx="3" stroke="currentColor" strokeWidth="1.8" />
          <path d="M12 8V5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          <circle cx="12" cy="4" r="1.6" fill="currentColor" />
          <circle cx="9" cy="13.5" r="1.4" fill="currentColor" />
          <circle cx="15" cy="13.5" r="1.4" fill="currentColor" />
          <path d="M9.5 17h5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
      </button>

      {chatOpen && (
        <div className="chatbot-panel">
          <AgentChat variant="panel" onClose={() => setChatOpen(false)} />
        </div>
      )}
    </main>
  );
}
