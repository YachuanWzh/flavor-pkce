import { useState } from "react";
import { useSearchParams } from "react-router-dom";

const AUTH_SERVER = "http://localhost:8091";

export default function ConsentPage() {
  const [searchParams] = useSearchParams();
  const [error, setError] = useState("");

  const clientName = searchParams.get("client_name") || "Unknown App";
  const scope = searchParams.get("scope") || "No scopes requested";
  const username = searchParams.get("username") || "";

  const handleApprove = async () => {
    try {
      const resp = await fetch(`${AUTH_SERVER}/consent`, {
        method: "POST",
        credentials: "include",
        redirect: "follow",
      });

      if (resp.redirected) {
        window.location.href = resp.url;
      } else {
        setError("Authorization failed");
      }
    } catch {
      setError("Network error");
    }
  };

  return (
    <div className="consent-wrapper">
      <h1>Authorization Request</h1>
      <div className="consent-card">
        <p>
          <strong>{clientName}</strong> is requesting access to your account.
        </p>
        <div className="consent-field">
          <div className="consent-label">Signed in as</div>
          <div className="consent-value">{username}</div>
        </div>
        <div className="consent-field">
          <div className="consent-label">Scopes</div>
          <div className="consent-value">{scope}</div>
        </div>
      </div>
      <div className="consent-actions">
        <button className="btn-approve" onClick={handleApprove}>
          Approve
        </button>
        <button className="btn-deny" onClick={() => window.history.back()}>
          Deny
        </button>
      </div>
      {error && <div className="auth-message error">{error}</div>}
    </div>
  );
}
