import { t as translateUI } from "../../lib/i18n";
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
import PreferencesControls from "../../components/PreferencesControls";

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
        <h2>{translateUI(title)}</h2>
        {translateUI(extra)}
      </div>
      {translateUI(children)}
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
  return <span className={`wb-badge ${tone}`}>{translateUI(children)}</span>;
}
function Avatar({ scenario }: { scenario: ScenarioId }) {
  const s = SCENARIOS.find((s) => s.id === scenario)!;
  return <span className={`wb-avatar ${s.color}`}>{translateUI(s.initials)}</span>;
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
          aria-label={translateUI("搜索工作")}
          placeholder={translateUI("搜索工作名称或编号")}
          value={query.search}
          onChange={(e) => setQuery({ ...query, search: e.target.value })}
        />
      </label>
      <select
        aria-label={translateUI("筛选场景")}
        value={query.scenario}
        onChange={(e) =>
          setQuery({
            ...query,
            scenario: e.target.value as QuerySpec["scenario"],
          })
        }
      >
        <option value="all">{translateUI("全部场景")}</option>
        {SCENARIOS.map((s) => (
          <option key={s.id} value={s.id}>
            {s.title}
          </option>
        ))}
      </select>
      <select
        aria-label={translateUI("筛选工作状态")}
        value={query.state}
        onChange={(e) =>
          setQuery({ ...query, state: e.target.value as QuerySpec["state"] })
        }
      >
        <option value="all">{translateUI("全部状态")}</option>
        {Object.entries(WORK_LABEL).map(([k, v]) => (
          <option key={k} value={k}>
            {translateUI(v)}
          </option>
        ))}
      </select>
      <button
        className="wb-secondary"
        onClick={() =>
          download("dios-demo-work.json", JSON.stringify(snapshot, null, 2))
        }
      >{translateUI("导出当前视图")}</button>
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
                {translateUI(SCENARIOS.find((s) => s.id === w.scenario)!.employee)} · {translateUI(w.id)}
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
                {translateUI(WORK_LABEL[w.state])}
              </Badge>
              <small>
                {translateUI(currentRun(w).stale
                  ? "观测已过期"
                  : PHASE_LABEL[currentRun(w).phase])}
              </small>
            </span>
            <span aria-hidden="true">↗</span>
          </button>
        ))}
      </div>
    ) : (
      <div className="wb-empty">
        <span>◇</span>
        <h3>{translateUI("当前没有匹配的工作")}</h3>
        <p>{translateUI("调整场景、状态或搜索条件后查看。")}</p>
        <button
          className="wb-secondary"
          onClick={() =>
            setQuery({ scenario: "all", state: "all", search: "" })
          }
        >{translateUI("清除筛选")}</button>
      </div>
    );

  return (
    <div className="workbench">
      <aside className="wb-sidebar">
        <a href="#workbench/employee/overview" className="wb-brand">
          <span className="wb-brand-mark">D</span>
          <span>
            DiOS<small>{translateUI("数字员工服务平台")}</small>
          </span>
        </a>
        <div className="wb-org">
          <span className="wb-org-icon">N</span>
          <span>{translateUI("北辰工作空间")}<small>{translateUI("合成组织 · 参考场景")}</small>
          </span>
        </div>
        <span className="wb-nav-label">{translateUI("WORKSPACE")}</span>
        <nav aria-label={translateUI("工作台导航")}>
          {NAV.map(([key, icon, label]) => (
            <a
              key={key}
              className={section === key ? "active" : ""}
              aria-current={section === key ? "page" : undefined}
              href={`#workbench/${role}/${key}`}
            >
              <span aria-hidden="true">{translateUI(icon)}</span>
              {translateUI(label)}
              {key === "approvals" && pending.length > 0 && (
                <i>{translateUI(pending.length)}</i>
              )}
            </a>
          ))}
        </nav>
        <div className="wb-sidebar-bottom">
          <a href="#console/topology">{translateUI("↗ 内核运维 Console")}</a>
          <p>{translateUI("业务工作与底层运行分层管理")}</p>
        </div>
      </aside>
      <div className="wb-shell">
        <header className="wb-topbar">
          <PreferencesControls />
          <span>
            <span className="wb-muted">{translateUI("工作台")}</span>
            <span className="wb-divider">/</span>
            {translateUI(title)}
          </span>
          <label className="wb-role">{translateUI("角色预览")}<select
              aria-label={translateUI("角色预览")}
              value={role}
              onChange={(e) => go(e.target.value as Role, section, workId)}
            >
              {Object.entries(ROLES).map(([key, label]) => (
                <option key={key} value={key}>
                  {translateUI(label)}
                </option>
              ))}
            </select>
          </label>
        </header>
        <div className="wb-prototype">
          <span>
            <b>{translateUI("交互原型")}</b>{translateUI("合成数据 · 操作仅影响本页，刷新重置")}</span>
          <button
            onClick={() => {
              dispatch({ type: "reset" });
              setQuery({ scenario: "all", state: "all", search: "" });
              setFormOpen(false);
              go(role, "overview");
            }}
          >{translateUI("重置演示")}</button>
        </div>
        <main className="wb-main">
          <div className="wb-page-heading">
            <div>
              <div className="wb-eyebrow">
                {translateUI(role === "supervisor"
                  ? "TEAM WORKSPACE"
                  : role === "admin"
                    ? "PLATFORM OVERVIEW"
                    : "MY WORKSPACE")}
              </div>
              <h1>
                {translateUI(section === "overview"
                  ? role === "supervisor"
                    ? "让团队成果，走向交付"
                    : role === "admin"
                      ? "连接能力，守住运行边界"
                      : "把工作交给可靠的协作伙伴"
                  : title)}
              </h1>
              <p>
                {translateUI(role === "admin"
                  ? "查看能力、运行与治理边界；业务验收由工作负责人完成。"
                  : role === "supervisor"
                    ? "关注团队交付、待复核成果与需要你决定的事项。"
                    : "从一个明确目标开始，跟踪进展，接收可追溯的成果。")}
              </p>
            </div>
            {role !== "admin" && (
              <button className="wb-primary" onClick={() => setFormOpen(true)}>{translateUI("＋ 发起工作")}</button>
            )}
          </div>
          {model.notice && (
            <div className="wb-notice" role="status">
              {translateUI(model.notice)}
            </div>
          )}
          {formOpen && (
            <Card
              title={translateUI("发起一项工作")}
              extra={
                <button
                  className="wb-text-button"
                  onClick={() => setFormOpen(false)}
                >{translateUI("收起")}</button>
              }
            >
              <form className="wb-form" onSubmit={createWork}>
                <label>{translateUI("选择参考场景")}<select
                    value={newScenario}
                    onChange={(e) =>
                      setNewScenario(e.target.value as ScenarioId)
                    }
                  >
                    {SCENARIOS.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.title} · {translateUI(s.employee)}
                      </option>
                    ))}
                  </select>
                </label>
                <label>{translateUI("工作目标")}<input
                    required
                    maxLength={100}
                    value={newTitle}
                    onChange={(e) => setNewTitle(e.target.value)}
                    placeholder={translateUI(
                      SCENARIOS.find((s) => s.id === newScenario)!.brief
                    )}
                  />
                </label>
                <p>{translateUI(SCENARIOS.find((s) => s.id === newScenario)!.acceptance)}</p>
                <button
                  type="submit"
                  className="wb-primary"
                  disabled={!newTitle.trim()}
                >{translateUI("创建演示工作")}</button>
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
                      <span>{translateUI(label)}</span>
                      <strong>{translateUI(value)}</strong>
                      <small>{translateUI(note)}</small>
                    </div>
                  ))}
                </div>
              )}
              <div className="wb-content-grid">
                <Card
                  title={translateUI(role === "supervisor" ? "团队工作" : "工作进展")}
                  extra={<Badge>{translateUI("水位")}{translateUI(snapshot.watermark)}</Badge>}
                >
                  {translateUI(filters)}
                  {translateUI(workList(works))}
                </Card>
                <div className="wb-side-stack">
                  <Card title={translateUI("待办提醒")}>
                    <p className="wb-muted">{translateUI("组织演示范围 · 不受工作筛选影响")}</p>
                    {pending.length ? (
                      pending.map((w) => (
                        <button
                          className="wb-reminder"
                          key={w.id}
                          onClick={() => go(role, "work", w.id)}
                        >
                          <Badge tone="orange">
                            {translateUI(w.approval === "pending" ? "动作审批" : "成果复核")}
                          </Badge>
                          <strong>{w.title}</strong>
                          <small>{translateUI("查看上下文与证据 →")}</small>
                        </button>
                      ))
                    ) : (
                      <p className="wb-placeholder">{translateUI("当前没有待办。")}</p>
                    )}
                  </Card>
                  <Card title={translateUI("数字员工贡献")}>
                    <p className="wb-muted">{translateUI("与工作列表共用筛选与水位")}</p>
                    {snapshot.contributors.map((c) => (
                      <div className="wb-contribution" key={c.employee}>
                        <span>{translateUI(c.employee)}</span>
                        <meter
                          min={0}
                          max={Math.max(snapshot.total, 1)}
                          value={c.work_count}
                          aria-label={translateUI(translateUI("{0} 工作数量", [c.employee]))}
                        />
                        <b>{translateUI(c.work_count)}</b>
                      </div>
                    ))}
                    {!snapshot.total && (
                      <p className="wb-placeholder">{translateUI("暂无匹配数据")}</p>
                    )}
                  </Card>
                </div>
              </div>
              {section === "overview" && (
                <>
                  <div className="wb-section-heading">
                    <h2>{translateUI("从参考场景开始")}</h2>
                    <span>{translateUI("相同工作模型，开放业务扩展")}</span>
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
                        <p>{translateUI(s.brief)}</p>
                        <span>
                          {translateUI(role === "admin"
                            ? "切换员工角色发起"
                            : "使用此场景 ↗")}
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
              <Card title={translateUI("工作未找到")}>
                <p className="wb-placeholder">{translateUI("演示工作在刷新后重置，此链接对应的工作当前不存在。")}</p>
                <button
                  className="wb-secondary"
                  onClick={() => go(role, "work")}
                >{translateUI("返回工作列表")}</button>
              </Card>
            ))}
          {section === "approvals" && (
            <Card
              title={translateUI(role === "supervisor" ? "待你决定" : "审批与复核跟踪")}
              extra={<Badge>{translateUI(pending.length)}{translateUI("项")}</Badge>}
            >
              <p className="wb-placeholder">
                {translateUI(role === "supervisor"
                  ? "先查看成果、来源与授权上下文，再做出决定。"
                  : "主管角色可预览审批与验收操作；管理员角色保留观察入口。")}
              </p>
              {translateUI(workList(pending))}
            </Card>
          )}
          {section === "employees" && (
            <>
              <div className="wb-scenarios">
                {SCENARIOS.map((s) => (
                  <Card
                    key={s.id}
                    title={translateUI(s.employee)}
                    extra={<Avatar scenario={s.id} />}
                  >
                    <p className="wb-placeholder">{translateUI(s.permission)}</p>
                    <dl className="wb-facts">
                      <div>
                        <dt>{translateUI("工作空间")}</dt>
                        <dd>{translateUI(s.workspace)}</dd>
                      </div>
                      <div>
                        <dt>{translateUI("实现绑定")}</dt>
                        <dd>{translateUI("待配置")}</dd>
                      </div>
                      <div>
                        <dt>{translateUI("协议 / 能力")}</dt>
                        <dd>{translateUI("待探测与验证")}</dd>
                      </div>
                      <div>
                        <dt>{translateUI("用量 / 费用")}</dt>
                        <dd>{translateUI("未知")}</dd>
                      </div>
                    </dl>
                    <button
                      className="wb-secondary"
                      onClick={() => {
                        setQuery({ scenario: s.id, state: "all", search: "" });
                        go(role, "work");
                      }}
                    >{translateUI("查看相关工作")}</button>
                  </Card>
                ))}
              </div>
              <Card title={translateUI("实现与能力绑定")}>
                <p className="wb-placeholder">{translateUI("岗位身份与执行实现分离。可接入 DeepSeek Harness、Cursor、Codex、Claude Code 等实现；协议适配按 A2A、ACP 或厂商接口逐项验证。")}</p>
                <div className="wb-capabilities">
                  <Badge>{translateUI("任务：待验证")}</Badge>
                  <Badge>{translateUI("流式输出：待验证")}</Badge>
                  <Badge>{translateUI("取消：待验证")}</Badge>
                  <Badge>{translateUI("恢复：待验证")}</Badge>
                  <Badge>{translateUI("工具治理：待验证")}</Badge>
                </div>
                <button disabled>{translateUI("绑定端点 · 待接入身份与部署服务")}</button>
              </Card>
            </>
          )}
          {section === "spaces" && (
            <div className="wb-scenarios">
              {SCENARIOS.map((s) => (
                <Card
                  key={s.id}
                  title={translateUI(s.workspace)}
                  extra={<Avatar scenario={s.id} />}
                >
                  <p className="wb-placeholder">{translateUI(s.input)}</p>
                  <dl className="wb-facts">
                    <div>
                      <dt>{translateUI("成员范围")}</dt>
                      <dd>{translateUI("合成员工、主管")}</dd>
                    </div>
                    <div>
                      <dt>{translateUI("成果")}</dt>
                      <dd>{translateUI(s.output)}</dd>
                    </div>
                    <div>
                      <dt>{translateUI("共享规则")}</dt>
                      <dd>{translateUI(s.permission)}</dd>
                    </div>
                  </dl>
                  <button
                    className="wb-secondary"
                    onClick={() => {
                      setQuery({ scenario: s.id, state: "all", search: "" });
                      go(role, "work");
                    }}
                  >{translateUI("进入空间工作")}</button>
                  <button disabled>{translateUI("上传 / 分享 · 待接入 ACL 服务")}</button>
                </Card>
              ))}
            </div>
          )}
          {section === "knowledge" && (
            <Card title={translateUI("知识与记忆的使用路径")}>
              <div className="wb-flow">
                {[
                  "选择空间资料",
                  "按身份检索",
                  "保留引用来源",
                  "成果复核",
                  "按权限分享",
                ].map((s, i) => (
                  <div key={s}>
                    <b>0{translateUI(i + 1)}</b>
                    <strong>{translateUI(s)}</strong>
                  </div>
                ))}
              </div>
              <p className="wb-placeholder">{translateUI("资料权限随检索、引用与分享持续校验。跨会话记忆遵循相同授权范围；知识与记忆后端通过可替换服务接入。")}</p>
              <div className="wb-actions">
                <button
                  className="wb-secondary"
                  onClick={() => go(role, "spaces")}
                >{translateUI("查看空间与资料样例")}</button>
                <button disabled>{translateUI("接入知识源 · 待实现权限联动")}</button>
              </div>
            </Card>
          )}
          {section === "studio" && (
            <Card title={translateUI("从需求到受控发布")}>
              <div className="wb-flow">
                {[
                  "描述工作目标",
                  "选择执行实现",
                  "预览变更",
                  "隔离评测",
                  "审批与发布",
                ].map((s, i) => (
                  <div key={s}>
                    <b>0{translateUI(i + 1)}</b>
                    <strong>{translateUI(s)}</strong>
                  </div>
                ))}
              </div>
              <p className="wb-placeholder">{translateUI("Studio 连接多个 Harness。配置、工具授权和版本差异可复核；发布使用固定 Revision，失败可回滚。")}</p>
              <div className="wb-actions">
                <button
                  className="wb-secondary"
                  onClick={() => go(role, "employees")}
                >{translateUI("查看数字员工与实现边界")}</button>
                <button disabled>{translateUI("创建 / 发布 · 待部署与 E2AG 验收")}</button>
              </div>
            </Card>
          )}
          {section === "operations" && <Operations model={model} role={role} />}
          {!NAV.some((n) => n[0] === section) && (
            <Card title={translateUI("页面未找到")}>
              <button
                className="wb-secondary"
                onClick={() => go(role, "overview")}
              >{translateUI("返回工作总览")}</button>
            </Card>
          )}
          <footer className="wb-footer">
            <span>{translateUI("DiOS · 工作有归属，执行可追溯，成果可验收")}</span>
            <span>{translateUI("演示数据水位")}{translateUI(snapshot.watermark)}</span>
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
  const result = `# ${work.title}

> DiOS 合成场景成果，供交互评审。

## 输入
${s.input}

## 交付要求
${s.acceptance}

## 示例结论
${work.scenario === "review" ? "样例函数缺少空值判断，可能在空输入时访问不存在的字段。位置：demo/service.ts:8；待验证：调用路径及修复对照。此项不计成功覆盖。" : "示例初稿已形成。正式业务交付需要逐项确认输入约束、引用和适用范围。"}

## 关联
WorkItem: ${work.id}
Run: ${run.id}
Artifact: ${run.output || "无完整产物"}
`;
  return (
    <>
      <button className="wb-text-button" onClick={() => go(role, "work")}>{translateUI("← 返回工作列表")}</button>
      <div className="wb-detail-title">
        <Avatar scenario={work.scenario} />
        <div>
          <h2>{work.title}</h2>
          <p>
            {translateUI(s.workspace)} · {translateUI(work.id)}
          </p>
        </div>
        <Badge tone={work.state === "completed" ? "green" : "blue"}>
          {translateUI(WORK_LABEL[work.state])}
        </Badge>
      </div>
      <div className="wb-content-grid">
        <div className="wb-side-stack">
          <Card title={translateUI("工作目标与验收")}>
            <p className="wb-placeholder">{translateUI(s.input)}</p>
            <div className="wb-callout">
              <b>{translateUI("验收条件")}</b>
              <p>{translateUI(s.acceptance)}</p>
            </div>
            <div className="wb-actions">
              {canAct && !work.human && (
                <button
                  className="wb-secondary"
                  onClick={() => act("takeover", work.id)}
                >{translateUI("请求人工接管")}</button>
              )}
              {work.human && <Badge tone="orange">{translateUI("人工接管已请求")}</Badge>}
              {canAct && work.state === "changes_requested" && (
                <button
                  className="wb-primary"
                  onClick={() => act("revise", work.id)}
                >{translateUI("按反馈发起新一轮")}</button>
              )}
            </div>
          </Card>
          <Card
            title={translateUI("成果与业务复核")}
            extra={
              <Badge tone={run.phase === "succeeded" ? "green" : "muted"}>
                {translateUI(run.phase === "succeeded" ? "完整成果样例" : "尚无完整成果")}
              </Badge>
            }
          >
            {run.phase === "succeeded" ? (
              <>
                <div className="wb-artifact">
                  <span>▧</span>
                  <div>
                    <strong>{translateUI(s.output)}</strong>
                    <small>{translateUI(run.output)}{translateUI("· 合成内容")}</small>
                  </div>
                  <button
                    className="wb-secondary"
                    onClick={() => download(s.output, result, "text/markdown")}
                  >{translateUI("下载 Markdown")}</button>
                </div>
                <pre className="wb-result">{result}</pre>
                <div className="wb-actions">
                  {role === "supervisor" && work.state === "awaiting_review" ? (
                    <>
                      <button
                        className="wb-primary"
                        onClick={() => act("accept", work.id)}
                      >{translateUI("验收成果")}</button>
                      <button
                        className="wb-secondary"
                        onClick={() => act("return", work.id)}
                      >{translateUI("退回补充来源")}</button>
                    </>
                  ) : (
                    <p className="wb-muted">
                      {translateUI(work.state === "completed"
                        ? "业务成果已验收。"
                        : work.state === "changes_requested"
                          ? "补充来源后发起新一轮，原运行与成果保留。"
                          : "技术运行已完成，等待主管业务复核。")}
                    </p>
                  )}
                </div>
              </>
            ) : (
              <p className="wb-placeholder">
                {translateUI(run.phase === "cooling"
                  ? "链路错误后的部分输出已保留。先对账，再决定恢复校验或创建新尝试。"
                  : "运行完成并通过结果校验后，成果进入业务复核。")}
              </p>
            )}
          </Card>
          {work.approval !== "none" && (
            <Card
              title={translateUI("外发动作审批")}
              extra={
                <Badge tone="orange">
                  {translateUI(
                    { pending: "待审批", approved: "已批准", denied: "已拒绝" }[
                      work.approval
                    ]
                  )}
                </Badge>
              }
            >
              <dl className="wb-facts">
                <div>
                  <dt>{translateUI("动作与对象")}</dt>
                  <dd>{translateUI("发送方案 → demo-recipient@example.invalid")}</dd>
                </div>
                <div>
                  <dt>{translateUI("绑定上下文")}</dt>
                  <dd>
                    {translateUI(run.id)} · {translateUI(run.attemptIds.at(-1))}{translateUI("· 参数摘要与策略版本")}</dd>
                </div>
                <div>
                  <dt>{translateUI("执行门槛")}</dt>
                  <dd>{translateUI("参数变更、授权撤销或过期须重新决策")}</dd>
                </div>
              </dl>
              <p className="wb-placeholder">{translateUI("审批样例仅改变演示状态；实际外发由 E2AG 在工具动作边界校验。")}</p>
              {role === "supervisor" && work.approval === "pending" && (
                <div className="wb-actions">
                  <button
                    className="wb-primary"
                    onClick={() => act("approve", work.id)}
                  >{translateUI("批准此次动作")}</button>
                  <button
                    className="wb-secondary"
                    onClick={() => act("deny", work.id)}
                  >{translateUI("拒绝此次动作")}</button>
                </div>
              )}
            </Card>
          )}
          <Card title={translateUI("事件记录")}>
            <ol className="wb-timeline">
              {work.events.map((event, i) => (
                <li key={`${i}-${event}`}>
                  <span>#{translateUI(i + 1)}</span>
                  <p>{translateUI(event)}</p>
                </li>
              ))}
            </ol>
          </Card>
        </div>
        <div className="wb-side-stack">
          <Card
            title={translateUI("运行观察")}
            extra={
              <Badge tone={run.stale ? "orange" : "blue"}>
                {translateUI(run.stale ? "观测已过期" : PHASE_LABEL[run.phase])}
              </Badge>
            }
          >
            <dl className="wb-facts">
              <div>
                <dt>Run</dt>
                <dd>{translateUI(run.id)}</dd>
              </div>
              <div>
                <dt>{translateUI("当前 Attempt")}</dt>
                <dd>{translateUI(run.attemptIds.at(-1))}</dd>
              </div>
              <div>
                <dt>{translateUI("模型会话")}</dt>
                <dd>{translateUI(run.sessionId || "尚未创建")}</dd>
              </div>
              <div>
                <dt>{translateUI("最近有效输出")}</dt>
                <dd>{translateUI(run.output || "尚未产生")}</dd>
              </div>
              <div>
                <dt>{translateUI("实际并发 / 配置上限")}</dt>
                <dd>{translateUI("未知 / 3（配置样例）")}</dd>
              </div>
              <div>
                <dt>{translateUI("最近事件时间")}</dt>
                <dd>{translateUI("未知 · 当前为演示序号")}</dd>
              </div>
              <div>
                <dt>{translateUI("进度 / 详情新鲜度")}</dt>
                <dd>{translateUI("未连接 / 未连接")}</dd>
              </div>
              <div>
                <dt>{translateUI("最近完成时间 / 用量")}</dt>
                <dd>{translateUI("未知 / 未报告")}</dd>
              </div>
              {run.phase === "cooling" && (
                <>
                  <div>
                    <dt>{translateUI("失败原因")}</dt>
                    <dd>{translateUI("网络链路中断（样例）")}</dd>
                  </div>
                  <div>
                    <dt>{translateUI("下次重试时间")}</dt>
                    <dd>{translateUI("待执行端提供")}</dd>
                  </div>
                </>
              )}
            </dl>
            {run.stale && (
              <div className="wb-callout">
                <b>{translateUI("等待观测恢复")}</b>
                <p>{translateUI("最后已知状态：")}{translateUI(PHASE_LABEL[run.phase])}{translateUI("。查询超时保留运行身份，先对账。")}</p>
              </div>
            )}
            <div className="wb-demo-controls">
              <span>{translateUI("演示控制")}</span>
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
                    >{translateUI("推进一个演示阶段")}</button>
                  )}
                {canAct && run.phase === "cooling" && (
                  <button
                    className="wb-primary"
                    disabled={run.stale}
                    onClick={() => act("retry", work.id)}
                  >{translateUI("演示对账后重试")}</button>
                )}
                {canAct &&
                  !["succeeded", "cancelled", "cancel_requested"].includes(
                    run.phase,
                  ) && (
                    <button
                      className="wb-secondary"
                      onClick={() => act("cancel", work.id)}
                    >{translateUI("请求取消")}</button>
                  )}
                {canAct && run.phase === "cancel_requested" && (
                  <button
                    className="wb-secondary"
                    onClick={() => act("confirm_cancel", work.id)}
                  >{translateUI("演示执行端确认取消")}</button>
                )}
                <button
                  className="wb-secondary"
                  onClick={() => act("stale", work.id)}
                >
                  {translateUI(run.stale ? "恢复演示观测" : "演示观测过期")}
                </button>
              </div>
              {work.approval === "pending" && (
                <p className="wb-muted">{translateUI("此样例等待主管审批。")}</p>
              )}
            </div>
          </Card>
          <Card title={translateUI("历次运行与尝试")}>
            {work.runs.map((r) => (
              <div className="wb-history" key={r.id}>
                <strong>{translateUI(r.id)}</strong>
                <span>{translateUI(PHASE_LABEL[r.phase])}</span>
                <small>{translateUI(r.attemptIds.join(" · "))}</small>
                {r.previousAttempts.map((a) => (
                  <small key={a.id}>{translateUI("保留尝试")}{translateUI(a.id)}{translateUI("· 会话")}{translateUI(a.sessionId || "未创建")}{translateUI("· 产物")}{translateUI(" ")}
                    {translateUI(a.output || "未产生")}
                  </small>
                ))}
                <small>{translateUI("会话")}{translateUI(r.sessionId || "未创建")}{translateUI("· 产物")}{translateUI(r.output || "未产生")}
                </small>
              </div>
            ))}
          </Card>
          {work.scenario === "review" && (
            <Card title={translateUI("覆盖账本口径")}>
              <dl className="wb-facts">
                <div>
                  <dt>{translateUI("当前分支 / 版本")}</dt>
                  <dd>{translateUI("release/next @ 4a1c9e2（样例）")}</dd>
                </div>
                <div>
                  <dt>{translateUI("当前范围成功覆盖")}</dt>
                  <dd>{translateUI("未知 · 待覆盖账本接入")}</dd>
                </div>
                <div>
                  <dt>{translateUI("全历史完成量")}</dt>
                  <dd>{translateUI("未知 · 待历史查询接入")}</dd>
                </div>
                <div>
                  <dt>{translateUI("共享代码复用")}</dt>
                  <dd>{translateUI("相同内容与相容上下文分别核验")}</dd>
                </div>
              </dl>
              <p className="wb-placeholder">{translateUI("证据不足进入待验证；分支关联和覆盖分母持续保留。")}</p>
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
            <span>{translateUI(label)}</span>
            <strong>{translateUI(value)}</strong>
            <small>{translateUI("需要独立观测证据")}</small>
          </div>
        ))}
      </div>
      <Card title={translateUI("工作与运行关联")}>
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
                  {translateUI(currentRun(w).id)} · {translateUI(currentRun(w).attemptIds.length)}{translateUI(" ")}{translateUI("次演示尝试")}</small>
              </span>
              <Badge tone={currentRun(w).stale ? "orange" : "muted"}>
                {translateUI(currentRun(w).stale
                  ? "观测已过期"
                  : PHASE_LABEL[currentRun(w).phase])}
              </Badge>
            </button>
          ))}
        </div>
      </Card>
      <div className="wb-two-col">
        <Card title={translateUI("运行与交付趋势")}>
          <div className="wb-empty">
            <span>⌁</span>
            <h3>{translateUI("趋势数据尚未接入")}</h3>
            <p>{translateUI("每小时 / 每天完成量按事件水位聚合；观测缺失显示未知。")}</p>
          </div>
        </Card>
        <Card title={translateUI("平台管理入口")}>
          <p className="wb-placeholder">{translateUI("Console 管理内核运行、连接器与 Registry；CLI 保留运维与自动化入口。企业组织、凭据、策略与审计沿统一权限服务接入。")}</p>
          <div className="wb-actions">
            <a className="wb-link-button" href="#console/topology">{translateUI("打开 Console ↗")}</a>
            <button disabled>{translateUI("生产治理操作 · 待身份与 E2AG 验收")}</button>
          </div>
        </Card>
      </div>
    </>
  );
}
