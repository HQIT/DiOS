import { t as translateUI } from "../../../lib/i18n";
import McpServersPage from "./McpServersPage";
import PluginsPage from "./PluginsPage";
import SkillsPage from "./SkillsPage";
import PackageCatalogPage from "./PackageCatalogPage";

type RegistryKind = "mcp" | "skills" | "plugins" | "packages";

const REGISTRIES: Array<{ key: RegistryKind; label: string; standard: string }> = [
  { key: "packages", label: "受控能力包", standard: "版本与准入" },
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
          <span className="registry-eyebrow">{translateUI("DiOS Capability Registry")}</span>
          <h2>{translateUI("能力注册表")}</h2>
          <p>{translateUI("统一发现和注册扩展能力，再由 Agent 或任务显式授权使用。")}</p>
        </div>
        <div className="registry-lifecycle" aria-label="Registry lifecycle">
          {['发现', '校验', '注册', '授权', '审计'].map((step, index) => (
            <span key={step}>
              {index > 0 && <i aria-hidden="true">→</i>}
              <b>{translateUI(step)}</b>
            </span>
          ))}
        </div>
      </section>

      <nav className="registry-tabs" aria-label={translateUI("能力注册表类型")}>
        {REGISTRIES.map((item) => (
          <button
            key={item.key}
            className={kind === item.key ? "active" : ""}
            onClick={() => { window.location.hash = `console/registry/${item.key}`; }}
          >
            <span>{translateUI(item.label)}</span>
            <small>{translateUI(item.standard)}</small>
          </button>
        ))}
      </nav>

      <div className="registry-content">
        {kind === "packages" && <PackageCatalogPage selected={sub.split("/").filter(Boolean)[1] || ""} />}
        {kind === "mcp" && <McpServersPage />}
        {kind === "skills" && <SkillsPage />}
        {kind === "plugins" && <PluginsPage />}
      </div>
    </div>
  );
}
