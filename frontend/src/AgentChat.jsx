import { useEffect, useRef, useState } from "react";
import echarts from "./echarts-lite";

/* Shared data-agent chat: session loop, real SSE streaming, human SQL
 * confirmation. Used full-page by /agent and as a floating panel on
 * /dashboard. */

function parseSseBlock(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (dataLines.length === 0) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}

async function consumeSse(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    buffer = buffer.replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const parsed = parseSseBlock(block);
      if (parsed) onEvent(parsed);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  if (buffer.trim()) {
    const parsed = parseSseBlock(buffer.trim());
    if (parsed) onEvent(parsed);
  }
}

function ResultTable({ result }) {
  if (!result || !result.rows) return null;
  if (result.rows.length === 0) {
    return <div className="chat-hint">No rows returned.</div>;
  }
  return (
    <div className="chat-table-wrap">
      <table className="chat-table">
        <thead>
          <tr>{result.columns.map((col) => <th key={col}>{col}</th>)}</tr>
        </thead>
        <tbody>
          {result.rows.map((row, i) => (
            <tr key={i}>
              {result.columns.map((col) => (
                <td key={col}>{String(row[col] ?? "")}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {result.truncated && (
        <div className="chat-hint">Results were truncated to the row limit.</div>
      )}
    </div>
  );
}

function ResultChart({ chart, result }) {
  const ref = useRef(null);

  useEffect(() => {
    if (!ref.current || !chart || !result?.rows?.length) return undefined;
    const ctype = chart.type;
    const x = chart.x;
    const series = chart.series;
    if (!result.columns.includes(x) || !result.columns.includes(series)) {
      return undefined;
    }
    const categories = result.rows.map((r) => String(r[x] ?? ""));
    const values = result.rows.map((r) => Number(r[series] ?? 0));
    const option = ctype === "pie"
      ? {
          tooltip: { trigger: "item" },
          series: [{
            type: "pie",
            radius: ["35%", "70%"],
            data: result.rows.map((r) => ({ name: String(r[x] ?? ""), value: Number(r[series] ?? 0) })),
          }],
        }
      : {
          tooltip: { trigger: "axis" },
          grid: { left: 48, right: 16, top: 24, bottom: 32 },
          xAxis: { type: "category", data: categories },
          yAxis: { type: "value" },
          series: [{ name: series, type: ctype === "bar" ? "bar" : "line", data: values, smooth: true }],
        };
    const chartInstance = echarts.init(ref.current);
    chartInstance.setOption(option);
    const onResize = () => chartInstance.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chartInstance.dispose();
    };
  }, [chart, result]);

  return <div ref={ref} className="chat-chart" style={{ height: 280 }} />;
}

export default function AgentChat({ variant = "panel", onClose }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [sessionId, setSessionId] = useState(null);
  const [pendingConfirm, setPendingConfirm] = useState(null); // {sql, attempt}
  const [presets, setPresets] = useState([]); // one-click preset questions
  const scrollRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/gw/api/agent/presets?enabled_only=true", { credentials: "include" })
      .then((resp) => (resp.ok ? resp.json() : { items: [] }))
      .then((data) => { if (!cancelled) setPresets(data.items || []); })
      .catch(() => { if (!cancelled) setPresets([]); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, status, pendingConfirm]);

  const appendMessage = (msg) => {
    setMessages((prev) => [...prev, msg]);
  };

  const updateLastAssistant = (updater) => {
    setMessages((prev) => {
      for (let i = prev.length - 1; i >= 0; i -= 1) {
        if (prev[i].role === "assistant") {
          const next = [...prev];
          next[i] = updater({ ...next[i] });
          return next;
        }
      }
      return prev;
    });
  };

  const runStream = async (url, body) => {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.message || `HTTP ${resp.status}`);
    }
    if (!resp.body) throw new Error("Streaming response is unavailable");

    let sawError = "";
    await consumeSse(resp, ({ event, data }) => {
      if (event === "session" && data.session_id) setSessionId(data.session_id);
      else if (event === "rewrite" && data.rewritten && data.rewritten !== data.original) {
        appendMessage({ role: "system", kind: "info", text: `Rewrote to: ${data.rewritten}` });
      } else if (event === "status") setStatus(data.message || "");
      else if (event === "delta") {
        updateLastAssistant((msg) => ({
          ...msg,
          streaming: true,
          text: (msg.text || "") + (data.text || ""),
        }));
      } else if (event === "retrying") {
        setStatus(`Reflecting and retrying (${data.reason || "error"})…`);
        appendMessage({ role: "system", kind: "retry", text: data.reason ? `Retrying: ${data.reason}` : "Retrying…" });
      } else if (event === "blocked") {
        appendMessage({ role: "system", kind: "blocked", text: `Blocked: ${data.reason}`, sql: data.sql });
      } else if (event === "message") {
        updateLastAssistant((msg) => ({ ...msg, text: data.message || msg.text, streaming: false }));
      } else if (event === "confirmation_required") {
        setPendingConfirm({ sql: data.sql, attempt: data.attempt ?? 1 });
        setStatus("Waiting for confirmation…");
      } else if (event === "result") {
        updateLastAssistant((msg) => ({ ...msg, streaming: false, result: data }));
        setStatus("");
      } else if (event === "rejected") {
        setPendingConfirm(null);
        appendMessage({ role: "system", kind: "info", text: "SQL execution was rejected." });
        setStatus("");
      } else if (event === "error") {
        sawError = data.message || "Agent stream failed";
        appendMessage({ role: "system", kind: "error", text: sawError });
        setStatus("");
      } else if (event === "done") {
        setStatus("");
      }
    });
    if (sawError) throw new Error(sawError);
  };

  const sendText = async (text) => {
    const clean = (text || "").trim();
    if (!clean || busy) return;
    setInput("");
    setBusy(true);
    setStatus("Thinking…");
    appendMessage({ role: "user", text: clean });
    appendMessage({ role: "assistant", text: "", streaming: true });
    setPendingConfirm(null);
    try {
      await runStream("/gw/api/agent/chat", {
        message: clean,
        session_id: sessionId,
      });
    } catch (cause) {
      setStatus("");
      appendMessage({ role: "system", kind: "error", text: cause.message });
    } finally {
      setBusy(false);
    }
  };

  const send = async (event) => {
    event.preventDefault();
    await sendText(input);
  };

  const confirm = async (approved) => {
    if (busy) return;
    const pending = pendingConfirm;
    setBusy(true);
    setPendingConfirm(null);
    setStatus(approved ? "Executing approved SQL…" : "Rejecting…");
    try {
      await runStream("/gw/api/agent/chat/confirm", {
        session_id: sessionId,
        approved,
      });
    } catch (cause) {
      setStatus("");
      appendMessage({ role: "system", kind: "error", text: cause.message });
    } finally {
      setBusy(false);
      if (!approved && pending) {
        appendMessage({ role: "system", kind: "info", text: "You rejected the SQL — nothing was executed." });
      }
    }
  };

  return (
    <div className={`chat-root chat-${variant}`}>
      <div className="chat-header">
        <div className="chat-title">
          <span className="chat-dot" aria-hidden="true" />
          <span>Data Agent</span>
        </div>
        {onClose && (
          <button className="chat-close" onClick={onClose} aria-label="Close chat">✕</button>
        )}
      </div>

      <div className="chat-body" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="chat-empty">
            Ask anything about the gateway audit log.
            <div className="chat-empty-sub">
              Natural-language questions become read-only SQL — executed only after your confirmation.
            </div>
            {presets.length > 0 && (
              <div className="chat-presets">
                {presets.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    className="chat-preset"
                    disabled={busy}
                    onClick={() => sendText(p.question)}
                  >
                    {p.question}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {messages.map((msg, i) => {
          if (msg.role === "user") {
            return <div key={i} className="chat-msg chat-user"><span>{msg.text}</span></div>;
          }
          if (msg.role === "system") {
            return (
              <div key={i} className={`chat-msg chat-system chat-system-${msg.kind || "info"}`}>
                {msg.sql && <pre className="chat-sql">{msg.sql}</pre>}
                <span>{msg.text}</span>
              </div>
            );
          }
          return (
            <div key={i} className="chat-msg chat-assistant">
              {msg.text && <span className="chat-text">{msg.text}{msg.streaming && <span className="chat-cursor" />}</span>}
              {!msg.text && msg.streaming && <span className="chat-text"><span className="chat-cursor" /></span>}
              {msg.result && (
                <>
                  <pre className="chat-sql">{msg.result.sql}</pre>
                  <ResultTable result={msg.result} />
                  {msg.result.chart && <ResultChart chart={msg.result.chart} result={msg.result} />}
                </>
              )}
            </div>
          );
        })}

        {pendingConfirm && !busy && (
          <div className="chat-confirm">
            <div className="chat-confirm-label">
              The agent wants to run this SQL. Approve execution?
              {pendingConfirm.attempt > 1 && ` (attempt ${pendingConfirm.attempt})`}
            </div>
            <pre className="chat-sql">{pendingConfirm.sql}</pre>
            <div className="chat-confirm-actions">
              <button className="btn-confirm-approve" onClick={() => confirm(true)}>
                Approve & run
              </button>
              <button className="btn-confirm-reject" onClick={() => confirm(false)}>
                Reject
              </button>
            </div>
          </div>
        )}
      </div>

      {status && (
        <div className="chat-status">
          <span className="chat-live-dot" aria-hidden="true" />
          {status}
        </div>
      )}

      <form className="chat-form" onSubmit={send}>
        <input
          className="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="e.g. How many requests per user in the last 7 days?"
          disabled={busy}
        />
        <button className="chat-send" type="submit" disabled={busy || !input.trim()}>
          {busy ? "…" : "Send"}
        </button>
      </form>
    </div>
  );
}
