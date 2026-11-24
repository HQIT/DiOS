import { t as translateUI } from "./lib/i18n";
import { useContext, useEffect, useState } from "react";
import { AuthSessionContext } from "./lib/authSession";
import AccessGate from "./components/AccessGate";
import ConsolePage from "./apps/console/ConsolePage";
import ChatPage from "./apps/chat/ChatPage";
import WorkbenchPage from "./apps/workbench/WorkbenchPage";

import PreferencesControls from "./components/PreferencesControls";
import { usePreferences } from "./lib/preferences";

type AppId = "console" | "chat" | "workbench";
const APPS: { id: AppId; label: string }[] = [
  { id: "workbench", label: "工作台原型" },
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
  usePreferences();
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

  // This route contains synthetic, in-memory fixtures only. Console and Chat
  // retain AccessGate; the prototype must never import protected API clients.
  if (currentApp === "workbench") return <WorkbenchPage sub={sub} />;

  return (
    <AccessGate>
      <AppContent currentApp={currentApp} sub={sub} />
    </AccessGate>
  );
}

function AppContent({ currentApp, sub }: { currentApp: AppId; sub: string }) {
  const session = useContext(AuthSessionContext);
  const app = session.principal ? "console" : currentApp;
  const section = session.principal && !(currentApp === "console" && (sub === "tasks" || sub.startsWith("tasks/"))) ? "tasks" : sub;
  return (
    <div className={session.browser ? "app browser-session" : "app"}>
      <header className="app-header">
        <h1>DiOS</h1>
        <nav className="header-nav" style={{ borderRight: "1px solid var(--border)", paddingRight: 12, marginRight: 4 }}>
          {APPS.filter(a => !session.principal || a.id === "console").map((a) => (
            <button
              key={a.id}
              className={app === a.id ? "header-tab active" : "header-tab"}
              onClick={() => { window.location.hash = a.id; }}
            >
              {translateUI(a.label)}
            </button>
          ))}
        </nav>
        {app === "console" && <ConsolePage.Nav sub={section} />}
        <PreferencesControls />
        {session.browser && <span className="session-label" title={session.label}>{translateUI("企业账号")}</span>}
        {session.signOut && <button className="session-exit" type="button" onClick={() => void session.signOut?.()}>{translateUI("退出登录")}</button>}
      </header>

      <div style={{ height: "calc(100vh - 57px)" }}>
        {app === "console" && <ConsolePage.Content sub={section} />}
        {app === "chat" && <ChatPage />}
      </div>
    </div>
  );
}
