import McpServersPage from "./McpServersPage";
import PluginsPage from "./PluginsPage";
import SkillsPage from "./SkillsPage";

type RegistryKind = "mcp" | "skills" | "plugins";

const REGISTRIES: Array<{ key: RegistryKind; label: string; standard: string }> = [
  { key: "mcp", label: "MCP Servers", standard: "MCP Registry" },
  { key: "skills", label: "Skills", standard: "Agent Skills" },
  { key: "plugins", label: "Plugins", standard: "Open Plugins" },
];

function parseKind(sub: string): RegistryKind {
  const kind = sub.split("/").filter(Boolean)[0];
  return REGISTRIES.some((item) => item.key === kind) ? kind as RegistryKind : "mcp";
}

export default function RegistryPage({ sub }: { sub: string }) {
  const kind = parseKind(sub);

  return (
    <div className="panel registry-shell">
      <section className="registry-hero">
        <div>
          <span className="registry-eyebrow">DiOS Capability Registry</span>
          <h2>能力注册表</h2>
          <p>统一发现和注册扩展能力，再由 Agent 或任务显式授权使用。</p>
        </div>
        <div className="registry-lifecycle" aria-label="Registry lifecycle">
          {['发现', '校验', '注册', '授权', '审计'].map((step, index) => (
            <span key={step}>
              {index > 0 && <i aria-hidden="true">→</i>}
              <b>{step}</b>
            </span>
          ))}
        </div>
      </section>

      <nav className="registry-tabs" aria-label="能力注册表类型">
        {REGISTRIES.map((item) => (
          <button
            key={item.key}
            className={kind === item.key ? "active" : ""}
            onClick={() => { window.location.hash = `console/registry/${item.key}`; }}
          >
            <span>{item.label}</span>
            <small>{item.standard}</small>
          </button>
        ))}
      </nav>

      <div className="registry-content">
        {kind === "mcp" && <McpServersPage />}
        {kind === "skills" && <SkillsPage />}
        {kind === "plugins" && <PluginsPage />}
      </div>
    </div>
  );
}
