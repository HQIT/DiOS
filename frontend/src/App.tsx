import { useEffect, useState } from "react";
import ConsolePage from "./apps/console/ConsolePage";
import ChatPage from "./apps/chat/ChatPage";

type AppId = "console" | "chat";
const APPS: { id: AppId; label: string }[] = [
  { id: "console", label: "Console" },
  { id: "chat", label: "Chat" },
];

function readHash(): { app: AppId; sub: string } {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [first, ...rest] = raw.split("/");
  const app = APPS.some((a) => a.id === first) ? (first as AppId) : "console";
  return { app, sub: rest.join("/") };
}

export default function App() {
  const [currentApp, setCurrentApp] = useState<AppId>(() => readHash().app);
  const [sub, setSub] = useState(() => readHash().sub);

  useEffect(() => {
    const onHash = () => {
      const h = readHash();
      setCurrentApp(h.app);
      setSub(h.sub);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <div className="app">
      <header className="app-header">
        <h1>DiOS</h1>
        <nav className="header-nav" style={{ borderRight: "1px solid var(--border)", paddingRight: 12, marginRight: 4 }}>
          {APPS.map((a) => (
            <button
              key={a.id}
              className={currentApp === a.id ? "header-tab active" : "header-tab"}
              onClick={() => { window.location.hash = a.id; }}
            >
              {a.label}
            </button>
          ))}
        </nav>
        {currentApp === "console" && <ConsolePage.Nav sub={sub} />}
      </header>

      <div style={{ height: "calc(100vh - 57px)" }}>
        {currentApp === "console" && <ConsolePage.Content sub={sub} />}
        {currentApp === "chat" && <ChatPage />}
      </div>
    </div>
  );
}
