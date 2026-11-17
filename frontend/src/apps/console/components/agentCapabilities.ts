/** 表单与 JSON 共用配置对象，单项编辑保留其他配置字段。 */
export interface AgentCapabilities extends Record<string, unknown> {
  diagent_tools?: string[];
}

type ParsedCapabilities = { value: AgentCapabilities; error?: never } | { value?: never; error: string };

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

/** 校验表单需要读取的字段，其他字段交由保存接口校验。 */
export function parseCapabilities(text: string): ParsedCapabilities {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { error: "请输入合法的 JSON 对象" };
  }
  if (!isObject(value)) return { error: "请输入合法的 JSON 对象" };
  if (Object.hasOwn(value, "diagent_tools") && !isStringList(value.diagent_tools)) {
    return { error: "diagent_tools 必须是字符串数组" };
  }
  return { value };
}

/** 省略工具名单表示继承默认值，空数组表示显式关闭常规工具。 */
export function withToolSelection(value: AgentCapabilities, tools: string[] | undefined): AgentCapabilities {
  if (tools !== undefined) return { ...value, diagent_tools: tools };
  const next = { ...value };
  delete next.diagent_tools;
  return next;
}

/** 更新 MCP 名单并保留同层的事件与审批规则。 */
export function withAllowedMcpTools(value: AgentCapabilities, tools: string[]): AgentCapabilities {
  const governance = isObject(value.governance) ? value.governance : {};
  return { ...value, governance: { ...governance, allowed_tools: tools } };
}

/** 只向表单暴露有效名单，保留 JSON 中的存量值供用户修订。 */
export function getAllowedMcpTools(value: AgentCapabilities): string[] {
  const tools = isObject(value.governance) ? value.governance.allowed_tools : undefined;
  return isStringList(tools) ? tools : [];
}
