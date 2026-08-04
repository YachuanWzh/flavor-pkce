import { useEffect, useState } from "react";
import LlmConfigForm from "./LlmConfigForm";
import { EMPTY_LLM_FORM, llmConfigToForm, llmFormToPayload } from "./llm-form";

export default function LlmSettingsPage() {
  const [form, setForm] = useState({ ...EMPTY_LLM_FORM });
  const [user, setUser] = useState(null);
  const [keyConfigured, setKeyConfigured] = useState(false);
  const [version, setVersion] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  useEffect(() => {
    const load = async () => {
      const me = await fetch("/api/me", { credentials: "include" });
      if (me.status === 401) {
        window.location.href = "/login";
        return;
      }
      if (!me.ok) throw new Error("Unable to load the signed-in user");
      setUser(await me.json());
      const response = await fetch("/api/me/llm-config", { credentials: "include" });
      if (response.status === 404) return;
      if (!response.ok) throw new Error("Unable to load LLM configuration");
      const data = await response.json();
      setForm(llmConfigToForm(data));
      setKeyConfigured(data.api_key_configured);
      setVersion(data.config_version);
    };
    load().catch((cause) => setError(cause.message)).finally(() => setLoading(false));
  }, []);

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const save = async (event) => {
    event.preventDefault();
    setError("");
    setSuccess("");
    setSaving(true);
    try {
      const response = await fetch("/api/me/llm-config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(llmFormToPayload(form)),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Configuration validation failed");
      setKeyConfigured(data.api_key_configured);
      setVersion(data.config_version);
      update("upstream_api_key", "");
      setSuccess("Saved. Run /login in Flavor Code to activate version " + data.config_version + ".");
    } catch (cause) {
      setError(cause.message);
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="settings-shell"><div className="settings-card">Loading configuration...</div></div>;

  return <main className="settings-shell">
    <section className="settings-card">
      <header className="settings-header">
        <div>
          <div className="settings-title-row">
            <p className="settings-kicker">ROUTE CONTROL / {user?.username || "USER"}</p>
            {user?.role === "admin" && <a className="admin-link" href="/admin/llm-configs">Manage users →</a>}
          </div>
          <h1>Your LLM route</h1>
          <p>Flavor Code receives the service identity and models. The upstream key stays here.</p>
        </div>
        <div className="route-version"><span>CONFIG</span><strong>v{version ?? "new"}</strong></div>
      </header>

      <LlmConfigForm form={form} onChange={update} onSubmit={save} keyConfigured={keyConfigured} saving={saving} error={error} success={success} />
    </section>
  </main>;
}
