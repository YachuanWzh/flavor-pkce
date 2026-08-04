import { BrowserRouter, Routes, Route } from "react-router-dom";
import LoginPage from "./LoginPage";
import RegisterPage from "./RegisterPage";
import ConsentPage from "./ConsentPage";
import LlmSettingsPage from "./LlmSettingsPage";
import AdminLlmSettingsPage from "./AdminLlmSettingsPage";
import "./App.css";

function Home() {
  return (
    <div className="home-wrapper">
      <h1>PKCE Authorization Server</h1>
      <p>The authorization server is running.</p>
      <p>
        Endpoints: <code>/authorize</code>, <code>/token</code>,{" "}
        <code>/login</code>, <code>/register</code>
      </p>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/consent" element={<ConsentPage />} />
        <Route path="/settings/llm" element={<LlmSettingsPage />} />
        <Route path="/admin/llm-configs" element={<AdminLlmSettingsPage />} />
      </Routes>
    </BrowserRouter>
  );
}
