/** Synthetic presentation model. No API DTOs, credentials, network or persistence. */
export type Role = "employee" | "supervisor" | "admin";
export type ScenarioId = "sales" | "team" | "education" | "review";
export type WorkState =
  | "assigned"
  | "in_progress"
  | "awaiting_review"
  | "changes_requested"
  | "completed"
  | "cancelled";
export type Phase =
  | "queued"
  | "preparing"
  | "creating_session"
  | "waiting_model"
  | "streaming"
  | "validating"
  | "succeeded"
  | "cooling"
  | "cancel_requested"
  | "cancelled";
export type Scenario = {
  id: ScenarioId;
  title: string;
  employee: string;
  initials: string;
  color: string;
  workspace: string;
  brief: string;
  input: string;
  acceptance: string;
  output: string;
  permission: string;
};
export const SCENARIOS: Scenario[] = [
  {
    id: "sales",
    title: "专业服务",
    employee: "方案顾问",
    initials: "方",
    color: "purple",
    workspace: "客户方案空间",
    brief: "准备设备选型建议",
    input: "合成需求：室内巡检，续航至少 4 小时；提供两份公开规格样例。",
    acceptance: "规格有来源、约束逐项回应，外发前由主管确认。",
    output: "设备选型建议.md",
    permission: "读取本空间规格；外发需逐次审批",
  },
  {
    id: "team",
    title: "岗位协作",
    employee: "项目协调员",
    initials: "协",
    color: "blue",
    workspace: "项目协作空间",
    brief: "汇总下周岗位计划",
    input: "三个虚构岗位的计划样例；其中一项缺少责任人。",
    acceptance: "保留各岗位来源，缺项交还责任人，主管复核汇总。",
    output: "岗位计划汇总.md",
    permission: "读取受托岗位材料；委派范围不可扩大",
  },
  {
    id: "education",
    title: "教育数字分身",
    employee: "教学助理",
    initials: "教",
    color: "green",
    workspace: "课程准备空间",
    brief: "整理课程导读与练习",
    input: "合成课程讲义，包含公开段落与仅教师可见的答案。",
    acceptance: "导读引用讲义；学生分享版排除答案，分享撤销即时生效。",
    output: "课程导读.md",
    permission: "读取本课程；分享按接收者权限重新校验",
  },
  {
    id: "review",
    title: "持续代码评审",
    employee: "代码审查员",
    initials: "审",
    color: "orange",
    workspace: "静态评审空间",
    brief: "复核 release/next 分支基线",
    input: "虚构仓库 demo/service；冻结版本 4a1c9e2；包含缺陷与修复对照。",
    acceptance:
      "只读静态检索；问题有位置、可达条件、证据和影响，覆盖账本可复算。",
    output: "静态评审报告.md",
    permission: "读取冻结版本；禁止编译、运行、修改源码",
  },
];
export const PHASE_LABEL: Record<Phase, string> = {
  queued: "排队中",
  preparing: "准备中",
  creating_session: "创建会话",
  waiting_model: "等待模型",
  streaming: "输出中",
  validating: "结果校验",
  succeeded: "技术运行完成",
  cooling: "冷却重试",
  cancel_requested: "取消待确认",
  cancelled: "已取消",
};
export const WORK_LABEL: Record<WorkState, string> = {
  assigned: "待开始",
  in_progress: "进行中",
  awaiting_review: "待复核",
  changes_requested: "待修改",
  completed: "已验收",
  cancelled: "已取消",
};
export type DemoRun = {
  id: string;
  attemptIds: string[];
  previousAttempts: {
    id: string;
    sessionId: string | null;
    output: string | null;
    phase: Phase;
  }[];
  phase: Phase;
  sessionId: string | null;
  output: string | null;
  stale: boolean;
  cancelConfirmed?: boolean;
};
export type Work = {
  id: string;
  title: string;
  scenario: ScenarioId;
  state: WorkState;
  runs: DemoRun[];
  approval: "none" | "pending" | "approved" | "denied";
  human: boolean;
  events: string[];
};
export type Model = { works: Work[]; revision: number; notice: string };
export type Action =
  | { type: "create"; scenario: ScenarioId; title: string }
  | { type: "reset" }
  | {
      type:
        | "advance"
        | "approve"
        | "deny"
        | "accept"
        | "return"
        | "revise"
        | "retry"
        | "takeover"
        | "cancel"
        | "confirm_cancel"
        | "stale";
      id: string;
      role: Role;
    };
