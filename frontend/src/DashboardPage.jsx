import { useCallback, useEffect, useRef, useState } from "react";
import * as echarts from "echarts";
import AgentChat from "./AgentChat";

const PALETTE = ["#5b8def", "#34d399", "#f87171", "#fbbf24", "#a78bfa", "#22d3ee"];
const AUTO_REFRESH_MS = 60_000;

async function fetchStats(path) {
  // In production the gateway sits behind the /gw path prefix (Caddy); in
  // dev, vite proxies /gw to the gateway and strips the prefix.
  const resp = await fetch(`/gw/api/stats/${path}`, { credentials: "include" });
  if (resp.status === 401) throw new Error("Sign in as an administrator");
  if (resp.status === 403) throw new Error("Administrator access required");
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return (await resp.json()).items;
}

async function fetchJson(path) {
  const resp = await fetch(path, { credentials: "include" });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function fetchOpenAlerts() {
  // Anomaly alerts are advisory — a failed fetch must not break the dashboard.
  try {
    const alerts = await fetchJson("/gw/api/alerts");
    return alerts.items;
  } catch {
    return [];
  }
}

function useChart(ref, option, onClick) {
  useEffect(() => {
    if (!ref.current || !option) return undefined;
    const chart = echarts.init(ref.current);
    chart.setOption(option);
    if (onClick) chart.on("click", onClick);
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, [ref, option, onClick]);
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

function days(rows) {
  return rows.map((r) => r.date);
}

function sumTokens(rows) {
  // Match provider-reported volume: prompt + completion + cache read/write
  // (agentic traffic is dominated by cache reads; prompt-only sums mislead).
  return rows.reduce(
    (a, r) => a + r.prompt_tokens + r.completion_tokens
      + r.cache_read_tokens + r.cache_creation_tokens,
    0,
  );
}

function addDays(iso, delta) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + delta);
  return d.toISOString().slice(0, 10);
}

function dayCount(start, end) {
  const ms = Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`);
  return Math.round(ms / 86_400_000) + 1;
}

/** Previous period of equal length immediately before [start, end]. */
function previousRange(start, end) {
  const n = dayCount(start, end);
  const prevEnd = addDays(start, -1);
  return { start: addDays(prevEnd, -(n - 1)), end: prevEnd, days: n };
}

/** Percent change of cur vs prev; null when there is no comparable base. */
function deltaPct(cur, prev) {
  if (prev == null || prev === 0) return null;
  return (100 * (cur - prev)) / prev;
}

function DeltaTag({ pct, label }) {
  if (pct == null) return null;
  const rounded = Math.abs(pct) >= 10 ? pct.toFixed(0) : pct.toFixed(1);
  const dir = pct > 0.05 ? "up" : pct < -0.05 ? "down" : "flat";
  const sign = pct > 0.05 ? "▲" : pct < -0.05 ? "▼" : "•";
  return (
    <span className={`delta-tag delta-${dir}`} title={label}>
      {sign} {Math.abs(Number(rounded))}% {label}
    </span>
  );
}

export default function DashboardPage() {
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [userFilter, setUserFilter] = useState("");
  const [errorGroup, setErrorGroup] = useState("status");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [data, setData] = useState(null);
  const [prevData, setPrevData] = useState(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [alerts, setAlerts] = useState([]);
  const [scanning, setScanning] = useState(false);
  const [lastRefresh, setLastRefresh] = useState("");

  const loadAlerts = useCallback(async () => {
    setAlerts(await fetchOpenAlerts());
  }, []);

  const ackAlert = useCallback(async (id) => {
    try {
      const resp = await fetch(`/gw/api/alerts/${id}/ack`, {
        method: "POST", credentials: "include",
      });
      if (resp.ok) setAlerts((prev) => prev.filter((a) => a.id !== id));
    } catch {
      // Banner stays; the next refresh retries.
    }
  }, []);

  const scanNow = useCallback(async () => {
    setScanning(true);
    try {
      await fetch("/gw/api/alerts/scan", { method: "POST", credentials: "include" });
      await loadAlerts();
    } catch {
      // A failed manual scan keeps the previous banner.
    } finally {
      setScanning(false);
    }
  }, [loadAlerts]);

  const tokensRef = useRef(null);
  const requestsRef = useRef(null);
  const latencyRef = useRef(null);
  const modelsRef = useRef(null);
  const usersRef = useRef(null);
  const servicesRef = useRef(null);
  const cacheRef = useRef(null);
  const costRef = useRef(null);
  const costUsersRef = useRef(null);
  const errorsRef = useRef(null);

  const load = useCallback(async ({ silent = false } = {}) => {
    if (!silent) {
      setLoading(true);
      setError("");
    }
    try {
      const filters = new URLSearchParams();
      if (startDate) filters.set("start_date", startDate);
      if (endDate) filters.set("end_date", endDate);
      const userQs = new URLSearchParams(filters);
      if (userFilter) userQs.set("user", userFilter);
      const qm = (p) => (p.toString() ? `?${p}` : "");
      const amp = (p) => (p.toString() ? `&${p}` : "");

      const [tokens, requests, models, users, services, cache, cost, costUsers, latency, errors] = await Promise.all([
        fetchStats(`tokens${qm(userQs)}`),
        fetchStats(`requests${qm(userQs)}`),
        fetchStats(`models?limit=10${amp(userQs)}`),
        // The user leaderboard stays unfiltered: it also feeds the
        // dropdown options, which must not collapse to the selection.
        fetchStats(`tokens?group_by=user${amp(filters)}`),
        fetchStats(`tokens?group_by=service${amp(userQs)}`),
        fetchStats(`cache${qm(userQs)}`),
        fetchStats(`cost${qm(userQs)}`),
        fetchStats(`cost?group_by=user${amp(userQs)}`),
        fetchJson(`/gw/api/stats/latency${qm(userQs)}`),
        fetchStats(`errors?group_by=${errorGroup}${amp(userQs)}`),
      ]);
      setData({ tokens, requests, models, users, services, cache, cost, costUsers, latency, errors });

      // Period-over-period only makes sense against an explicit window.
      if (startDate && endDate) {
        const prev = previousRange(startDate, endDate);
        const prevParams = new URLSearchParams();
        prevParams.set("start_date", prev.start);
        prevParams.set("end_date", prev.end);
        if (userFilter) prevParams.set("user", userFilter);
        const ps = `?${prevParams.toString()}`;
        const [prevRequests, prevTokens, prevCost] = await Promise.all([
          fetchStats(`requests${ps}`),
          fetchStats(`tokens${ps}`),
          fetchStats(`cost${ps}`),
        ]);
        setPrevData({ requests: prevRequests, tokens: prevTokens, cost: prevCost });
      } else {
        setPrevData(null);
      }
      setLastRefresh(new Date().toLocaleTimeString());
    } catch (cause) {
      // Silent refresh failures keep the last good data on screen.
      if (!silent) setError(cause.message);
    } finally {
      if (!silent) setLoading(false);
    }
  }, [startDate, endDate, userFilter, errorGroup]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadAlerts(); }, [loadAlerts]);

  // Auto-refresh keeps this an observability page rather than a snapshot.
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) load({ silent: true });
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const tokenOption = data && {
    ...seriesBase(),
    legend: { data: ["Prompt", "Completion", "Cache Read", "Cache Create"], top: 0 },
    xAxis: { type: "category", data: days(data.tokens) },
    yAxis: { type: "value" },
    // Agentic traffic is dominated by cache reads; excluding them made the
    // chart contradict the Total tokens card (prompt-only bars ~100x smaller).
    series: [
      { name: "Prompt", type: "bar", stack: "t", data: data.tokens.map((r) => r.prompt_tokens) },
      { name: "Completion", type: "bar", stack: "t", data: data.tokens.map((r) => r.completion_tokens) },
      { name: "Cache Read", type: "bar", stack: "t", data: data.tokens.map((r) => r.cache_read_tokens) },
      { name: "Cache Create", type: "bar", stack: "t", data: data.tokens.map((r) => r.cache_creation_tokens) },
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

  // Drill-down: clicking a day opens the gateway audit viewer pre-filtered
  // to that day (logs.html reads the query params on load).
  const onRequestsClick = useCallback((params) => {
    if (!params || !params.name) return;
    const url = `/gw/audit?start_date=${encodeURIComponent(params.name)}&end_date=${encodeURIComponent(params.name)}`;
    window.open(url, "_blank", "noopener");
  }, []);

  const latencyOption = data && {
    ...seriesBase(),
    legend: { data: ["P95", "P50", "Avg"], top: 0 },
    xAxis: { type: "category", data: days(data.requests) },
    yAxis: { type: "value", name: "ms" },
    series: [
      { name: "P95", type: "line", smooth: true, areaStyle: {}, data: data.requests.map((r) => r.p95) },
      { name: "P50", type: "line", smooth: true, data: data.requests.map((r) => r.p50) },
      { name: "Avg", type: "line", smooth: true, lineStyle: { type: "dashed" }, data: data.requests.map((r) => r.avg_duration_ms) },
    ],
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
    series: [{ name: "Total tokens", type: "bar", data: data.users.map((r) => r.prompt_tokens + r.completion_tokens + r.cache_read_tokens + r.cache_creation_tokens) }],
  };

  const servicesOption = data && {
    ...seriesBase(),
    tooltip: { trigger: "item" },
    grid: { left: 16, right: 60, top: 8, bottom: 8 },
    xAxis: { type: "value" },
    yAxis: { type: "category", data: data.services.map((r) => r.service ?? "(none)") },
    series: [{ name: "Total tokens", type: "bar", data: data.services.map((r) => r.prompt_tokens + r.completion_tokens + r.cache_read_tokens + r.cache_creation_tokens) }],
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

  const errorsOption = data && {
    color: PALETTE,
    tooltip: { trigger: "item" },
    series: [{
      type: "pie",
      radius: ["38%", "68%"],
      data: data.errors.map((r) => ({ name: String(r.key), value: r.count })),
    }],
  };

  useChart(tokensRef, tokenOption);
  useChart(requestsRef, requestOption, onRequestsClick);
  useChart(latencyRef, latencyOption);
  useChart(modelsRef, modelsOption);
  useChart(usersRef, usersOption);
  useChart(servicesRef, servicesOption);
  useChart(cacheRef, cacheOption);
  useChart(costRef, costOption);
  useChart(costUsersRef, costUsersOption);
  useChart(errorsRef, errorsOption);

  const totalRequests = data?.requests.reduce((a, r) => a + r.requests, 0) ?? 0;
  const totalErrors = data?.requests.reduce((a, r) => a + r.errors, 0) ?? 0;
  const totalTokens = data ? sumTokens(data.tokens) : 0;
  const totalCost = data?.cost.reduce((a, r) => a + r.cost, 0) ?? 0;

  const prevTotals = prevData && {
    requests: prevData.requests.reduce((a, r) => a + r.requests, 0),
    tokens: sumTokens(prevData.tokens),
    cost: prevData.cost.reduce((a, r) => a + r.cost, 0),
  };
  const windowLabel = startDate && endDate
    ? `vs prev ${dayCount(startDate, endDate)}d`
    : null;

  const periodLabel = startDate || endDate
    ? `${startDate || "…"} → ${endDate || "…"}`
    : "all time";

  return (
    <main className="admin-shell">
      <header className="admin-hero">
        <div>
          <p className="settings-kicker">OBSERVABILITY / DASHBOARD</p>
          <h1>Audit analytics at a glance.</h1>
          <p>Token usage, request volume, errors, latency, top models and cache efficiency from the gateway audit log.</p>
        </div>
        <div className="dashboard-filter-col">
          <div className="dashboard-filter-top">
            <a className="agent-nav-link" href="/dashboard/prices">Model prices →</a>
          </div>
          <div className="dashboard-filters">
            <label>Start
              <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
            </label>
            <label>End
              <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
            </label>
            <label>User
              <select value={userFilter} onChange={(e) => setUserFilter(e.target.value)}>
                <option value="">All users</option>
                {(data?.users ?? []).map((r) => (
                  <option key={r.user} value={r.user}>{r.user}</option>
                ))}
              </select>
            </label>
          </div>
        </div>
      </header>

      {error && <div className="error-msg">{error}</div>}

      {alerts.length > 0 && (
        <section className="alert-banner" aria-label="Anomaly alerts">
          <div className="alert-banner-head">
            <span className="alert-dot" />
            <strong>{alerts.length} anomaly alert{alerts.length > 1 ? "s" : ""}</strong>
            <span className="muted">— daily scan vs trailing 7-day baseline</span>
            <button className="btn-mini" onClick={scanNow} disabled={scanning}>
              {scanning ? "Scanning…" : "Scan now"}
            </button>
          </div>
          <ul>
            {alerts.map((a) => (
              <li key={a.id}>
                <span className="alert-day">{a.day}</span>
                <span className={`alert-kind kind-${a.kind}`}>{a.kind}</span>
                <span className="alert-msg">{a.message}</span>
                <button className="btn-mini" onClick={() => ackAlert(a.id)}>确认</button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {loading && <div className="loading">Loading dashboard…</div>}

      {data && !loading && (
        <>
          <section className="dashboard-cards">
            <div className="dashboard-card">
              <span className="label">Requests</span>
              <strong>{fmtNum(totalRequests)}</strong>
              <small>
                {fmtNum(totalErrors)} errors ({totalRequests ? (100 * totalErrors / totalRequests).toFixed(1) : "0"}%)
                {prevTotals && <DeltaTag pct={deltaPct(totalRequests, prevTotals.requests)} label={windowLabel} />}
              </small>
            </div>
            <div className="dashboard-card">
              <span className="label">Total tokens</span>
              <strong>{fmtNum(totalTokens)}</strong>
              <small>
                prompt + completion + cache
                {prevTotals && <DeltaTag pct={deltaPct(totalTokens, prevTotals.tokens)} label={windowLabel} />}
              </small>
            </div>
            <div className="dashboard-card">
              <span className="label">Latency P95</span>
              <strong>{data.latency ? `${fmtNum(data.latency.p95)} ms` : "—"}</strong>
              <small>avg {fmtNum(data.latency?.avg_duration_ms)} · p50 {fmtNum(data.latency?.p50)} · max {fmtNum(data.latency?.max_duration_ms)}</small>
            </div>
            <div className="dashboard-card">
              <span className="label">Top model</span>
              <strong>{data.models[0]?.model ?? "—"}</strong>
              <small>{fmtNum(data.models[0]?.total_tokens ?? 0)} tokens</small>
            </div>
            <div className="dashboard-card">
              <span className="label">Estimated cost</span>
              <strong>{fmtUsd(totalCost)}</strong>
              <small>
                {totalCost === 0
                  ? <a href="/dashboard/prices">Configure model prices →</a>
                  : (data.costUsers[0] ? `${data.costUsers[0].user} leads at ${fmtUsd(data.costUsers[0].cost)}` : "no priced models")}
                {prevTotals && <DeltaTag pct={deltaPct(totalCost, prevTotals.cost)} label={windowLabel} />}
              </small>
            </div>
          </section>

          <div className="dashboard-toolbar">
            <span className="muted">{periodLabel} · auto-refresh {AUTO_REFRESH_MS / 1000}s{lastRefresh ? ` · updated ${lastRefresh}` : ""}</span>
            <span className="muted">Click a bar in Requests & errors to drill into that day's audit log</span>
          </div>

          <div className="dashboard-grid">
            <div className="dashboard-panel wide"><h2>Token usage</h2><div ref={tokensRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Requests & errors</h2><div ref={requestsRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Latency p50 / p95 / avg</h2><div ref={latencyRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Cache hit ratio</h2><div ref={cacheRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>
              Errors
              <span className="chart-toggle">
                <button className={`btn-mini${errorGroup === "status" ? " active" : ""}`} onClick={() => setErrorGroup("status")}>by status</button>
                <button className={`btn-mini${errorGroup === "model" ? " active" : ""}`} onClick={() => setErrorGroup("model")}>by model</button>
              </span>
            </h2><div ref={errorsRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Top models</h2><div ref={modelsRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Top users</h2><div ref={usersRef} className="chart" /></div>
            <div className="dashboard-panel"><h2>Top services</h2><div ref={servicesRef} className="chart" /></div>
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
