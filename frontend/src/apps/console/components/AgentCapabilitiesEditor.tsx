/** 在同一个配置草稿上提供常用工具表单和完整 JSON 编辑。 */
import { useId, useState } from "react";
import type { Agent } from "../../../types";
import { getAllowedMcpTools, withAllowedMcpTools, withToolSelection } from "./agentCapabilities";
import type { AgentCapabilities } from "./agentCapabilities";
import McpToolChoices from "./McpToolChoices";

const toolOptions = [
  { id: "read", label: "读取文件", description: "读取任务工作区中的文件", taskOnly: true },
  { id: "write", label: "写入文件", description: "在任务工作区中创建文件，例如调研报告", taskOnly: true },
  { id: "edit", label: "编辑文件", description: "修改任务工作区中的已有文件", taskOnly: true },
  { id: "skill", label: "使用 Skill", description: "加载已分配给 Agent 的操作指南" },
  { id: "mcp", label: "调用 MCP 工具", description: "使用已绑定的外部服务，例如网页搜索" },
  { id: "shell", label: "运行 Shell", description: "在 Agent 容器内执行命令" },
  { id: "publish_event", label: "发布事件", description: "向 DiOS 发送部署已授权的事件" },
];

interface Props {
  agent: Partial<Agent>;
  value: AgentCapabilities;
  text: string;
  error?: string;
  onTextChange: (text: string) => void;
}

/** 以 Agent 编辑页的草稿为准，JSON 无效时保留表单的最后一次合法状态。 */
export default function AgentCapabilitiesEditor({ agent, value, text, error, onTextChange }: Props) {
  const id = useId();
  const [mcpToolInput, setMcpToolInput] = useState("");
  const selected = value.diagent_tools;
  const allowedTools = getAllowedMcpTools(value);
  const isDefault = selected === undefined;
  const isTask = agent.mode === "task";
  const hasSkills = Boolean(agent.skills?.length);
  const mcpServerIds = agent.mcp_server_ids ?? [];
  const update = (next: AgentCapabilities) => onTextChange(JSON.stringify(next, null, 2));

  function toggleMcpTool(name: string, checked: boolean) {
    const tools = checked ? [...new Set([...allowedTools, name])] : allowedTools.filter((item) => item !== name);
    update(withAllowedMcpTools(value, tools));
  }

  function addMcpTool() {
    const name = mcpToolInput.trim();
    if (!name || allowedTools.includes(name)) return;
    toggleMcpTool(name, true);
    setMcpToolInput("");
  }

  return (
    <section className="agent-capabilities" aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`}>能力配置</h3>
      <label htmlFor={`${id}-mode`}>工具配置方式</label>
      <select
        id={`${id}-mode`}
        value={isDefault ? "default" : "custom"}
        disabled={Boolean(error)}
        onChange={(event) => update(withToolSelection(value, event.target.value === "default" ? undefined : []))}
      >
        <option value="default">使用默认配置</option>
        <option value="custom">自定义工具</option>
      </select>
      {isDefault ? (
        <p className="agent-capabilities-hint">JSON 省略 <code>diagent_tools</code> 时，沿用 {isTask ? "Task" : "Service"} 的部署默认工具。</p>
      ) : (
        <fieldset className="agent-tool-options" disabled={Boolean(error)}>
          <legend>可选工具</legend>
          {toolOptions.filter((tool) => isTask || !tool.taskOnly || selected.includes(tool.id)).map((tool) => (
            <label className="agent-tool-option" key={tool.id}>
              <input
                type="checkbox"
                checked={selected.includes(tool.id)}
                onChange={(event) => update(withToolSelection(value, event.target.checked
                  ? [...selected, tool.id]
                  : selected.filter((item) => item !== tool.id)))}
              />
              <span><strong>{tool.label}</strong><small>{!isTask && tool.taskOnly ? "仅 Task 支持，请取消勾选或切换模式" : tool.description}</small></span>
            </label>
          ))}
          {isTask && <p className="agent-capabilities-hint">Task 的文件工具和 Shell 需要部署启用受管模型代理和凭据续期。</p>}
          {!isTask && <p className="agent-capabilities-hint">Service 自定义工具需要部署启用受管推理。</p>}
          {selected.length === 0 && <p className="agent-capabilities-hint">当前关闭所有常规工具。</p>}
          {selected.includes("skill") && !hasSkills && (
            <p className="agent-capabilities-hint">请先在 Agent 卡片的 Skills 中分配 Skill。</p>
          )}
          {selected.includes("mcp") && (
            <div className="agent-mcp-tools">
              <label htmlFor={`${id}-mcp`}>允许调用的 MCP 工具</label>
              {mcpServerIds.length ? (
                <>
                  <p className="agent-capabilities-hint">按工具名授权，名单适用于所有已绑定服务。Task 需要 E2AG 授权和 Streamable HTTP 服务。</p>
                  <McpToolChoices key={mcpServerIds.join(",")} serverIds={mcpServerIds} selected={allowedTools} onToggle={toggleMcpTool} />
                </>
              ) : (
                <p className="agent-capabilities-hint">请先在 Agent 卡片的 MCP 中绑定 Streamable HTTP 服务。</p>
              )}
              <div className="agent-mcp-tool-entry">
                <input
                  id={`${id}-mcp`}
                  value={mcpToolInput}
                  placeholder="手动添加工具名，例如 search"
                  onChange={(event) => setMcpToolInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                      event.preventDefault();
                      addMcpTool();
                    }
                  }}
                />
                <button type="button" className="btn-secondary" onClick={addMcpTool} disabled={!mcpToolInput.trim() || allowedTools.includes(mcpToolInput.trim())}>添加</button>
              </div>
              {allowedTools.length > 0 && (
                <ul className="agent-mcp-tool-list">
                  {[...new Set(allowedTools)].map((name) => (
                    <li key={name}>
                      <code>{name}</code>
                      <button type="button" className="btn-sm btn-secondary" aria-label={`移除 ${name}`}
                        onClick={() => toggleMcpTool(name, false)}>移除</button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </fieldset>
      )}
      <div className="agent-capabilities-json">
        <label htmlFor={`${id}-json`}>能力配置 JSON</label>
        <textarea
          id={`${id}-json`}
          aria-invalid={Boolean(error)}
          aria-describedby={error ? `${id}-error` : undefined}
          rows={9}
          spellCheck={false}
          value={text}
          onChange={(event) => onTextChange(event.target.value)}
        />
      </div>
      {error && <p className="agent-capabilities-error" id={`${id}-error`} role="alert">{error}</p>}
    </section>
  );
}
