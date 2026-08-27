import { useCallback, useEffect, useState } from "react";

const TABS = [
  { id: "qa", label: "Q&A pairs" },
  { id: "glossary", label: "Column glossary" },
  { id: "presets", label: "Preset questions" },
];

const EMPTY_QA = { question: "", sql_template: "", tags: "", enabled: true };
const EMPTY_GLOSSARY = {
  table_name: "", column_name: "", business_name: "", synonyms: "", description: "", enabled: true,
};
const EMPTY_PRESET = { question: "", sort_order: 0, enabled: true };

async function api(path, options = {}) {
  const resp = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (resp.status === 401 || resp.status === 403) {
    throw new Error(resp.status === 401 ? "Sign in as an administrator" : "Administrator access required");
  }
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function splitList(value) {
  return (value || "")
    .split(/[,，、]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function AgentKnowledgePage() {
  const [tab, setTab] = useState("qa");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [qa, setQa] = useState(EMPTY_QA);
  const [qaItems, setQaItems] = useState([]);
  const [glossary, setGlossary] = useState(EMPTY_GLOSSARY);
  const [glossaryItems, setGlossaryItems] = useState([]);
  const [preset, setPreset] = useState(EMPTY_PRESET);
  const [presetItems, setPresetItems] = useState([]);

  const load = useCallback(async () => {
    setError("");
    try {
      if (tab === "qa") {
        const data = await api("/gw/api/agent/qa");
        setQaItems(data.items || []);
      } else if (tab === "glossary") {
        const data = await api("/gw/api/agent/glossary");
        setGlossaryItems(data.items || []);
      } else {
        const data = await api("/gw/api/agent/presets");
        setPresetItems(data.items || []);
      }
    } catch (cause) {
      setError(cause.message);
    }
  }, [tab]);

  useEffect(() => { load(); }, [load]);

  const flash = (message) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 4000);
  };

  const submitQa = async (event) => {
    event.preventDefault();
    setError("");
    try {
      await api("/gw/api/agent/qa", {
        method: "POST",
        body: JSON.stringify({ ...qa, tags: splitList(qa.tags) }),
      });
      setQa(EMPTY_QA);
      flash("Q&A pair saved.");
      await load();
    } catch (cause) { setError(cause.message); }
  };

  const removeQa = async (id) => {
    try {
      await api(`/gw/api/agent/qa/${id}`, { method: "DELETE" });
      flash("Q&A pair deleted.");
      await load();
    } catch (cause) { setError(cause.message); }
  };

  const submitGlossary = async (event) => {
    event.preventDefault();
    setError("");
    try {
      await api("/gw/api/agent/glossary", {
        method: "POST",
        body: JSON.stringify({ ...glossary, synonyms: splitList(glossary.synonyms) }),
      });
      setGlossary(EMPTY_GLOSSARY);
      flash("Column glossary entry saved.");
      await load();
    } catch (cause) { setError(cause.message); }
  };

  const removeGlossary = async (id) => {
    try {
      await api(`/gw/api/agent/glossary/${id}`, { method: "DELETE" });
      flash("Column glossary entry deleted.");
      await load();
    } catch (cause) { setError(cause.message); }
  };

  const submitPreset = async (event) => {
    event.preventDefault();
    setError("");
    try {
      await api("/gw/api/agent/presets", {
        method: "POST",
        body: JSON.stringify(preset),
      });
      setPreset(EMPTY_PRESET);
      flash("Preset question saved.");
      await load();
    } catch (cause) { setError(cause.message); }
  };

  const removePreset = async (id) => {
    try {
      await api(`/gw/api/agent/presets/${id}`, { method: "DELETE" });
      flash("Preset question deleted.");
      await load();
    } catch (cause) { setError(cause.message); }
  };

  return (
    <main className="admin-shell">
      <header className="admin-hero">
        <div>
          <p className="settings-kicker">DATA AGENT / KNOWLEDGE</p>
          <h1>Teach the agent without touching prompts.</h1>
          <p>
            Q&amp;A pairs fix recurring SQL mistakes (few-shot), the column glossary explains column
            semantics, and preset questions are one-click shortcuts in the chat.
          </p>
        </div>
        <a className="agent-nav-link" href="/agent">Ask questions →</a>
      </header>

      {error && <div className="form-error">{error}</div>}
      {notice && <div className="form-notice">{notice}</div>}

      <nav className="knowledge-tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`knowledge-tab${tab === t.id ? " active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {tab === "qa" && (
        <section className="review-table-wrap">
          <form className="knowledge-form" onSubmit={submitQa}>
            <h3>Add a Q&amp;A pair</h3>
            <p className="muted">
              When a user question matches, the pair is injected as a few-shot example so the agent
              follows the SQL approach you show. The question should resemble a real user query.
            </p>
            <div className="knowledge-grid">
              <label className="knowledge-field knowledge-wide">
                Question pattern
                <input
                  value={qa.question}
                  onChange={(e) => setQa({ ...qa, question: e.target.value })}
                  placeholder="统计上个月的销售额"
                  required
                />
              </label>
              <label className="knowledge-field knowledge-wide">
                SQL approach (or CoT instructions)
                <textarea
                  value={qa.sql_template}
                  onChange={(e) => setQa({ ...qa, sql_template: e.target.value })}
                  placeholder="SELECT COUNT(*) AS n FROM audit_logs WHERE ..."
                  rows={3}
                  required
                />
              </label>
              <label className="knowledge-field">
                Tags (comma separated)
                <input
                  value={qa.tags}
                  onChange={(e) => setQa({ ...qa, tags: e.target.value })}
                  placeholder="sales, 月度"
                />
              </label>
              <label className="knowledge-check">
                <input
                  type="checkbox"
                  checked={qa.enabled}
                  onChange={(e) => setQa({ ...qa, enabled: e.target.checked })}
                />
                Enabled
              </label>
            </div>
            <div className="form-actions">
              <button className="btn-primary" type="submit">Save Q&amp;A pair</button>
            </div>
          </form>

          <h3>Saved pairs</h3>
          <table className="review-table">
            <thead>
              <tr><th>Question</th><th>SQL approach</th><th>Tags</th><th>Status</th><th /></tr>
            </thead>
            <tbody>
              {qaItems.length === 0 && (
                <tr><td colSpan={5} className="muted">No Q&amp;A pairs yet.</td></tr>
              )}
              {qaItems.map((item) => (
                <tr key={item.id}>
                  <td>{item.question}</td>
                  <td className="knowledge-sql">{item.sql_template}</td>
                  <td>{splitList(item.tags).join(", ")}</td>
                  <td>{item.enabled ? "enabled" : "disabled"}</td>
                  <td>
                    <button className="btn-danger" type="button" onClick={() => removeQa(item.id)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {tab === "glossary" && (
        <section className="review-table-wrap">
          <form className="knowledge-form" onSubmit={submitGlossary}>
            <h3>Add a column annotation</h3>
            <p className="muted">
              Business name, synonyms and enum semantics for a schema column. Rendered into every
              agent prompt so column meaning is resolved consistently.
            </p>
            <div className="knowledge-grid">
              <label className="knowledge-field">
                Table
                <input
                  value={glossary.table_name}
                  onChange={(e) => setGlossary({ ...glossary, table_name: e.target.value })}
                  placeholder="audit_logs"
                  required
                />
              </label>
              <label className="knowledge-field">
                Column
                <input
                  value={glossary.column_name}
                  onChange={(e) => setGlossary({ ...glossary, column_name: e.target.value })}
                  placeholder="status"
                  required
                />
              </label>
              <label className="knowledge-field">
                Business name
                <input
                  value={glossary.business_name}
                  onChange={(e) => setGlossary({ ...glossary, business_name: e.target.value })}
                  placeholder="HTTP 状态码"
                />
              </label>
              <label className="knowledge-field">
                Synonyms (comma separated)
                <input
                  value={glossary.synonyms}
                  onChange={(e) => setGlossary({ ...glossary, synonyms: e.target.value })}
                  placeholder="状态, http状态"
                />
              </label>
              <label className="knowledge-field knowledge-wide">
                Description (enum meanings / business logic)
                <textarea
                  value={glossary.description}
                  onChange={(e) => setGlossary({ ...glossary, description: e.target.value })}
                  placeholder="200=成功, 4xx=客户端错误, 5xx=服务端错误"
                  rows={2}
                />
              </label>
              <label className="knowledge-check">
                <input
                  type="checkbox"
                  checked={glossary.enabled}
                  onChange={(e) => setGlossary({ ...glossary, enabled: e.target.checked })}
                />
                Enabled
              </label>
            </div>
            <div className="form-actions">
              <button className="btn-primary" type="submit">Save annotation</button>
            </div>
          </form>

          <h3>Saved annotations</h3>
          <table className="review-table">
            <thead>
              <tr><th>Column</th><th>Business name</th><th>Synonyms</th><th>Description</th><th>Status</th><th /></tr>
            </thead>
            <tbody>
              {glossaryItems.length === 0 && (
                <tr><td colSpan={6} className="muted">No column annotations yet.</td></tr>
              )}
              {glossaryItems.map((item) => (
                <tr key={item.id}>
                  <td><code>{item.table_name}.{item.column_name}</code></td>
                  <td>{item.business_name}</td>
                  <td>{splitList(item.synonyms).join(", ")}</td>
                  <td className="knowledge-sql">{item.description}</td>
                  <td>{item.enabled ? "enabled" : "disabled"}</td>
                  <td>
                    <button className="btn-danger" type="button" onClick={() => removeGlossary(item.id)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {tab === "presets" && (
        <section className="review-table-wrap">
          <form className="knowledge-form" onSubmit={submitPreset}>
            <h3>Add a preset question</h3>
            <p className="muted">
              One-click shortcuts shown above the chat input when a conversation is empty.
            </p>
            <div className="knowledge-grid">
              <label className="knowledge-field knowledge-wide">
                Question
                <input
                  value={preset.question}
                  onChange={(e) => setPreset({ ...preset, question: e.target.value })}
                  placeholder="最近 7 天请求量趋势"
                  required
                />
              </label>
              <label className="knowledge-field">
                Sort order
                <input
                  type="number"
                  value={preset.sort_order}
                  onChange={(e) => setPreset({ ...preset, sort_order: Number(e.target.value) })}
                />
              </label>
              <label className="knowledge-check">
                <input
                  type="checkbox"
                  checked={preset.enabled}
                  onChange={(e) => setPreset({ ...preset, enabled: e.target.checked })}
                />
                Enabled
              </label>
            </div>
            <div className="form-actions">
              <button className="btn-primary" type="submit">Save preset</button>
            </div>
          </form>

          <h3>Saved presets</h3>
          <table className="review-table">
            <thead>
              <tr><th>Question</th><th>Order</th><th>Status</th><th /></tr>
            </thead>
            <tbody>
              {presetItems.length === 0 && (
                <tr><td colSpan={4} className="muted">No preset questions yet.</td></tr>
              )}
              {presetItems.map((item) => (
                <tr key={item.id}>
                  <td>{item.question}</td>
                  <td>{item.sort_order}</td>
                  <td>{item.enabled ? "enabled" : "disabled"}</td>
                  <td>
                    <button className="btn-danger" type="button" onClick={() => removePreset(item.id)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </main>
  );
}
