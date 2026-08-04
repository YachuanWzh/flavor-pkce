export default function LlmConfigForm({
  form,
  onChange,
  onSubmit,
  keyConfigured,
  saving,
  error,
  success,
  submitLabel = "Save route",
}) {
  return <form className="settings-form" onSubmit={onSubmit}>
    <div className="settings-grid">
      <label><span>Provider ID</span><input required pattern="[A-Za-z0-9_-]+" value={form.provider_id} onChange={(event) => onChange("provider_id", event.target.value)} placeholder="deepseek" /></label>
      <label><span>Service name</span><input required value={form.service_name} onChange={(event) => onChange("service_name", event.target.value)} placeholder="Enterprise DeepSeek" /></label>
      <label><span>API protocol</span><select value={form.api_type} onChange={(event) => onChange("api_type", event.target.value)}><option value="anthropic">Anthropic</option><option value="openai">OpenAI</option></select></label>
      <label><span>Authentication</span><select value={form.upstream_auth_type} onChange={(event) => onChange("upstream_auth_type", event.target.value)}><option value="x-api-key">x-api-key</option><option value="bearer">Bearer</option><option value="api-key">api-key</option></select></label>
    </div>

    <label><span>Upstream URL</span><input required type="url" value={form.upstream_url} onChange={(event) => onChange("upstream_url", event.target.value)} placeholder="https://api.example.com/anthropic" /></label>
    <label><span>Upstream API key</span><input required={!keyConfigured} type="password" autoComplete="new-password" value={form.upstream_api_key} onChange={(event) => onChange("upstream_api_key", event.target.value)} placeholder={keyConfigured ? "Configured — leave blank to keep it" : "Enter the upstream key"} /></label>

    <div className="model-lane">
      <label><span>Main model</span><input required value={form.default_model} onChange={(event) => onChange("default_model", event.target.value)} placeholder="deepseek-v4-pro" /></label>
      <div className="lane-arrow" aria-hidden="true">→</div>
      <label><span>Subagent model</span><input required value={form.cheap_model} onChange={(event) => onChange("cheap_model", event.target.value)} placeholder="deepseek-v4-flash" /></label>
    </div>
    <label><span>Allowed models</span><input required value={form.models} onChange={(event) => onChange("models", event.target.value)} placeholder="deepseek-v4-pro, deepseek-v4-flash" /><small>Comma-separated. Main and subagent models must be included.</small></label>
    <label><span>Maximum output tokens</span><input required type="number" min="1" max="1000000" value={form.max_output_tokens} onChange={(event) => onChange("max_output_tokens", event.target.value)} /></label>

    {error && <div className="auth-message error">{error}</div>}
    {success && <div className="auth-message success">{success}</div>}
    <button className={`btn-primary${saving ? " loading" : ""}`} disabled={saving}>{saving ? "Saving..." : submitLabel}</button>
  </form>;
}
