import { useState } from "react";

export default function ProfilesToolbar({
  profiles,
  selectedProfileId,
  fallbackProfileId,
  saving,
  onSelect,
  onActivate,
  onDelete,
  onSaveAs,
  onSetFallback,
}) {
  const [newProfileName, setNewProfileName] = useState("");
  const selectedProfile = profiles.find((profile) => profile.id === selectedProfileId) || null;

  const saveAs = () => {
    const name = newProfileName.trim();
    if (name) onSaveAs(name);
  };

  return <>
    <div className="profile-toolbar">
      <label className="profile-picker">
        <span>Saved profiles</span>
        <select value={selectedProfileId} onChange={(event) => onSelect(event.target.value)}>
          <option value="">— select a saved profile —</option>
          {profiles.map((profile) => (
            <option key={profile.id} value={profile.id}>{profile.name}</option>
          ))}
        </select>
      </label>
      <div className="profile-actions">
        <button type="button" className="btn-secondary" disabled={!selectedProfile || saving} onClick={onActivate}>Activate</button>
        <button type="button" className="btn-danger" disabled={!selectedProfile || saving} onClick={onDelete}>Delete</button>
      </div>
      <div className="profile-save-as">
        <input
          value={newProfileName}
          onChange={(event) => setNewProfileName(event.target.value)}
          placeholder="New profile name"
        />
        <button type="button" className="btn-secondary" disabled={saving} onClick={saveAs}>
          {selectedProfile && newProfileName.trim() === selectedProfile.name ? "Update profile" : "Save as profile"}
        </button>
      </div>
      <small className="profile-hint">Save once, switch later: pick a profile from the dropdown, then Activate.</small>
    </div>

    <div className="fallback-picker">
      <label>
        <span>Failover route (gateway smart routing)</span>
        <select value={fallbackProfileId || ""} onChange={(event) => onSetFallback(event.target.value)}>
          <option value="">— no failover (single route) —</option>
          {profiles.map((profile) => (
            <option key={profile.id} value={profile.id}>{profile.name}</option>
          ))}
        </select>
      </label>
      <small>When the primary upstream is unreachable, the gateway retries this route and tells Flavor Code via X-Gateway-Route / route_switched.</small>
    </div>
  </>;
}
