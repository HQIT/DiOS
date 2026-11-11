import { useEffect, useReducer, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  currentRun,
  exportSnapshot,
  initialModel,
  PHASE_LABEL,
  reduceModel,
  SCENARIOS,
  selectWorks,
  WORK_LABEL,
} from "./model";
import type { Action, Model, QuerySpec, Role, ScenarioId, Work } from "./model";
import "./workbench.css";

const ROLES: Record<Role, string> = {
  employee: "员工",
  supervisor: "主管",
  admin: "平台管理员",
};
const NAV = [
  ["overview", "◈", "工作总览"],
  ["work", "▤", "工作与成果"],
  ["approvals", "◇", "审批与复核"],
  ["employees", "◎", "数字员工"],
  ["spaces", "▦", "工作空间"],
  ["knowledge", "▧", "知识与记忆"],
  ["studio", "⌘", "Studio"],
  ["operations", "⌁", "运行与治理"],
] as const;
const go = (role: Role, section: string, id = "") => {
  window.location.hash = `workbench/${role}/${section}${id ? `/${id}` : ""}`;
};
function download(name: string, content: string, mime = "application/json") {
  const url = URL.createObjectURL(
    new Blob([content], { type: `${mime};charset=utf-8` }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function Card({
  title,
  children,
  extra,
}: {
  title: string;
  children: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <section className="wb-card">
      <div className="wb-card-head">
        <h2>{title}</h2>
        {extra}
      </div>
      {children}
    </section>
  );
}
function Badge({
  children,
  tone = "muted",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={`wb-badge ${tone}`}>{children}</span>;
}
function Avatar({ scenario }: { scenario: ScenarioId }) {
  const s = SCENARIOS.find((s) => s.id === scenario)!;
  return <span className={`wb-avatar ${s.color}`}>{s.initials}</span>;
}

export default function WorkbenchPage({ sub }: { sub: string }) {
  useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [sub]);
  const [rawRole, rawSection, workId] = sub.split("/");
  const role: Role = Object.hasOwn(ROLES, rawRole)
    ? (rawRole as Role)
    : "employee";
  const section = rawSection || "overview";
  const [model, dispatch] = useReducer(reduceModel, undefined, initialModel);
  const [query, setQuery] = useState<QuerySpec>({
    scenario: "all",
    state: "all",
    search: "",
  });
  const [formOpen, setFormOpen] = useState(false);
  const [newScenario, setNewScenario] = useState<ScenarioId>("sales");
  const [newTitle, setNewTitle] = useState("");
  const works = selectWorks(model, query);
  const snapshot = exportSnapshot(model, query);
  const selected = model.works.find((w) => w.id === workId);
  const pending = model.works.filter(
    (w) => w.state === "awaiting_review" || w.approval === "pending",
  );
  const title = NAV.find((n) => n[0] === section)?.[2] ?? "页面未找到";
  const act = (type: Exclude<Action["type"], "create" | "reset">, id: string) =>
    dispatch({ type, id, role });
  const createWork = (event: FormEvent) => {
    event.preventDefault();
    if (!newTitle.trim()) return;
    dispatch({ type: "create", title: newTitle, scenario: newScenario });
    setFormOpen(false);
    setNewTitle("");
    setQuery({ scenario: "all", state: "all", search: "" });
    go(role, "work");
  };
  const filters = (
    <div className="wb-filters">
      <label className="wb-search">
        <span>⌕</span>
        <input
          aria-label="搜索工作"
          placeholder="搜索工作名称或编号"
          value={query.search}
          onChange={(e) => setQuery({ ...query, search: e.target.value })}
        />
      </label>
      <select
        aria-label="筛选场景"
        value={query.scenario}
        onChange={(e) =>
          setQuery({
            ...query,
            scenario: e.target.value as QuerySpec["scenario"],
          })
        }
      >
        <option value="all">全部场景</option>
        {SCENARIOS.map((s) => (
          <option key={s.id} value={s.id}>
            {s.title}
          </option>
        ))}
      </select>
      <select
        aria-label="筛选工作状态"
        value={query.state}
        onChange={(e) =>
          setQuery({ ...query, state: e.target.value as QuerySpec["state"] })
        }
      >
        <option value="all">全部状态</option>
        {Object.entries(WORK_LABEL).map(([k, v]) => (
          <option key={k} value={k}>
            {v}
          </option>
        ))}
      </select>
      <button
        className="wb-secondary"
        onClick={() =>
          download("dios-demo-work.json", JSON.stringify(snapshot, null, 2))
        }
      >
        导出当前视图
      </button>
    </div>
  );
  const workList = (items: Work[]) =>
    items.length ? (
      <div className="wb-work-list">
        {items.map((w) => (
          <button
            key={w.id}
            className="wb-work-row"
            onClick={() => go(role, "work", w.id)}
          >
            <Avatar scenario={w.scenario} />
            <span className="wb-work-title">
              <strong>{w.title}</strong>
              <small>
                {SCENARIOS.find((s) => s.id === w.scenario)!.employee} · {w.id}
              </small>
            </span>
            <span className="wb-row-status">
              <Badge
                tone={
                  w.state === "completed"
                    ? "green"
                    : w.state === "awaiting_review" || w.approval === "pending"
                      ? "orange"
                      : "blue"
                }
              >
                {WORK_LABEL[w.state]}
              </Badge>
              <small>
                {currentRun(w).stale
                  ? "观测已过期"
                  : PHASE_LABEL[currentRun(w).phase]}
              </small>
            </span>
            <span aria-hidden="true">↗</span>
          </button>
        ))}
      </div>
    ) : (
      <div className="wb-empty">
        <span>◇</span>
        <h3>当前没有匹配的工作</h3>
        <p>调整场景、状态或搜索条件后查看。</p>
        <button
          className="wb-secondary"
          onClick={() =>
            setQuery({ scenario: "all", state: "all", search: "" })
          }
        >
          清除筛选
        </button>
      </div>
    );

  return (
    <div className="workbench">
      <aside className="wb-sidebar">
        <a href="#workbench/employee/overview" className="wb-brand">
          <span className="wb-brand-mark">D</span>
          <span>
            DiOS<small>数字员工服务平台</small>
          </span>
        </a>
        <div className="wb-org">
          <span className="wb-org-icon">N</span>
          <span>
            北辰工作空间<small>合成组织 · 参考场景</small>
          </span>
        </div>
        <span className="wb-nav-label">WORKSPACE</span>
        <nav aria-label="工作台导航">
          {NAV.map(([key, icon, label]) => (
            <a
              key={key}
              className={section === key ? "active" : ""}
              aria-current={section === key ? "page" : undefined}
              href={`#workbench/${role}/${key}`}
            >
              <span aria-hidden="true">{icon}</span>
              {label}
              {key === "approvals" && pending.length > 0 && (
                <i>{pending.length}</i>
              )}
            </a>
          ))}
        </nav>
        <div className="wb-sidebar-bottom">
          <a href="#console/topology">↗ 内核运维 Console</a>
          <p>业务工作与底层运行分层管理</p>
        </div>
      </aside>
      <div className="wb-shell">
        <header className="wb-topbar">
          <span>
            <span className="wb-muted">工作台</span>
            <span className="wb-divider">/</span>
            {title}
          </span>
          <label className="wb-role">
            角色预览
            <select
              aria-label="角色预览"
              value={role}
              onChange={(e) => go(e.target.value as Role, section, workId)}
            >
              {Object.entries(ROLES).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </header>
        <div className="wb-prototype">
          <span>
            <b>交互原型</b> 合成数据 · 操作仅影响本页，刷新重置
          </span>
          <button
            onClick={() => {
              dispatch({ type: "reset" });
              setQuery({ scenario: "all", state: "all", search: "" });
              setFormOpen(false);
              go(role, "overview");
            }}
          >
            重置演示
          </button>
        </div>
        <main className="wb-main">
          <div className="wb-page-heading">
            <div>
              <div className="wb-eyebrow">
                {role === "supervisor"
                  ? "TEAM WORKSPACE"
                  : role === "admin"
                    ? "PLATFORM OVERVIEW"
                    : "MY WORKSPACE"}
              </div>
              <h1>
                {section === "overview"
                  ? role === "supervisor"
                    ? "让团队成果，走向交付"
                    : role === "admin"
                      ? "连接能力，守住运行边界"
                      : "把工作交给可靠的协作伙伴"
                  : title}
              </h1>
              <p>
                {role === "admin"
                  ? "查看能力、运行与治理边界；业务验收由工作负责人完成。"
                  : role === "supervisor"
                    ? "关注团队交付、待复核成果与需要你决定的事项。"
                    : "从一个明确目标开始，跟踪进展，接收可追溯的成果。"}
              </p>
            </div>
            {role !== "admin" && (
              <button className="wb-primary" onClick={() => setFormOpen(true)}>
                ＋ 发起工作
              </button>
            )}
          </div>
          {model.notice && (
            <div className="wb-notice" role="status">
              {model.notice}
            </div>
          )}
          {formOpen && (
            <Card
              title="发起一项工作"
              extra={
                <button
                  className="wb-text-button"
                  onClick={() => setFormOpen(false)}
                >
                  收起
                </button>
              }
            >
              <form className="wb-form" onSubmit={createWork}>
                <label>
                  选择参考场景
                  <select
                    value={newScenario}
                    onChange={(e) =>
                      setNewScenario(e.target.value as ScenarioId)
                    }
                  >
                    {SCENARIOS.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.title} · {s.employee}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  工作目标
                  <input
                    required
                    maxLength={100}
                    value={newTitle}
                    onChange={(e) => setNewTitle(e.target.value)}
                    placeholder={
                      SCENARIOS.find((s) => s.id === newScenario)!.brief
                    }
                  />
                </label>
                <p>{SCENARIOS.find((s) => s.id === newScenario)!.acceptance}</p>
                <button
                  type="submit"
                  className="wb-primary"
                  disabled={!newTitle.trim()}
                >
                  创建演示工作
                </button>
              </form>
            </Card>
          )}

          {(section === "overview" || section === "work") && !workId && (
            <>
              {section === "overview" && (
                <div className="wb-metrics">
                  {[
                    ["当前工作", works.length, "遵循下方筛选条件"],
                    [
                      "需要复核",
                      works.filter((w) => w.state === "awaiting_review").length,
                      "技术完成后进入业务验收",
                    ],
                    ["已验收", snapshot.completed, "当前筛选范围"],
                    ["实际模型并发", "未知", "原型未连接执行端"],
                  ].map(([label, value, note]) => (
                    <div key={label} className="wb-metric">
                      <span>{label}</span>
                      <strong>{value}</strong>
                      <small>{note}</small>
                    </div>
                  ))}
                </div>
              )}
              <div className="wb-content-grid">
                <Card
                  title={role === "supervisor" ? "团队工作" : "工作进展"}
                  extra={<Badge>水位 {snapshot.watermark}</Badge>}
                >
                  {filters}
                  {workList(works)}
                </Card>
                <div className="wb-side-stack">
                  <Card title="待办提醒">
                    <p className="wb-muted">组织演示范围 · 不受工作筛选影响</p>
                    {pending.length ? (
                      pending.map((w) => (
                        <button
                          className="wb-reminder"
                          key={w.id}
                          onClick={() => go(role, "work", w.id)}
                        >
                          <Badge tone="orange">
                            {w.approval === "pending" ? "动作审批" : "成果复核"}
                          </Badge>
                          <strong>{w.title}</strong>
                          <small>查看上下文与证据 →</small>
                        </button>
                      ))
                    ) : (
                      <p className="wb-placeholder">当前没有待办。</p>
                    )}
                  </Card>
                  <Card title="数字员工贡献">
                    <p className="wb-muted">与工作列表共用筛选与水位</p>
                    {snapshot.contributors.map((c) => (
                      <div className="wb-contribution" key={c.employee}>
                        <span>{c.employee}</span>
                        <meter
                          min={0}
                          max={Math.max(snapshot.total, 1)}
                          value={c.work_count}
                          aria-label={`${c.employee} 工作数量`}
                        />
                        <b>{c.work_count}</b>
                      </div>
                    ))}
                    {!snapshot.total && (
                      <p className="wb-placeholder">暂无匹配数据</p>
                    )}
                  </Card>
                </div>
              </div>
              {section === "overview" && (
                <>
                  <div className="wb-section-heading">
                    <h2>从参考场景开始</h2>
                    <span>相同工作模型，开放业务扩展</span>
                  </div>
                  <div className="wb-scenarios">
                    {SCENARIOS.map((s) => (
                      <button
                        className="wb-scenario"
                        key={s.id}
                        onClick={() => {
                          setNewScenario(s.id);
                          setNewTitle(s.brief);
                          setFormOpen(true);
                          window.scrollTo({ top: 0, behavior: "smooth" });
                        }}
                        disabled={role === "admin"}
                      >
                        <Avatar scenario={s.id} />
                        <h3>{s.title}</h3>
                        <p>{s.brief}</p>
                        <span>
                          {role === "admin"
                            ? "切换员工角色发起"
                            : "使用此场景 ↗"}
                        </span>
                      </button>
                    ))}
                  </div>
                </>
              )}
            </>
          )}
          {section === "work" &&
            workId &&
            (selected ? (
              <WorkDetail work={selected} role={role} act={act} />
            ) : (
              <Card title="工作未找到">
                <p className="wb-placeholder">
                  演示工作在刷新后重置，此链接对应的工作当前不存在。
                </p>
                <button
                  className="wb-secondary"
                  onClick={() => go(role, "work")}
                >
                  返回工作列表
                </button>
              </Card>
            ))}
          {section === "approvals" && (
            <Card
              title={role === "supervisor" ? "待你决定" : "审批与复核跟踪"}
              extra={<Badge>{pending.length} 项</Badge>}
            >
              <p className="wb-placeholder">
                {role === "supervisor"
                  ? "先查看成果、来源与授权上下文，再做出决定。"
                  : "主管角色可预览审批与验收操作；管理员角色保留观察入口。"}
              </p>
              {workList(pending)}
            </Card>
          )}
          {section === "employees" && (
            <>
              <div className="wb-scenarios">
                {SCENARIOS.map((s) => (
                  <Card
                    key={s.id}
                    title={s.employee}
                    extra={<Avatar scenario={s.id} />}
                  >
                    <p className="wb-placeholder">{s.permission}</p>
                    <dl className="wb-facts">
                      <div>
                        <dt>工作空间</dt>
                        <dd>{s.workspace}</dd>
                      </div>
                      <div>
                        <dt>实现绑定</dt>
                        <dd>待配置</dd>
                      </div>
                      <div>
                        <dt>协议 / 能力</dt>
                        <dd>待探测与验证</dd>
                      </div>
                      <div>
                        <dt>用量 / 费用</dt>
                        <dd>未知</dd>
                      </div>
                    </dl>
                    <button
                      className="wb-secondary"
                      onClick={() => {
                        setQuery({ scenario: s.id, state: "all", search: "" });
                        go(role, "work");
                      }}
                    >
                      查看相关工作
                    </button>
                  </Card>
                ))}
              </div>
              <Card title="实现与能力绑定">
                <p className="wb-placeholder">
                  岗位身份与执行实现分离。可接入 DeepSeek
                  Harness、Cursor、Codex、Claude Code 等实现；协议适配按
                  A2A、ACP 或厂商接口逐项验证。
                </p>
                <div className="wb-capabilities">
                  <Badge>任务：待验证</Badge>
                  <Badge>流式输出：待验证</Badge>
                  <Badge>取消：待验证</Badge>
                  <Badge>恢复：待验证</Badge>
                  <Badge>工具治理：待验证</Badge>
                </div>
                <button disabled>绑定端点 · 待接入身份与部署服务</button>
              </Card>
            </>
          )}
          {section === "spaces" && (
            <div className="wb-scenarios">
              {SCENARIOS.map((s) => (
                <Card
                  key={s.id}
                  title={s.workspace}
                  extra={<Avatar scenario={s.id} />}
                >
                  <p className="wb-placeholder">{s.input}</p>
                  <dl className="wb-facts">
                    <div>
                      <dt>成员范围</dt>
                      <dd>合成员工、主管</dd>
                    </div>
                    <div>
                      <dt>成果</dt>
                      <dd>{s.output}</dd>
                    </div>
                    <div>
                      <dt>共享规则</dt>
                      <dd>{s.permission}</dd>
                    </div>
                  </dl>
                  <button
                    className="wb-secondary"
                    onClick={() => {
                      setQuery({ scenario: s.id, state: "all", search: "" });
                      go(role, "work");
                    }}
                  >
                    进入空间工作
                  </button>
                  <button disabled>上传 / 分享 · 待接入 ACL 服务</button>
                </Card>
              ))}
            </div>
          )}
          {section === "knowledge" && (
            <Card title="知识与记忆的使用路径">
              <div className="wb-flow">
                {[
                  "选择空间资料",
                  "按身份检索",
                  "保留引用来源",
                  "成果复核",
                  "按权限分享",
                ].map((s, i) => (
                  <div key={s}>
                    <b>0{i + 1}</b>
                    <strong>{s}</strong>
                  </div>
                ))}
              </div>
              <p className="wb-placeholder">
                资料权限随检索、引用与分享持续校验。跨会话记忆遵循相同授权范围；知识与记忆后端通过可替换服务接入。
              </p>
              <div className="wb-actions">
                <button
                  className="wb-secondary"
                  onClick={() => go(role, "spaces")}
                >
                  查看空间与资料样例
                </button>
                <button disabled>接入知识源 · 待实现权限联动</button>
              </div>
            </Card>
          )}
          {section === "studio" && (
            <Card title="从需求到受控发布">
              <div className="wb-flow">
                {[
                  "描述工作目标",
                  "选择执行实现",
                  "预览变更",
                  "隔离评测",
                  "审批与发布",
                ].map((s, i) => (
                  <div key={s}>
                    <b>0{i + 1}</b>
                    <strong>{s}</strong>
                  </div>
                ))}
              </div>
              <p className="wb-placeholder">
                Studio 连接多个
                Harness。配置、工具授权和版本差异可复核；发布使用固定
                Revision，失败可回滚。
              </p>
              <div className="wb-actions">
                <button
                  className="wb-secondary"
                  onClick={() => go(role, "employees")}
                >
                  查看数字员工与实现边界
                </button>
                <button disabled>创建 / 发布 · 待部署与 E2AG 验收</button>
              </div>
            </Card>
          )}
          {section === "operations" && <Operations model={model} role={role} />}
          {!NAV.some((n) => n[0] === section) && (
            <Card title="页面未找到">
              <button
                className="wb-secondary"
                onClick={() => go(role, "overview")}
              >
                返回工作总览
              </button>
            </Card>
          )}
          <footer className="wb-footer">
            <span>DiOS · 工作有归属，执行可追溯，成果可验收</span>
            <span>演示数据水位 {snapshot.watermark}</span>
          </footer>
        </main>
      </div>
    </div>
  );
}

function WorkDetail({
  work,
  role,
  act,
}: {
  work: Work;
  role: Role;
  act: (type: Exclude<Action["type"], "create" | "reset">, id: string) => void;
}) {
  const s = SCENARIOS.find((s) => s.id === work.scenario)!;
  const run = currentRun(work);
  const terminal = work.state === "completed" || work.state === "cancelled";
  const canAct = role !== "admin" && !terminal;
  const result = `# ${work.title}\n\n> DiOS 合成场景成果，供交互评审。\n\n## 输入\n${s.input}\n\n## 交付要求\n${s.acceptance}\n\n## 示例结论\n${work.scenario === "review" ? "样例函数缺少空值判断，可能在空输入时访问不存在的字段。位置：demo/service.ts:8；待验证：调用路径及修复对照。此项不计成功覆盖。" : "示例初稿已形成。正式业务交付需要逐项确认输入约束、引用和适用范围。"}\n\n## 关联\nWorkItem: ${work.id}\nRun: ${run.id}\nArtifact: ${run.output || "无完整产物"}\n`;
  return (
    <>
      <button className="wb-text-button" onClick={() => go(role, "work")}>
        ← 返回工作列表
      </button>
      <div className="wb-detail-title">
        <Avatar scenario={work.scenario} />
        <div>
          <h2>{work.title}</h2>
          <p>
            {s.workspace} · {work.id}
          </p>
        </div>
        <Badge tone={work.state === "completed" ? "green" : "blue"}>
          {WORK_LABEL[work.state]}
        </Badge>
      </div>
      <div className="wb-content-grid">
        <div className="wb-side-stack">
          <Card title="工作目标与验收">
            <p className="wb-placeholder">{s.input}</p>
            <div className="wb-callout">
              <b>验收条件</b>
              <p>{s.acceptance}</p>
            </div>
            <div className="wb-actions">
              {canAct && !work.human && (
                <button
                  className="wb-secondary"
                  onClick={() => act("takeover", work.id)}
                >
                  请求人工接管
                </button>
              )}
              {work.human && <Badge tone="orange">人工接管已请求</Badge>}
              {canAct && work.state === "changes_requested" && (
                <button
                  className="wb-primary"
                  onClick={() => act("revise", work.id)}
                >
                  按反馈发起新一轮
                </button>
              )}
            </div>
          </Card>
          <Card
            title="成果与业务复核"
            extra={
              <Badge tone={run.phase === "succeeded" ? "green" : "muted"}>
                {run.phase === "succeeded" ? "完整成果样例" : "尚无完整成果"}
              </Badge>
            }
          >
            {run.phase === "succeeded" ? (
              <>
                <div className="wb-artifact">
                  <span>▧</span>
                  <div>
                    <strong>{s.output}</strong>
                    <small>{run.output} · 合成内容</small>
                  </div>
                  <button
                    className="wb-secondary"
                    onClick={() => download(s.output, result, "text/markdown")}
                  >
                    下载 Markdown
                  </button>
                </div>
                <pre className="wb-result">{result}</pre>
                <div className="wb-actions">
                  {role === "supervisor" && work.state === "awaiting_review" ? (
                    <>
                      <button
                        className="wb-primary"
                        onClick={() => act("accept", work.id)}
                      >
                        验收成果
                      </button>
                      <button
                        className="wb-secondary"
                        onClick={() => act("return", work.id)}
                      >
                        退回补充来源
                      </button>
                    </>
                  ) : (
                    <p className="wb-muted">
                      {work.state === "completed"
                        ? "业务成果已验收。"
                        : work.state === "changes_requested"
                          ? "补充来源后发起新一轮，原运行与成果保留。"
                          : "技术运行已完成，等待主管业务复核。"}
                    </p>
                  )}
                </div>
              </>
            ) : (
              <p className="wb-placeholder">
                {run.phase === "cooling"
                  ? "链路错误后的部分输出已保留。先对账，再决定恢复校验或创建新尝试。"
                  : "运行完成并通过结果校验后，成果进入业务复核。"}
              </p>
            )}
          </Card>
          {work.approval !== "none" && (
            <Card
              title="外发动作审批"
              extra={
                <Badge tone="orange">
                  {
                    { pending: "待审批", approved: "已批准", denied: "已拒绝" }[
                      work.approval
                    ]
                  }
                </Badge>
              }
            >
              <dl className="wb-facts">
                <div>
                  <dt>动作与对象</dt>
                  <dd>发送方案 → demo-recipient@example.invalid</dd>
                </div>
                <div>
                  <dt>绑定上下文</dt>
                  <dd>
                    {run.id} · {run.attemptIds.at(-1)} · 参数摘要与策略版本
                  </dd>
                </div>
                <div>
                  <dt>执行门槛</dt>
                  <dd>参数变更、授权撤销或过期须重新决策</dd>
                </div>
              </dl>
              <p className="wb-placeholder">
                审批样例仅改变演示状态；实际外发由 E2AG 在工具动作边界校验。
              </p>
              {role === "supervisor" && work.approval === "pending" && (
                <div className="wb-actions">
                  <button
                    className="wb-primary"
                    onClick={() => act("approve", work.id)}
                  >
                    批准此次动作
                  </button>
                  <button
                    className="wb-secondary"
                    onClick={() => act("deny", work.id)}
                  >
                    拒绝此次动作
                  </button>
                </div>
              )}
            </Card>
          )}
          <Card title="事件记录">
            <ol className="wb-timeline">
              {work.events.map((event, i) => (
                <li key={`${i}-${event}`}>
                  <span>#{i + 1}</span>
                  <p>{event}</p>
                </li>
              ))}
            </ol>
          </Card>
        </div>
        <div className="wb-side-stack">
          <Card
            title="运行观察"
            extra={
              <Badge tone={run.stale ? "orange" : "blue"}>
                {run.stale ? "观测已过期" : PHASE_LABEL[run.phase]}
              </Badge>
            }
          >
            <dl className="wb-facts">
              <div>
                <dt>Run</dt>
                <dd>{run.id}</dd>
              </div>
              <div>
                <dt>当前 Attempt</dt>
                <dd>{run.attemptIds.at(-1)}</dd>
              </div>
              <div>
                <dt>模型会话</dt>
                <dd>{run.sessionId || "尚未创建"}</dd>
              </div>
              <div>
                <dt>最近有效输出</dt>
                <dd>{run.output || "尚未产生"}</dd>
              </div>
              <div>
                <dt>实际并发 / 配置上限</dt>
                <dd>未知 / 3（配置样例）</dd>
              </div>
              <div>
                <dt>最近事件时间</dt>
                <dd>未知 · 当前为演示序号</dd>
              </div>
              <div>
                <dt>进度 / 详情新鲜度</dt>
                <dd>未连接 / 未连接</dd>
              </div>
              <div>
                <dt>最近完成时间 / 用量</dt>
                <dd>未知 / 未报告</dd>
              </div>
              {run.phase === "cooling" && (
                <>
                  <div>
                    <dt>失败原因</dt>
                    <dd>网络链路中断（样例）</dd>
                  </div>
                  <div>
                    <dt>下次重试时间</dt>
                    <dd>待执行端提供</dd>
                  </div>
                </>
              )}
            </dl>
            {run.stale && (
              <div className="wb-callout">
                <b>等待观测恢复</b>
                <p>
                  最后已知状态：{PHASE_LABEL[run.phase]}
                  。查询超时保留运行身份，先对账。
                </p>
              </div>
            )}
            <div className="wb-demo-controls">
              <span>演示控制</span>
              <div className="wb-actions">
                {canAct &&
                  ![
                    "succeeded",
                    "cancelled",
                    "cooling",
                    "cancel_requested",
                  ].includes(run.phase) && (
                    <button
                      className="wb-primary"
                      disabled={run.stale || work.approval === "pending"}
                      onClick={() => act("advance", work.id)}
                    >
                      推进一个演示阶段
                    </button>
                  )}
                {canAct && run.phase === "cooling" && (
                  <button
                    className="wb-primary"
                    disabled={run.stale}
                    onClick={() => act("retry", work.id)}
                  >
                    演示对账后重试
                  </button>
                )}
                {canAct &&
                  !["succeeded", "cancelled", "cancel_requested"].includes(
                    run.phase,
                  ) && (
                    <button
                      className="wb-secondary"
                      onClick={() => act("cancel", work.id)}
                    >
                      请求取消
                    </button>
                  )}
                {canAct && run.phase === "cancel_requested" && (
                  <button
                    className="wb-secondary"
                    onClick={() => act("confirm_cancel", work.id)}
                  >
                    演示执行端确认取消
                  </button>
                )}
                <button
                  className="wb-secondary"
                  onClick={() => act("stale", work.id)}
                >
                  {run.stale ? "恢复演示观测" : "演示观测过期"}
                </button>
              </div>
              {work.approval === "pending" && (
                <p className="wb-muted">此样例等待主管审批。</p>
              )}
            </div>
          </Card>
          <Card title="历次运行与尝试">
            {work.runs.map((r) => (
              <div className="wb-history" key={r.id}>
                <strong>{r.id}</strong>
                <span>{PHASE_LABEL[r.phase]}</span>
                <small>{r.attemptIds.join(" · ")}</small>
                {r.previousAttempts.map((a) => (
                  <small key={a.id}>
                    保留尝试 {a.id} · 会话 {a.sessionId || "未创建"} · 产物{" "}
                    {a.output || "未产生"}
                  </small>
                ))}
                <small>
                  会话 {r.sessionId || "未创建"} · 产物 {r.output || "未产生"}
                </small>
              </div>
            ))}
          </Card>
          {work.scenario === "review" && (
            <Card title="覆盖账本口径">
              <dl className="wb-facts">
                <div>
                  <dt>当前分支 / 版本</dt>
                  <dd>release/next @ 4a1c9e2（样例）</dd>
                </div>
                <div>
                  <dt>当前范围成功覆盖</dt>
                  <dd>未知 · 待覆盖账本接入</dd>
                </div>
                <div>
                  <dt>全历史完成量</dt>
                  <dd>未知 · 待历史查询接入</dd>
                </div>
                <div>
                  <dt>共享代码复用</dt>
                  <dd>相同内容与相容上下文分别核验</dd>
                </div>
              </dl>
              <p className="wb-placeholder">
                证据不足进入待验证；分支关联和覆盖分母持续保留。
              </p>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
function Operations({ model, role }: { model: Model; role: Role }) {
  return (
    <>
      <div className="wb-metrics">
        {[
          ["实际 Worker / 会话并发", "未知"],
          ["配置并发上限", "3 · 样例"],
          ["调度器观测", "未连接"],
          ["看板同步", "未连接"],
        ].map(([label, value]) => (
          <div className="wb-metric" key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <small>需要独立观测证据</small>
          </div>
        ))}
      </div>
      <Card title="工作与运行关联">
        <div className="wb-work-list">
          {model.works.map((w) => (
            <button
              className="wb-work-row"
              key={w.id}
              onClick={() => go(role, "work", w.id)}
            >
              <Avatar scenario={w.scenario} />
              <span className="wb-work-title">
                <strong>{w.title}</strong>
                <small>
                  {currentRun(w).id} · {currentRun(w).attemptIds.length}{" "}
                  次演示尝试
                </small>
              </span>
              <Badge tone={currentRun(w).stale ? "orange" : "muted"}>
                {currentRun(w).stale
                  ? "观测已过期"
                  : PHASE_LABEL[currentRun(w).phase]}
              </Badge>
            </button>
          ))}
        </div>
      </Card>
      <div className="wb-two-col">
        <Card title="运行与交付趋势">
          <div className="wb-empty">
            <span>⌁</span>
            <h3>趋势数据尚未接入</h3>
            <p>每小时 / 每天完成量按事件水位聚合；观测缺失显示未知。</p>
          </div>
        </Card>
        <Card title="平台管理入口">
          <p className="wb-placeholder">
            Console 管理内核运行、连接器与 Registry；CLI
            保留运维与自动化入口。企业组织、凭据、策略与审计沿统一权限服务接入。
          </p>
          <div className="wb-actions">
            <a className="wb-link-button" href="#console/topology">
              打开 Console ↗
            </a>
            <button disabled>生产治理操作 · 待身份与 E2AG 验收</button>
          </div>
        </Card>
      </div>
    </>
  );
}
