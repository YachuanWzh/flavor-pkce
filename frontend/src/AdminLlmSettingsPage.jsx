import { useEffect, useMemo, useState } from "react";
import LlmConfigForm from "./LlmConfigForm";
import ProfilesToolbar from "./ProfilesToolbar";
import { EMPTY_LLM_FORM, llmConfigToForm, llmFormToPayload } from "./llm-form";

export default function AdminLlmSettingsPage() {
  const [users, setUsers] = useState([]);
  const [me, setMe] = useState(null);
  const [selectedId, setSelectedId] = useState("");
  const [form, setForm] = useState({ ...EMPTY_LLM_FORM });
  const [keyConfigured, setKeyConfigured] = useState(false);
  const [profiles, setProfiles] = useState([]);
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [fallbackProfileId, setFallbackProfileId] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const selected = useMemo(() => users.find((user) => user.id === selectedId), [users, selectedId]);

  const loadProfiles = async (userId) => {
    const response = await fetch(
      `/api/admin/users/${userId}/llm-config-profiles`,
      { credentials: "include" },
    );
    if (!response.ok) throw new Error("Unable to load the user's profiles");
    const data = await response.json();
    setProfiles(data.profiles);
    return data.profiles;
  };

  const changeRole = async (user, role) => {
    setError("");
    setSuccess("");
    try {
      const response = await fetch(`/api/admin/users/${user.id}/role`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ role }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Role change failed");
      setUsers((current) => current.map((item) => item.id === user.id ? { ...item, role } : item));
      setSuccess(`${user.username} is now ${role === "admin" ? "an administrator" : "a regular user"}.`);
    } catch (cause) {
      setError(cause.message);
    }
  };

  const selectUser = async (user) => {
    setSelectedId(user.id);
    setForm(llmConfigToForm(user.llm_config));
    setKeyConfigured(Boolean(user.llm_config?.api_key_configured));
    setFallbackProfileId(user.llm_config?.fallback_profile_id || "");
    setSelectedProfileId(user.llm_config?.active_profile_id || "");
    setError("");
    setSuccess("");
    try {
      await loadProfiles(user.id);
    } catch (cause) {
      setError(cause.message);
    }
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
      setMe(me);
      const response = await fetch("/api/admin/users", { credentials: "include" });
      if (!response.ok) throw new Error("Unable to load users");
      const loaded = (await response.json()).users;
      setUsers(loaded);
      if (loaded.length) await selectUser(loaded[0]);
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
    if (!selected || !profile) return;
    setError("");
    setSuccess("");
    setSaving(true);
    try {
      const response = await fetch(
        `/api/admin/users/${selected.id}/llm-config-profiles/${profile.id}/activate`,
        { method: "POST", credentials: "include" },
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Activation failed");
      setForm(llmConfigToForm(data));
      setKeyConfigured(data.api_key_configured);
      setUsers((current) => current.map((user) => user.id === selected.id ? { ...user, llm_config: data } : user));
      setSuccess(`${selected.username}'s active route is now "${profile.name}" (v${data.config_version}). Ask them to run /login again.`);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setSaving(false);
    }
  };

  const saveAsProfile = async (name) => {
    if (!selected) return;
    setError("");
    setSuccess("");
    setSaving(true);
    try {
      const payload = { ...llmFormToPayload(form), name };
      const current = profiles.find((item) => item.id === selectedProfileId);
      const isUpdate = Boolean(current && current.name === name);
      const url = isUpdate
        ? `/api/admin/users/${selected.id}/llm-config-profiles/${current.id}`
        : `/api/admin/users/${selected.id}/llm-config-profiles`;
      const response = await fetch(url, {
        method: isUpdate ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Saving the profile failed");
      const refreshed = await loadProfiles(selected.id);
      if (refreshed.some((item) => item.id === data.id)) setSelectedProfileId(data.id);
      setSuccess(`Profile "${data.name}" saved for ${selected.username}.`);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setSaving(false);
    }
  };

  const deleteProfile = async () => {
    const profile = profiles.find((item) => item.id === selectedProfileId);
    if (!selected || !profile) return;
    setError("");
    setSuccess("");
    setSaving(true);
    try {
      const response = await fetch(
        `/api/admin/users/${selected.id}/llm-config-profiles/${profile.id}`,
        { method: "DELETE", credentials: "include" },
      );
      if (!response.ok) throw new Error("Deleting the profile failed");
      await loadProfiles(selected.id);
      setSelectedProfileId("");
      if (fallbackProfileId === profile.id) setFallbackProfileId("");
      setSuccess(`Profile "${profile.name}" deleted.`);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setSaving(false);
    }
  };

  const setFallback = async (profileId) => {
    if (!selected) return;
    const previous = fallbackProfileId;
    setFallbackProfileId(profileId);
    setError("");
    setSuccess("");
    try {
      const response = await fetch(`/api/admin/users/${selected.id}/llm-config/fallback`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ fallback_profile_id: profileId || null }),
      });
      if (!response.ok) throw new Error("Updating the failover route failed");
      setUsers((current) => current.map((user) => user.id === selected.id
        ? { ...user, llm_config: { ...(user.llm_config || {}), fallback_profile_id: profileId || null } }
        : user));
      setSuccess(profileId
        ? `Failover route armed for ${selected.username}.`
        : `Failover route cleared for ${selected.username}.`);
    } catch (cause) {
      setFallbackProfileId(previous);
      setError(cause.message);
    }
  };

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
      // Admin responses now include the decrypted key; keep it visible so
      // the credential can be inspected or rotated without re-entering it.
      update("upstream_api_key", data.upstream_api_key || "");
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
        <p>Select an account, manage its profiles and upstream route, then have that user run <code>/login</code> to activate the new version.</p>
      </div>
      <a className="admin-link" href="/settings/llm">My route →</a>
    </header>

    <section className="admin-workbench">
      <aside className="user-rail" aria-label="Users">
        <div className="rail-heading"><span>Accounts</span><strong>{users.length}</strong></div>
        {users.map((user) => <div key={user.id} className={`user-route${user.id === selectedId ? " active" : ""}`}>
          <button type="button" className="user-route-main" onClick={() => selectUser(user)}>
            <span className={`route-state${user.llm_config ? " configured" : ""}`} aria-hidden="true" />
            <span><strong>{user.username}</strong><small>{user.llm_config ? `v${user.llm_config.config_version} · ${user.llm_config.service_name}` : "Route not configured"}</small></span>
            {user.role === "admin" && <em>ADMIN</em>}
          </button>
          {me && user.id !== me.id && (
            <button
              type="button"
              className="role-toggle"
              title={user.role === "admin" ? "Demote to regular user" : "Promote to administrator"}
              onClick={(event) => {
                event.stopPropagation();
                changeRole(user, user.role === "admin" ? "user" : "admin");
              }}
            >
              {user.role === "admin" ? "Demote" : "Promote"}
            </button>
          )}
        </div>)}
      </aside>

      <article className="admin-editor">
        {selected ? <>
          <header className="editor-heading">
            <div><p className="settings-kicker">USER / {selected.username}</p><h2>{selected.llm_config ? "Edit assigned route" : "Assign a first route"}</h2></div>
            <div className="route-version"><span>CONFIG</span><strong>v{selected.llm_config?.config_version ?? "new"}</strong></div>
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
          <LlmConfigForm form={form} onChange={update} onSubmit={save} keyConfigured={keyConfigured} saving={saving} error={error} success={success} submitLabel={`Save ${selected.username}'s route`} />
        </> : <div className="empty-route"><strong>No users found</strong><span>Register a user before assigning a route.</span></div>}
      </article>
    </section>
  </main>;
}
