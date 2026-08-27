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
      </header>

      <section className="agent-chat-page">
        <AgentChat variant="page" />
      </section>
    </main>
  );
}
