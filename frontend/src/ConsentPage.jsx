import { useState } from "react";
import { useSearchParams } from "react-router-dom";

const AUTH_SERVER = "http://localhost:8000";

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
    <div className="container">
      <h1>Authorization Request</h1>
      <div className="card">
        <p>
          <strong>{clientName}</strong> is requesting access to your account.
        </p>
        <div className="field">
          <div className="label">Signed in as</div>
          <div className="value">{username}</div>
        </div>
        <div className="field">
          <div className="label">Scopes</div>
          <div className="value">{scope}</div>
        </div>
      </div>
      <div className="actions">
        <button className="btn-approve" onClick={handleApprove}>
          Approve
        </button>
        <button className="btn-deny" onClick={() => window.history.back()}>
          Deny
        </button>
      </div>
      {error && <div className="error">{error}</div>}
    </div>
  );
}
