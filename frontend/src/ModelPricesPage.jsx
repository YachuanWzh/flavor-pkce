import { useCallback, useEffect, useState } from "react";

/* Admin CRUD for per-model USD prices (per 1M tokens) used by the
 * dashboard cost reports and daily cost budgets. DB prices override the
 * MODEL_PRICES_JSON env config per model and apply without a restart. */

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

const EMPTY = { model: "", prompt: "", completion: "", cache_read: "", cache_creation: "" };
const FIELDS = [
  { key: "prompt", label: "Prompt" },
  { key: "completion", label: "Completion" },
  { key: "cache_read", label: "Cache read" },
  { key: "cache_creation", label: "Cache create" },
];

function fmtPrice(n) {
  if (n == null) return "—";
  return `$${Number(n).toFixed(n >= 1 ? 2 : 4)}`;
}

export default function ModelPricesPage() {
  const [items, setItems] = useState([]);
  const [catalog, setCatalog] = useState([]);
  const [form, setForm] = useState(EMPTY);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const [prices, cat] = await Promise.all([
        api("/gw/api/prices"),
        api("/gw/api/prices/catalog"),
      ]);
      setItems(prices.items || []);
      setCatalog(cat.items || []);
    } catch (cause) {
      setError(cause.message);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const flash = (message) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 4000);
  };

  const save = async (entry) => {
    try {
      await api("/gw/api/prices", { method: "POST", body: JSON.stringify(entry) });
      flash(`Price saved for ${entry.model}.`);
      setForm(EMPTY);
      await load();
    } catch (cause) {
      setError(cause.message);
    }
  };

  const submit = (event) => {
    event.preventDefault();
    setError("");
    const entry = { model: form.model.trim() };
    for (const { key } of FIELDS) entry[key] = Number(form[key] || 0);
    save(entry);
  };

  const addFromCatalog = (cat) => save({ ...cat, configured: undefined });

  const remove = async (model) => {
    try {
      await api(`/gw/api/prices/${encodeURIComponent(model)}`, { method: "DELETE" });
      flash(`Price deleted for ${model}.`);
      await load();
    } catch (cause) {
      setError(cause.message);
    }
  };

  return (
    <main className="admin-shell">
      <header className="admin-hero">
        <div>
          <p className="settings-kicker">OBSERVABILITY / MODEL PRICES</p>
          <h1>Price the models you run.</h1>
          <p>
            USD per 1M tokens for the estimated-cost card, the cost charts and the
            daily cost budget. Changes apply immediately — no restart. Models
            without a price count as $0.
          </p>
        </div>
        <a className="agent-nav-link" href="/dashboard">← Dashboard</a>
      </header>

      {error && <div className="form-error">{error}</div>}
      {notice && <div className="form-notice">{notice}</div>}

      <section className="review-table-wrap">
        <form className="knowledge-form" onSubmit={submit}>
          <h3>Add or update a price</h3>
          <div className="knowledge-grid">
            <label className="knowledge-field knowledge-wide">
              Model name (exactly as it appears in the audit log)
              <input
                value={form.model}
                onChange={(e) => setForm({ ...form, model: e.target.value })}
                placeholder="qwen3.8-flash"
                required
              />
            </label>
            {FIELDS.map(({ key, label }) => (
              <label key={key} className="knowledge-field">
                {label} ($/1M tokens)
                <input
                  type="number"
                  min="0"
                  step="0.0001"
                  value={form[key]}
                  onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                  placeholder="0"
                />
              </label>
            ))}
          </div>
          <div className="form-actions">
            <button className="btn-primary" type="submit">Save price</button>
          </div>
        </form>

        <h3>Configured prices</h3>
        <table className="review-table">
          <thead>
            <tr>
              <th>Model</th>
              {FIELDS.map(({ key, label }) => <th key={key}>{label}</th>)}
              <th />
            </tr>
          </thead>
          <tbody>
            {items.length === 0 && (
              <tr><td colSpan={6} className="muted">No prices configured — costs show as $0.</td></tr>
            )}
            {items.map((item) => (
              <tr key={item.model}>
                <td><code>{item.model}</code></td>
                {FIELDS.map(({ key }) => <td key={key}>{fmtPrice(item[key])}</td>)}
                <td>
                  <div className="chat-confirm-actions">
                    <button className="btn-mini" type="button" onClick={() => setForm({
                      model: item.model,
                      prompt: String(item.prompt), completion: String(item.completion),
                      cache_read: String(item.cache_read), cache_creation: String(item.cache_creation),
                    })}>Edit</button>
                    <button className="btn-danger" type="button" onClick={() => remove(item.model)}>Delete</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <h3>Common models (public list prices — verify before trusting)</h3>
        <table className="review-table">
          <thead>
            <tr>
              <th>Model</th>
              {FIELDS.map(({ key, label }) => <th key={key}>{label}</th>)}
              <th />
            </tr>
          </thead>
          <tbody>
            {catalog.map((cat) => (
              <tr key={cat.model}>
                <td><code>{cat.model}</code></td>
                {FIELDS.map(({ key }) => <td key={key}>{fmtPrice(cat[key])}</td>)}
                <td>
                  <button
                    className="btn-mini"
                    type="button"
                    disabled={cat.configured}
                    onClick={() => addFromCatalog(cat)}
                  >
                    {cat.configured ? "Configured" : "Add"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}