const runFor = (id: string, phase: Phase): DemoRun => ({
  id,
  phase,
  attemptIds: [`${id}.a1`],
  previousAttempts: [],
  sessionId: phase === "queued" ? null : `${id}.s1`,
  output:
    phase === "succeeded"
      ? `${id}.artifact`
      : phase === "cooling"
        ? `${id}.partial`
        : null,
  stale: false,
});
export function initialModel(): Model {
  return {
    revision: 1,
    notice: "",
    works: [
      {
        id: "demo-w1",
        title: "准备设备选型建议",
        scenario: "sales",
        state: "in_progress",
        runs: [runFor("demo-r1", "validating")],
        approval: "pending",
        human: false,
        events: ["方案草稿已保留；外发动作等待审批。"],
      },
      {
        id: "demo-w2",
        title: "汇总下周岗位计划",
        scenario: "team",
        state: "in_progress",
        runs: [runFor("demo-r2", "waiting_model")],
        approval: "none",
        human: false,
        events: ["会话有事件，最近有效输出尚未产生。"],
      },
      {
        id: "demo-w3",
        title: "整理课程导读与练习",
        scenario: "education",
        state: "awaiting_review",
        runs: [runFor("demo-r3", "succeeded")],
        approval: "none",
        human: false,
        events: ["成果校验完成，等待主管进行业务复核。"],
      },
      {
        id: "demo-w4",
        title: "复核 release/next 分支基线",
        scenario: "review",
        state: "in_progress",
        runs: [runFor("demo-r4", "cooling")],
        approval: "none",
        human: false,
        events: ["模型链路中断；原始输出保留，退避后先对账。"],
      },
    ],
  };
}
export function currentRun(work: Work): DemoRun {
  return work.runs[work.runs.length - 1];
}
const progress: Partial<Record<Phase, Phase>> = {
  queued: "preparing",
  preparing: "creating_session",
  creating_session: "waiting_model",
  waiting_model: "streaming",
  streaming: "validating",
  validating: "succeeded",
};
export function reduceModel(model: Model, action: Action): Model {
  if (action.type === "reset") return initialModel();
  if (action.type === "create") {
    if (!action.title.trim()) return { ...model, notice: "请填写工作名称。" };
    const id = `demo-w${model.works.length + 1}`;
    const work: Work = {
      id,
      title: action.title.trim().slice(0, 100),
      scenario: action.scenario,
      state: "assigned",
      runs: [runFor(`${id}.r1`, "queued")],
      approval: "none",
      human: false,
      events: ["演示工作已创建，进入排队。"],
    };
    return {
      works: [work, ...model.works],
      revision: model.revision + 1,
      notice: "工作已加入演示列表。",
    };
  }
  const index = model.works.findIndex((w) => w.id === action.id);
  if (index < 0) return model;
  const work = structuredClone(model.works[index]);
  const run = currentRun(work);
  let event = "";
  const terminal = work.state === "completed" || work.state === "cancelled";
  if (action.type === "stale") {
    run.stale = !run.stale;
    event = run.stale
      ? "观测已过期；保留最后已知状态，等待对账。"
      : "观测已恢复，状态已对账。";
  } else if (terminal || action.role === "admin")
    return { ...model, notice: "当前角色或工作状态不支持此操作。" };
  else if (action.type === "takeover" && !work.human) {
    work.human = true;
    event = "人工接管请求已记录；当前运行保持原状态。";
  } else if (
    action.type === "approve" &&
    action.role === "supervisor" &&
    work.approval === "pending"
  ) {
    work.approval = "approved";
    event = "外发审批通过；实际动作仍须校验参数、有效期与 E2AG 授权。";
  } else if (
    action.type === "deny" &&
    action.role === "supervisor" &&
    work.approval === "pending"
  ) {
    work.approval = "denied";
    event = "外发审批已拒绝；成果可留在空间内复核。";
  } else if (
    action.type === "accept" &&
    action.role === "supervisor" &&
    work.state === "awaiting_review"
  ) {
    work.state = "completed";
    event = "主管已验收业务成果。";
  } else if (
    action.type === "return" &&
    action.role === "supervisor" &&
    work.state === "awaiting_review"
  ) {
    work.state = "changes_requested";
    event = "主管要求补充来源说明；原成果与运行保留。";
  } else if (action.type === "revise" && work.state === "changes_requested") {
    work.runs.push(runFor(`${work.id}.r${work.runs.length + 1}`, "queued"));
    work.state = "assigned";
    work.approval = "none";
    work.human = false;
    event = "修改要求已建立新一轮运行，原运行与成果保留。";
  } else if (action.type === "retry" && run.phase === "cooling" && !run.stale) {
    run.previousAttempts.push({
      id: run.attemptIds.at(-1)!,
      sessionId: run.sessionId,
      output: run.output,
      phase: run.phase,
    });
    run.output = null;
    run.attemptIds.push(`${run.id}.a${run.attemptIds.length + 1}`);
    run.phase = "preparing";
    run.sessionId = null;
    event =
      "演示对账已确认上一尝试结束，创建新 Attempt；逻辑运行身份保持不变。";
  } else if (
    action.type === "cancel" &&
    !["succeeded", "cancelled", "cancel_requested"].includes(run.phase)
  ) {
    run.phase = "cancel_requested";
    event = "已记录取消请求，等待执行端确认。";
  } else if (
    action.type === "confirm_cancel" &&
    run.phase === "cancel_requested"
  ) {
    run.phase = "cancelled";
    run.cancelConfirmed = true;
    work.state = "cancelled";
    event = "演示执行端已确认取消；部分输出继续保留。";
  } else if (
    action.type === "advance" &&
    progress[run.phase] &&
    !run.stale &&
    work.approval !== "pending"
  ) {
    run.phase = progress[run.phase]!;
    work.state = "in_progress";
    if (run.phase === "waiting_model")
      run.sessionId = `${run.id}.s${run.attemptIds.length}`;
    if (run.phase === "streaming") run.output = `${run.id}.partial`;
    if (run.phase === "succeeded") {
      run.output = `${run.id}.artifact`;
      work.state = "awaiting_review";
    }
    event = `演示运行进入「${PHASE_LABEL[run.phase]}」。`;
  }
  if (!event)
    return {
      ...model,
      notice: "此操作当前不可用，请查看角色、审批或观测状态。",
    };
  work.events.push(event);
  const works = [...model.works];
  works[index] = work;
  return { works, revision: model.revision + 1, notice: event };
}
export type QuerySpec = {
  scenario: ScenarioId | "all";
  state: WorkState | "all";
  search: string;
};
export function selectWorks(model: Model, query: QuerySpec): Work[] {
  return model.works.filter(
    (w) =>
      (query.scenario === "all" || w.scenario === query.scenario) &&
      (query.state === "all" || w.state === query.state) &&
      `${w.title} ${w.id}`
        .toLowerCase()
        .includes(query.search.trim().toLowerCase()),
  );
}
export function exportSnapshot(model: Model, query: QuerySpec) {
  const works = selectWorks(model, query);
  return {
    synthetic: true,
    query_spec: query,
    watermark: `demo-v${model.revision}`,
    total: works.length,
    completed: works.filter((w) => w.state === "completed").length,
    contributors: SCENARIOS.filter((s) =>
      works.some((w) => w.scenario === s.id),
    ).map((s) => ({
      employee: s.employee,
      work_count: works.filter((w) => w.scenario === s.id).length,
    })),
    works,
  };
}
