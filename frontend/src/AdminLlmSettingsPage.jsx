import { useEffect, useMemo, useState } from "react";
import LlmConfigForm from "./LlmConfigForm";
import { EMPTY_LLM_FORM, llmConfigToForm, llmFormToPayload } from "./llm-form";

export default function AdminLlmSettingsPage() {
  const [users, setUsers] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [form, setForm] = useState({ ...EMPTY_LLM_FORM });
  const [keyConfigured, setKeyConfigured] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const selected = useMemo(() => users.find((user) => user.id === selectedId), [users, selectedId]);

  const selectUser = (user) => {
    setSelectedId(user.id);
    setForm(llmConfigToForm(user.llm_config));
    setKeyConfigured(Boolean(user.llm_config?.api_key_configured));
    setError("");
    setSuccess("");
  };

  useEffect(() => {
    const load = async () => {
      const meResponse = await fetch("/api/me", { credentials: "include" });
      if (meResponse.status === 401) {
        window.location.href = "/login";
        return;
      }
      if (!meResponse.ok) throw new Error("Unable to load the signed-in user");
      const me = await meResponse.json();
      if (me.role !== "admin") {
        window.location.href = "/settings/llm";
        return;
      }
      const response = await fetch("/api/admin/users", { credentials: "include" });
      if (!response.ok) throw new Error("Unable to load users");
      const loaded = (await response.json()).users;
      setUsers(loaded);
      if (loaded.length) selectUser(loaded[0]);
    };
    load().catch((cause) => setError(cause.message)).finally(() => setLoading(false));
  }, []);

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const save = async (event) => {
    event.preventDefault();
    if (!selected) return;
    setError("");
    setSuccess("");
    setSaving(true);
    try {
      const response = await fetch(`/api/admin/users/${selected.id}/llm-config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(llmFormToPayload(form)),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Configuration validation failed");
      setUsers((current) => current.map((user) => user.id === selected.id ? { ...user, llm_config: data } : user));
      setKeyConfigured(data.api_key_configured);
      update("upstream_api_key", "");
      setSuccess(`${selected.username}'s route is now version ${data.config_version}. Ask them to run /login again.`);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="settings-shell"><div className="settings-card">Loading administrator console...</div></div>;

  return <main className="admin-shell">
    <header className="admin-hero">
      <div>
        <p className="settings-kicker">FLEET ROUTING / ADMIN</p>
        <h1>Route every user from one console.</h1>
        <p>Select an account, set its upstream route, then have that user run <code>/login</code> to activate the new version.</p>
      </div>
      <a className="admin-link" href="/settings/llm">My route →</a>
    </header>

    <section className="admin-workbench">
      <aside className="user-rail" aria-label="Users">
        <div className="rail-heading"><span>Accounts</span><strong>{users.length}</strong></div>
        {users.map((user) => <button
          type="button"
          key={user.id}
          className={`user-route${user.id === selectedId ? " active" : ""}`}
          onClick={() => selectUser(user)}
        >
          <span className={`route-state${user.llm_config ? " configured" : ""}`} aria-hidden="true" />
          <span><strong>{user.username}</strong><small>{user.llm_config ? `v${user.llm_config.config_version} · ${user.llm_config.service_name}` : "Route not configured"}</small></span>
          {user.role === "admin" && <em>ADMIN</em>}
        </button>)}
      </aside>

      <article className="admin-editor">
        {selected ? <>
          <header className="editor-heading">
            <div><p className="settings-kicker">USER / {selected.username}</p><h2>{selected.llm_config ? "Edit assigned route" : "Assign a first route"}</h2></div>
            <div className="route-version"><span>CONFIG</span><strong>v{selected.llm_config?.config_version ?? "new"}</strong></div>
          </header>
          <LlmConfigForm form={form} onChange={update} onSubmit={save} keyConfigured={keyConfigured} saving={saving} error={error} success={success} submitLabel={`Save ${selected.username}'s route`} />
        </> : <div className="empty-route"><strong>No users found</strong><span>Register a user before assigning a route.</span></div>}
      </article>
    </section>
  </main>;
}
