import { useState } from "react";
import type { Team } from "./types";
import TeamList from "./components/TeamList";
import AgentList from "./components/AgentList";
import RunPanel from "./components/RunPanel";
import ModelManager from "./components/ModelManager";

type Tab = "agents" | "runs";
type GlobalTab = "teams" | "models";

export default function App() {
  const [globalTab, setGlobalTab] = useState<GlobalTab>("teams");
  const [selectedTeam, setSelectedTeam] = useState<Team | undefined>();
  const [tab, setTab] = useState<Tab>("agents");

  return (
    <div className="app">
      <header className="app-header">
        <h1>NANA-OS</h1>
        <nav className="header-nav">
          <button className={globalTab === "teams" ? "header-tab active" : "header-tab"} onClick={() => setGlobalTab("teams")}>Teams</button>
          <button className={globalTab === "models" ? "header-tab active" : "header-tab"} onClick={() => setGlobalTab("models")}>Models</button>
        </nav>
      </header>

      {globalTab === "models" ? (
        <div className="main-content" style={{ height: "calc(100vh - 57px)" }}>
          <ModelManager />
        </div>
      ) : (
        <div className="layout">
          <aside className="sidebar">
            <TeamList onSelect={setSelectedTeam} selected={selectedTeam} />
          </aside>
          <main className="main-content">
            {selectedTeam ? (
              <>
                <nav className="tabs">
                  <button className={tab === "agents" ? "tab active" : "tab"} onClick={() => setTab("agents")}>Agents</button>
                  <button className={tab === "runs" ? "tab active" : "tab"} onClick={() => setTab("runs")}>Runs</button>
                </nav>
                {tab === "agents" && <AgentList team={selectedTeam} />}
                {tab === "runs" && <RunPanel team={selectedTeam} />}
              </>
            ) : (
              <div className="placeholder">选择一个团队开始</div>
            )}
          </main>
        </div>
      )}
    </div>
  );
}
