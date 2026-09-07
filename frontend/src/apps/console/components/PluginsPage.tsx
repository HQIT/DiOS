const COMPONENTS = [
  {
    name: "Skills",
    description: "按 Agent Skills 规范提供指令、脚本和资源。",
    status: "可映射",
  },
  {
    name: "MCP Servers",
    description: "复用 MCP 配置与任务级工具授权，不建立第二套工具协议。",
    status: "可映射",
  },
  {
    name: "Agents",
    description: "映射为独立 AgentRuntime，保留自己的身份和生命周期。",
    status: "待接线",
  },
  {
    name: "Hooks / LSP",
    description: "需要独立权限模型和 Runtime Adapter，暂不进入数据面。",
    status: "Phase 3",
  },
];

export default function PluginsPage() {
  return (
    <div className="registry-page">
      <header className="registry-page-header">
        <div>
          <div className="registry-title-line">
            <h3>Plugin Packages</h3>
            <span className="registry-standard-badge preview">兼容预览</span>
          </div>
          <p>Plugin 是组合分发格式；组件仍由 DiOS 按 MCP、Skill、Agent 和 Runtime 分别治理。</p>
        </div>
        <div className="registry-kpis">
          <div><strong>0</strong><span>已注册</span></div>
          <div><strong>Open Plugins</strong><span>兼容目标</span></div>
          <div><strong>Phase 3</strong><span>安装通道</span></div>
        </div>
      </header>

      <section className="registry-block">
        <div className="registry-section-heading">
          <div>
            <h4>Plugin Registry</h4>
            <p>当前没有行业统一的公共 Plugin Registry；DiOS 先固定清单映射和准入界面。</p>
          </div>
          <span className="registry-source-pill">Open Plugins format</span>
        </div>
        <div className="registry-empty-state">
          <span className="registry-empty-icon">P</span>
          <div>
            <strong>尚未配置可信插件源</strong>
            <p>单机 Phase 0 不会因为目录存在就加载并执行插件代码。外部安装将在签名、版本锁定和权限预览完成后开放。</p>
          </div>
          <button className="btn-sm btn-secondary" disabled>安装通道未开放</button>
        </div>
      </section>

      <section className="registry-block">
        <div className="registry-section-heading">
          <div>
            <h4>Component Mapping</h4>
            <p>Plugin 只负责打包；每类能力进入现有控制面后才可启用。</p>
          </div>
        </div>
        <div className="plugin-component-grid">
          {COMPONENTS.map((component) => (
            <article key={component.name} className="plugin-component-card">
              <div>
                <strong>{component.name}</strong>
                <span className={component.status === "可映射" ? "registry-status ready" : "registry-status"}>
                  {component.status}
                </span>
              </div>
              <p>{component.description}</p>
            </article>
          ))}
        </div>
      </section>

      <aside className="registry-policy-note">
        <strong>OS 边界</strong>
        <span>注册不等于授权。Plugin 内的 Skill 不能扩大 MCP 权限，MCP Server 仍需按 Agent 或任务显式分配。</span>
      </aside>
    </div>
  );
}
