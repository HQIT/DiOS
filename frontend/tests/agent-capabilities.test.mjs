// 验证表单和 JSON 共用的配置契约，覆盖默认值和手写字段。
import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/apps/console/components/agentCapabilities.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { parseCapabilities, withAllowedMcpTools, withToolSelection } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

test("默认配置保留高级字段并省略工具名单", () => {
  const draft = { diagent_tools: ["mcp"], subagents: [{ name: "reviewer" }] };

  assert.deepEqual(withToolSelection(draft, undefined), {
    subagents: [{ name: "reviewer" }],
  });
});

test("清空自定义选项保存为空名单", () => {
  assert.deepEqual(withToolSelection({}, []), { diagent_tools: [] });
});

test("合法 JSON 中的工具名单可以回显到表单", () => {
  assert.deepEqual(parseCapabilities('{"diagent_tools":["read","write"]}'), {
    value: { diagent_tools: ["read", "write"] },
  });
});

test("不完整的 JSON 和类型错误的工具名单不可保存", () => {
  assert.equal(parseCapabilities('{"diagent_tools":').error, "请输入合法的 JSON 对象");
  assert.equal(parseCapabilities('{"diagent_tools":"read"}').error, "diagent_tools 必须是字符串数组");
});

test("修改 MCP 工具名单保留其他授权规则", () => {
  const draft = { diagent_tools: ["mcp"], governance: { allowed_event_sources: ["manual/research"] } };

  assert.deepEqual(withAllowedMcpTools(draft, ["search", "fetch"]), {
    diagent_tools: ["mcp"],
    governance: { allowed_event_sources: ["manual/research"], allowed_tools: ["search", "fetch"] },
  });
});

test("已有 Agent 可以保留未设置的治理配置", () => {
  assert.deepEqual(parseCapabilities('{"governance":null}'), { value: { governance: null } });
});
