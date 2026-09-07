import { useState, useCallback } from "react";
import AgentList from "./components/AgentList";
import ModelManager from "./components/ModelManager";
import EventLogList from "./components/EventLogList";
import ConnectorsPage from "./components/ConnectorsPage";
import RegistryPage from "./components/RegistryPage";
import TopologyPage from "./components/TopologyPage";

type ConsoleTab = "agents" | "models" | "events" | "connectors" | "registry" | "topology";
const TABS: { key: ConsoleTab; label: string }[] = [
  { key: "agents", label: "Agents" },
  { key: "events", label: "Events" },
  { key: "models", label: "Models" },
  { key: "connectors", label: "Connectors" },
  { key: "registry", label: "Registry" },
  { key: "topology", label: "Topology" },
];

function parseSub(sub: string): { tab: ConsoleTab; rest: string } {
  const parts = sub.split("/").filter(Boolean);
  if (parts[0] === "mcp" || parts[0] === "skills") {
    return { tab: "registry", rest: parts[0] };
  }
  const tab = TABS.some((t) => t.key === parts[0]) ? (parts[0] as ConsoleTab) : "agents";
  return { tab, rest: parts.slice(1).join("/") };
}

function Nav({ sub }: { sub: string }) {
  const { tab } = parseSub(sub);
  return (
    <nav className="header-nav">
      {TABS.map((t) => (
        <button
          key={t.key}
          className={tab === t.key ? "header-tab active" : "header-tab"}
          onClick={() => { window.location.hash = `console/${t.key}`; }}
        >
          {t.label}
        </button>
      ))}
    </nav>
  );
}

function Content({ sub }: { sub: string }) {
  const { tab, rest } = parseSub(sub);
  const [subTab, setSubTab] = useState(rest);
  const navigate = useCallback((s: string) => {
    setSubTab(s);
    window.location.hash = `console/events/${s}`;
  }, []);

  return (
    <div className="main-content" style={{ height: "100%" }}>
      {tab === "agents" && <AgentList />}
      {tab === "events" && (
        <EventLogList
          subTab={subTab === "logs" || subTab === "activity" ? (subTab as "logs" | "activity") : "catalog"}
          onSubTabChange={navigate}
        />
      )}
      {tab === "models" && <ModelManager />}
      {tab === "connectors" && <ConnectorsPage />}
      {tab === "registry" && <RegistryPage sub={rest} />}
      {tab === "topology" && <TopologyPage />}
    </div>
  );
}

const ConsolePage = { Nav, Content };
export default ConsolePage;
