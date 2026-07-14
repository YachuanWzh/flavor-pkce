import { useState } from "react";
import { useSearchParams } from "react-router-dom";

const AUTH_SERVER = "http://localhost:8000";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [searchParams] = useSearchParams();
  const returnUrl = searchParams.get("return_url") || "";

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");

    const formData = new URLSearchParams();
    formData.append("username", username);
    formData.append("password", password);

    try {
      const resp = await fetch(`${AUTH_SERVER}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: formData,
        credentials: "include",
      });

      if (!resp.ok) {
        setError("Invalid username or password");
        return;
      }

      // Redirect back to authorize or home
      if (returnUrl) {
        window.location.href = `${AUTH_SERVER}${returnUrl}`;
      } else {
        window.location.href = `${AUTH_SERVER}/`;
      }
    } catch {
      setError("Network error. Check if the auth server is running.");
    }
  };

  return (
    <div className="container">
      <h1>PKCE Authorization Server</h1>
      <form onSubmit={handleSubmit}>
        <input
          type="text"
          placeholder="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          required
        />
        <input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        <button type="submit">Sign In</button>
        {error && <div className="error">{error}</div>}
      </form>
    </div>
  );
}
