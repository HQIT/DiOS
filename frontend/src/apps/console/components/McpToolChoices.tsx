/** 按已绑定服务读取目录，选中项仍由 Agent 的治理名单控制。 */
import { useState } from "react";
import { api } from "../../../api/os";
import type { McpToolCatalog } from "../../../types";

interface Props {
  serverIds: string[];
  selected: string[];
  onToggle: (name: string, checked: boolean) => void;
}

/** 只在用户请求时读取远程目录，部分服务失败时保留成功的目录。 */
export default function McpToolChoices({ serverIds, selected, onToggle }: Props) {
  const [results, setResults] = useState<PromiseSettledResult<McpToolCatalog>[] | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  async function loadTools() {
    setIsLoading(true);
    try {
      setResults(await Promise.allSettled(serverIds.map((serverId) => api.getMcpToolCatalog(serverId))));
    } finally {
      setIsLoading(false);
    }
  }

  const failures = results?.filter((result) => result.status === "rejected").length ?? 0;

  return (
    <div className="agent-mcp-tools">
      <div>
        <button type="button" className="btn-sm btn-secondary" onClick={loadTools} disabled={isLoading}>
          {isLoading ? "正在读取…" : "读取 MCP 工具列表"}
        </button>
      </div>
      {selected.some((name) => /[*?]|\[/.test(name)) && (
        <p className="agent-capabilities-hint">
          勾选项只表示精确工具名。已有的通配规则继续生效，可在下方名单或高级 JSON 中移除。
        </p>
      )}
      {failures > 0 && (
        <p role="status" className="agent-capabilities-hint">
          {failures} 个已绑定服务的目录读取失败。请在 Registry 检查连接和凭据，也可以手动添加工具名。
        </p>
      )}
      {results?.map((result, index) => result.status === "fulfilled" && (
        <div className="agent-mcp-tools" key={serverIds[index]}>
          <strong>{result.value.server_name}</strong>
          {result.value.tools.length === 0 && <p className="agent-capabilities-hint">该服务未返回可选工具。</p>}
          {result.value.tools.map((tool) => (
            <label className="agent-tool-option" key={tool.name}>
              <input type="checkbox" checked={selected.includes(tool.name)}
                onChange={(event) => onToggle(tool.name, event.target.checked)} />
              <span>
                <strong>{tool.title || tool.name}</strong>
                <small>{tool.title && tool.title !== tool.name ? `${tool.name} · ` : ""}{tool.description}</small>
              </span>
            </label>
          ))}
        </div>
      ))}
    </div>
  );
}
