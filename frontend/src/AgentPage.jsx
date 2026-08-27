import AgentChat from "./AgentChat";

export default function AgentPage() {
  return (
    <main className="admin-shell">
      <header className="admin-hero">
        <div>
          <p className="settings-kicker">DATA AGENT / NL→SQL</p>
          <h1>Ask questions about your audit log.</h1>
          <p>Natural language to read-only SQL with a conversation loop. Dangerous statements are blocked and every SQL execution needs your explicit approval.</p>
        </div>
        <div className="agent-nav-links">
          <a className="agent-nav-link" href="/agent/knowledge">Knowledge →</a>
          <a className="agent-nav-link" href="/agent/review">Usage review →</a>
        </div>
      </header>

      <section className="agent-chat-page">
        <AgentChat variant="page" />
      </section>
    </main>
  );
}
