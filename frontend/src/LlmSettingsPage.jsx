import { useEffect, useState } from "react";
import LlmConfigForm from "./LlmConfigForm";
import ProfilesToolbar from "./ProfilesToolbar";
import { EMPTY_LLM_FORM, llmConfigToForm, llmFormToPayload } from "./llm-form";

export default function LlmSettingsPage() {
  const [form, setForm] = useState({ ...EMPTY_LLM_FORM });
  const [user, setUser] = useState(null);
  const [keyConfigured, setKeyConfigured] = useState(false);
  const [version, setVersion] = useState(null);
  const [profiles, setProfiles] = useState([]);
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [fallbackProfileId, setFallbackProfileId] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const loadProfiles = async () => {
    const response = await fetch("/api/me/llm-config-profiles", { credentials: "include" });
    if (!response.ok) throw new Error("Unable to load saved profiles");
    const data = await response.json();
    setProfiles(data.profiles);
    return data.profiles;
  };

  useEffect(() => {
    const load = async () => {
      const me = await fetch("/api/me", { credentials: "include" });
      if (me.status === 401) {
        window.location.href = "/login";
        return;
      }
      if (!me.ok) throw new Error("Unable to load the signed-in user");
      setUser(await me.json());
      const [configResponse, loadedProfiles] = await Promise.all([
        fetch("/api/me/llm-config", { credentials: "include" }),
        loadProfiles(),
      ]);
      if (configResponse.status === 404) return;
      if (!configResponse.ok) throw new Error("Unable to load LLM configuration");
      const data = await configResponse.json();
      setForm(llmConfigToForm(data));
      setKeyConfigured(data.api_key_configured);
      setVersion(data.config_version);
      setFallbackProfileId(data.fallback_profile_id || "");
      if (data.active_profile_id && loadedProfiles.some((p) => p.id === data.active_profile_id)) {
        setSelectedProfileId(data.active_profile_id);
      }
    };
    load().catch((cause) => setError(cause.message)).finally(() => setLoading(false));
  }, []);

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));

  const selectProfile = (profileId) => {
    setSelectedProfileId(profileId);
    setError("");
    setSuccess("");
    const profile = profiles.find((item) => item.id === profileId);
    if (!profile) return;
    setForm(llmConfigToForm(profile));
    setKeyConfigured(profile.api_key_configured);
  };

  const activateProfile = async () => {
    const profile = profiles.find((item) => item.id === selectedProfileId);
    if (!profile) return;
    setError("");
    setSuccess("");
    setSaving(true);
    try {
      const response = await fetch(
        `/api/me/llm-config-profiles/${profile.id}/activate`,
        { method: "POST", credentials: "include" },
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Activation failed");
      setForm(llmConfigToForm(data));
      setKeyConfigured(data.api_key_configured);
      setVersion(data.config_version);
      setSuccess("Activated \"" + profile.name + "\" (v" + data.config_version + "). Run /login in Flavor Code to apply.");
    } catch (cause) {
      setError(cause.message);
    } finally {
      setSaving(false);
    }
  };

  const saveAsProfile = async (name) => {
    setError("");
    setSuccess("");
    setSaving(true);
    try {
      const payload = { ...llmFormToPayload(form), name };
      const current = profiles.find((item) => item.id === selectedProfileId);
      const isUpdate = Boolean(current && current.name === name);
      const url = isUpdate
        ? `/api/me/llm-config-profiles/${current.id}`
        : "/api/me/llm-config-profiles";
      const response = await fetch(url, {
        method: isUpdate ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Saving the profile failed");
      const refreshed = await loadProfiles();
      if (refreshed.some((item) => item.id === data.id)) setSelectedProfileId(data.id);
      setSuccess("Profile \"" + data.name + "\" saved. Select it from the dropdown any time.");
    } catch (cause) {
      setError(cause.message);
    } finally {
      setSaving(false);
    }
  };

  const deleteProfile = async () => {
    const profile = profiles.find((item) => item.id === selectedProfileId);
    if (!profile) return;
    setError("");
    setSuccess("");
    setSaving(true);
    try {
      const response = await fetch(
        `/api/me/llm-config-profiles/${profile.id}`,
        { method: "DELETE", credentials: "include" },
      );
      if (!response.ok) throw new Error("Deleting the profile failed");
      await loadProfiles();
      setSelectedProfileId("");
      if (fallbackProfileId === profile.id) setFallbackProfileId("");
      setSuccess("Profile \"" + profile.name + "\" deleted.");
    } catch (cause) {
      setError(cause.message);
    } finally {
      setSaving(false);
    }
  };

  const setFallback = async (profileId) => {
    const previous = fallbackProfileId;
    setFallbackProfileId(profileId);
    setError("");
    setSuccess("");
    try {
      const response = await fetch("/api/me/llm-config/fallback", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ fallback_profile_id: profileId || null }),
      });
      if (!response.ok) throw new Error("Updating the failover route failed");
      setSuccess(profileId
        ? "Failover route armed. The gateway switches to it automatically when the primary fails."
        : "Failover route cleared.");
    } catch (cause) {
      setFallbackProfileId(previous);
      setError(cause.message);
    }
  };

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
      // Owner responses carry the decrypted key back; keep it displayed so
      // switching profiles later repopulates the field correctly.
      update("upstream_api_key", data.upstream_api_key || "");
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

      <ProfilesToolbar
        profiles={profiles}
        selectedProfileId={selectedProfileId}
        fallbackProfileId={fallbackProfileId}
        saving={saving}
        onSelect={selectProfile}
        onActivate={activateProfile}
        onDelete={deleteProfile}
        onSaveAs={saveAsProfile}
        onSetFallback={setFallback}
      />

      <LlmConfigForm form={form} onChange={update} onSubmit={save} keyConfigured={keyConfigured} saving={saving} error={error} success={success} />
    </section>
  </main>;
}
