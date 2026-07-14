import { BrowserRouter, Routes, Route } from "react-router-dom";
import LoginPage from "./LoginPage";
import ConsentPage from "./ConsentPage";
import "./App.css";

function Home() {
  return (
    <div className="container">
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
        <Route path="/consent" element={<ConsentPage />} />
      </Routes>
    </BrowserRouter>
  );
}
