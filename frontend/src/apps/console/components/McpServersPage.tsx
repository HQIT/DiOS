import { useEffect, useMemo, useState } from "react";
import type { McpServer } from "../../../types";
import { api } from "../../../api/os";
import Drawer from "../../../components/Drawer";

interface RegistryServer {
  name: string;
  title?: string;
  description: string;
  version: string;
  command: string;
  args: string[];
  env_hints: Record<string, string>;
  transport: string;
  url?: string;
  header_hints?: Record<string, string>;
  registry_type?: string;
  package?: string;
  repository_url?: string;
}

const REMOTE_TRANSPORTS = new Set(["streamable_http", "streamable-http", "http", "sse"]);
const normalizeTransport = (transport: string) => transport === "streamable-http" || transport === "http" ? "streamable_http" : transport;

export default function McpServersPage() {
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [mcpEdit, setMcpEdit] = useState<Partial<McpServer> | null>(null);
  const [envPairs, setEnvPairs] = useState<{ key: string; value: string; hint?: string }[]>([]);
  const [headerPairs, setHeaderPairs] = useState<{ key: string; value: string; hint?: string }[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<RegistryServer[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchDone, setSearchDone] = useState(false);
  const [searchError, setSearchError] = useState("");

  const load = () => api.listMcpServers().then(setMcpServers);
  useEffect(() => { load(); }, []);

  const remoteCount = useMemo(
    () => mcpServers.filter((server) => REMOTE_TRANSPORTS.has((server.transport || "stdio").toLowerCase())).length,
    [mcpServers],
  );

  const envToList = (env: Record<string, string>) =>
    Object.entries(env || {}).map(([key, value]) => ({ key, value, hint: "" }));
  const listToEnv = (pairs: { key: string; value: string }[]) =>
    Object.fromEntries(pairs.filter((pair) => pair.key.trim()).map((pair) => [pair.key, pair.value]));

  const openEdit = (server?: McpServer) => {
    if (server) {
      setMcpEdit({ ...server });
      setEnvPairs(envToList(server.env || {}));
      setHeaderPairs(Object.entries(server.headers || {}).map(([key, value]) => ({ key, value, hint: "" })));
    } else {
      setMcpEdit({ name: "", transport: "streamable_http", url: "", headers: {}, command: "", args: [], env: {} });
      setEnvPairs([]);
      setHeaderPairs([]);
    }
  };

  const isRemote = REMOTE_TRANSPORTS.has((mcpEdit?.transport || "stdio").toLowerCase());

  const save = async () => {
    if (!mcpEdit?.name?.trim()) return;
    const transport = normalizeTransport((mcpEdit.transport || "stdio").trim());
    if (REMOTE_TRANSPORTS.has(transport.toLowerCase())) {
      if (!mcpEdit.url?.trim()) return;
    } else if (!mcpEdit.command?.trim()) return;

    const payload = {
      name: mcpEdit.name,
      transport,
      url: mcpEdit.url || "",
      headers: listToEnv(headerPairs),
      command: mcpEdit.command || "",
      args: mcpEdit.args ?? [],
      env: listToEnv(envPairs),
    };
    if (mcpEdit.id) await api.updateMcpServer(mcpEdit.id, payload);
    else await api.createMcpServer(payload);
    setMcpEdit(null);
    load();
  };

  const doSearch = async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    setSearchDone(false);
    setSearchError("");
    try {
      const response = await api.searchMcpRegistry(searchQuery.trim(), 20);
      setSearchResults(response.servers);
    } catch (error) {
      setSearchResults([]);
      setSearchError(error instanceof Error ? error.message : "Registry 暂时不可用");
    } finally {
      setSearching(false);
      setSearchDone(true);
    }
  };

  const addFromRegistry = (server: RegistryServer) => {
    const remote = !!server.url;
    const envHints = server.env_hints || {};
    const headerHints = server.header_hints || {};
    setMcpEdit({
      name: server.title || server.name.split("/").pop() || server.name,
      transport: remote ? normalizeTransport(server.transport || "streamable_http") : "stdio",
      url: server.url || "",
      headers: Object.fromEntries(Object.keys(headerHints).map((key) => [key, ""])),
      command: server.command,
      args: server.args || [],
      env: Object.fromEntries(Object.keys(envHints).map((key) => [key, ""])),
    });
    setEnvPairs(Object.entries(envHints).map(([key, hint]) => ({ key, value: "", hint })));
    setHeaderPairs(Object.entries(headerHints).map(([key, hint]) => ({ key, value: "", hint })));
  };

  const isRegistered = (server: RegistryServer) => {
    const shortName = server.name.split("/").pop()?.toLowerCase();
    return mcpServers.some((item) => item.name.toLowerCase() === shortName || (!!server.url && item.url === server.url));
  };

  const updateArg = (index: number, value: string) => {
    setMcpEdit((previous) => {
      if (!previous) return previous;
      const args = [...(previous.args || [])];
      args[index] = value;
      return { ...previous, args };
    });
  };

  const updateEnvPair = (index: number, field: "key" | "value", value: string) =>
    setEnvPairs((previous) => previous.map((pair, current) => current === index ? { ...pair, [field]: value } : pair));
  const updateHeaderPair = (index: number, field: "key" | "value", value: string) =>
    setHeaderPairs((previous) => previous.map((pair, current) => current === index ? { ...pair, [field]: value } : pair));

  const metaLine = (server: McpServer) => {
    const transport = (server.transport || "stdio").toLowerCase();
    return REMOTE_TRANSPORTS.has(transport) || server.url
      ? `${server.transport || "streamable_http"} · ${server.url}`
      : `${server.transport || "stdio"} · ${server.command} ${(server.args || []).join(" ")}`;
  };

  return (
    <div className="registry-page">
      <header className="registry-page-header">
        <div>
          <div className="registry-title-line">
            <h3>MCP Servers</h3>
            <span className="registry-standard-badge">Official Registry</span>
            <span className="registry-standard-badge preview">Preview</span>
          </div>
          <p>注册对外工具和数据服务；真正调用前仍需分配给 Agent，并由 E2AG 按任务授权。</p>
        </div>
        <div className="registry-kpis">
          <div><strong>{mcpServers.length}</strong><span>已注册</span></div>
          <div><strong>{remoteCount}</strong><span>远程服务</span></div>
          <div><strong>{mcpServers.length - remoteCount}</strong><span>本地进程</span></div>
        </div>
      </header>

      <section className="registry-block">
        <div className="registry-section-heading">
          <div>
            <h4>发现 MCP Server</h4>
            <p>数据来自 Official MCP Registry；它提供可验证的发布身份和安装元数据，不代表 DiOS 已授权。</p>
          </div>
          <span className="registry-source-pill">registry.modelcontextprotocol.io</span>
        </div>
        <div className="registry-search-row">
          <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="按名称或能力搜索，例如 github、database" onKeyDown={(event) => event.key === "Enter" && doSearch()} />
          <button className="btn-sm" onClick={doSearch} disabled={searching || !searchQuery.trim()}>{searching ? "搜索中…" : "搜索 Registry"}</button>
        </div>
        {searchError && <p className="registry-error">{searchError}</p>}
        {searchResults.length > 0 && (
          <div className="registry-results standardized">
            {searchResults.map((server) => {
              const registered = isRegistered(server);
              const registrable = !!server.url || !!server.command;
              return (
                <article key={`${server.name}@${server.version}`} className="registry-result-item">
                  <div className="registry-result-info">
                    <div className="registry-result-title">
                      <span className="registry-result-name">{server.title || server.name}</span>
                      {server.version && <span className="registry-version">v{server.version}</span>}
                      <span className={`transport-badge ${server.url ? "transport-remote" : "transport-stdio"}`}>{server.url ? "REMOTE" : (server.registry_type || "PACKAGE").toUpperCase()}</span>
                    </div>
                    <span className="registry-result-desc">{server.description || "无描述"}</span>
                    <span className="registry-result-cmd">{server.url || [server.command, ...(server.args || [])].filter(Boolean).join(" ")}</span>
                  </div>
                  <button className={registered || !registrable ? "btn-sm btn-secondary" : "btn-sm"} onClick={() => addFromRegistry(server)} disabled={registered || !registrable}>{registered ? "已注册" : registrable ? "注册" : "需手动配置"}</button>
                </article>
              );
            })}
          </div>
        )}
        {searchDone && searchResults.length === 0 && !searchError && <div className="registry-inline-empty">没有匹配的 MCP Server</div>}
      </section>

      <section className="registry-block">
        <div className="registry-section-heading">
          <div><h4>已注册 MCP Servers</h4><p>远程服务适合独立系统；stdio 仅用于本机受控进程。</p></div>
          <button className="btn-sm btn-secondary" onClick={() => openEdit()}>手动注册</button>
        </div>
        {mcpServers.length === 0 ? (
          <div className="registry-inline-empty">尚未注册 MCP Server，可从官方 Registry 搜索或手动添加。</div>
        ) : (
          <div className="registry-card-grid">
            {mcpServers.map((server) => {
              const remote = REMOTE_TRANSPORTS.has((server.transport || "stdio").toLowerCase()) || !!server.url;
              return (
                <article key={server.id} className="registry-resource-card">
                  <div className="registry-resource-head">
                    <span className="entity-card-name">{server.name}</span>
                    <span className={`transport-badge ${remote ? "transport-remote" : "transport-stdio"}`}>{remote ? "REMOTE" : "STDIO"}</span>
                  </div>
                  <div className="registry-resource-meta mono">{metaLine(server)}</div>
                  <div className="registry-card-footer">
                    <span>{Object.keys(remote ? server.headers || {} : server.env || {}).length} 个配置项</span>
                    <div><button className="btn-sm btn-secondary" onClick={() => openEdit(server)}>编辑</button><button className="btn-sm btn-danger" onClick={async () => { await api.deleteMcpServer(server.id); load(); }}>删除</button></div>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <Drawer open={!!mcpEdit} title={mcpEdit?.id ? "编辑 MCP Server" : "注册 MCP Server"} onClose={() => setMcpEdit(null)}>
        {mcpEdit && (
          <div className="drawer-form">
            <label>Name</label>
            <input value={mcpEdit.name || ""} onChange={(event) => setMcpEdit({ ...mcpEdit, name: event.target.value })} placeholder="e.g. git-perf" />
            <label>Transport</label>
            <select value={mcpEdit.transport || "streamable_http"} onChange={(event) => setMcpEdit({ ...mcpEdit, transport: event.target.value })}>
              <option value="streamable_http">streamable_http（远程，推荐）</option>
              <option value="sse">sse（远程，兼容）</option>
              <option value="stdio">stdio（本地进程）</option>
            </select>
            {isRemote ? (
              <>
                <label>URL</label>
                <input value={mcpEdit.url || ""} onChange={(event) => setMcpEdit({ ...mcpEdit, url: event.target.value })} placeholder="https://example.com/mcp" />
                <label>Headers（敏感值不会出现在 Registry 搜索结果中）</label>
                {headerPairs.map((pair, index) => (
                  <div key={`${pair.key}-${index}`} className="registry-field-row">
                    <input value={pair.key} onChange={(event) => updateHeaderPair(index, "key", event.target.value)} placeholder="Authorization" />
                    <input value={pair.value} onChange={(event) => updateHeaderPair(index, "value", event.target.value)} placeholder={pair.hint || "Bearer …"} />
                    <button className="btn-sm btn-danger" onClick={() => setHeaderPairs((previous) => previous.filter((_, current) => current !== index))}>×</button>
                  </div>
                ))}
                <button className="btn-sm btn-secondary" onClick={() => setHeaderPairs((previous) => [...previous, { key: "", value: "", hint: "" }])}>+ Header</button>
              </>
            ) : (
              <>
                <label>Command</label>
                <input value={mcpEdit.command || ""} onChange={(event) => setMcpEdit({ ...mcpEdit, command: event.target.value })} placeholder="npx / uvx / docker" />
                <label>Args</label>
                {(mcpEdit.args || []).map((arg, index) => (
                  <div key={index} className="registry-field-row single">
                    <input value={arg} onChange={(event) => updateArg(index, event.target.value)} placeholder={`arg ${index + 1}`} />
                    <button className="btn-sm btn-danger" onClick={() => setMcpEdit((previous) => previous ? { ...previous, args: (previous.args || []).filter((_, current) => current !== index) } : previous)}>×</button>
                  </div>
                ))}
                <button className="btn-sm btn-secondary" onClick={() => setMcpEdit((previous) => previous ? { ...previous, args: [...(previous.args || []), ""] } : previous)}>+ Arg</button>
                <label>Environment</label>
                {envPairs.map((pair, index) => (
                  <div key={`${pair.key}-${index}`}>
                    <div className="registry-field-row">
                      <input value={pair.key} onChange={(event) => updateEnvPair(index, "key", event.target.value)} placeholder="KEY" />
                      <input value={pair.value} onChange={(event) => updateEnvPair(index, "value", event.target.value)} placeholder={pair.hint || "VALUE"} />
                      <button className="btn-sm btn-danger" onClick={() => setEnvPairs((previous) => previous.filter((_, current) => current !== index))}>×</button>
                    </div>
                    {pair.hint && !pair.value && <span className="registry-field-hint">{pair.hint}</span>}
                  </div>
                ))}
                <button className="btn-sm btn-secondary" onClick={() => setEnvPairs((previous) => [...previous, { key: "", value: "" }])}>+ Environment</button>
              </>
            )}
            <div className="drawer-actions"><button onClick={save}>保存</button><button className="btn-secondary" onClick={() => setMcpEdit(null)}>取消</button></div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
