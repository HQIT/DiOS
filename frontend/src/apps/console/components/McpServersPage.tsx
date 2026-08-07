import { useEffect, useState } from "react";
import type { McpServer } from "../../../types";
import { api } from "../../../api/os";
import Drawer from "../../../components/Drawer";

interface RegistryServer {
  name: string;
  description: string;
  version: string;
  command: string;
  args: string[];
  env_hints: Record<string, string>;
  transport: string;
}

const REMOTE_TRANSPORTS = new Set(["streamable_http", "http", "sse"]);

export default function McpServersPage() {
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [mcpEdit, setMcpEdit] = useState<Partial<McpServer> | null>(null);
  const [envPairs, setEnvPairs] = useState<{ key: string; value: string; hint?: string }[]>([]);
  const [headerPairs, setHeaderPairs] = useState<{ key: string; value: string }[]>([]);

  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<RegistryServer[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchDone, setSearchDone] = useState(false);

  const load = () => api.listMcpServers().then(setMcpServers);
  useEffect(() => { load(); }, []);

  const envToList = (env: Record<string, string>) =>
    Object.entries(env || {}).map(([key, value]) => ({ key, value, hint: "" }));
  const listToEnv = (pairs: { key: string; value: string }[]) =>
    Object.fromEntries(pairs.filter((p) => p.key.trim()).map((p) => [p.key, p.value]));

  const openEdit = (s?: McpServer) => {
    if (s) {
      setMcpEdit({ ...s });
      setEnvPairs(envToList(s.env || {}));
      setHeaderPairs(Object.entries(s.headers || {}).map(([key, value]) => ({ key, value })));
    } else {
      setMcpEdit({
        name: "",
        transport: "streamable_http",
        url: "",
        headers: {},
        command: "",
        args: [],
        env: {},
      });
      setEnvPairs([]);
      setHeaderPairs([]);
    }
  };

  const isRemote = REMOTE_TRANSPORTS.has((mcpEdit?.transport || "stdio").toLowerCase());

  const save = async () => {
    if (!mcpEdit?.name?.trim()) return;
    const transport = (mcpEdit.transport || "stdio").trim();
    if (REMOTE_TRANSPORTS.has(transport.toLowerCase())) {
      if (!mcpEdit.url?.trim()) return;
    } else if (!mcpEdit.command?.trim()) {
      return;
    }
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
    try {
      const res = await api.searchMcpRegistry(searchQuery.trim(), 20);
      setSearchResults(res.servers);
    } catch {
      setSearchResults([]);
    } finally {
      setSearching(false);
      setSearchDone(true);
    }
  };

  const addFromRegistry = (srv: RegistryServer) => {
    const hints = srv.env_hints || {};
    setMcpEdit({
      name: srv.name.split("/").pop() || srv.name,
      transport: srv.transport || "stdio",
      url: "",
      headers: {},
      command: srv.command,
      args: srv.args || [],
      env: Object.fromEntries(Object.entries(hints).map(([k]) => [k, ""])),
    });
    setEnvPairs(Object.entries(hints).map(([key, desc]) => ({ key, value: "", hint: desc })));
    setHeaderPairs([]);
  };

  const updateArg = (idx: number, val: string) => {
    setMcpEdit((prev) => {
      if (!prev) return prev;
      const args = [...(prev.args || [])];
      args[idx] = val;
      return { ...prev, args };
    });
  };
  const removeArg = (idx: number) => {
    setMcpEdit((prev) => {
      if (!prev) return prev;
      return { ...prev, args: (prev.args || []).filter((_, i) => i !== idx) };
    });
  };
  const addArg = () => {
    setMcpEdit((prev) => prev ? { ...prev, args: [...(prev.args || []), ""] } : prev);
  };

  const updateEnvPair = (idx: number, field: "key" | "value", val: string) => {
    setEnvPairs((prev) => prev.map((p, i) => i === idx ? { ...p, [field]: val } : p));
  };
  const removeEnvPair = (idx: number) => {
    setEnvPairs((prev) => prev.filter((_, i) => i !== idx));
  };
  const addEnvPair = () => {
    setEnvPairs((prev) => [...prev, { key: "", value: "" }]);
  };

  const updateHeaderPair = (idx: number, field: "key" | "value", val: string) => {
    setHeaderPairs((prev) => prev.map((p, i) => i === idx ? { ...p, [field]: val } : p));
  };

  const metaLine = (s: McpServer) => {
    const t = (s.transport || "stdio").toLowerCase();
    if (REMOTE_TRANSPORTS.has(t) || s.url) {
      return `${s.transport || "streamable_http"} · ${s.url}`;
    }
    return `${s.transport || "stdio"} · ${s.command} ${(s.args || []).join(" ")}`;
  };

  return (
    <div className="panel">
      <p className="text-muted" style={{ marginBottom: 12 }}>
        外部独立系统请用 <b>远程 MCP</b>（streamable_http + URL）。stdio 仅适合本地小工具。
      </p>

      <div className="registry-search">
        <h4 className="catalog-section-title">Search MCP Registry</h4>
        <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
          <input
            style={{ flex: 1 }}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search registry…"
            onKeyDown={(e) => e.key === "Enter" && doSearch()}
          />
          <button className="btn-sm" onClick={doSearch} disabled={searching}>
            {searching ? "…" : "Search"}
          </button>
        </div>
        {searchResults.map((srv) => (
          <div key={srv.name} className="registry-result" style={{ marginBottom: 8 }}>
            <div className="registry-result-name">{srv.name}</div>
            <div className="text-muted" style={{ fontSize: 12 }}>{srv.description}</div>
            <button className="btn-sm btn-secondary" onClick={() => addFromRegistry(srv)}>Add</button>
          </div>
        ))}
        {searchDone && searchResults.length === 0 && (
          <p className="text-muted">No matching MCP servers found</p>
        )}
      </div>

      <h4 className="catalog-section-title" style={{ marginTop: 20 }}>Configured MCP Servers</h4>
      <div className="card-grid">
        {mcpServers.map((s) => (
          <div key={s.id} className="entity-card">
            <div className="entity-card-header">
              <span className="entity-card-name">{s.name}</span>
            </div>
            <div className="entity-card-meta">
              <span className="mono">{metaLine(s)}</span>
            </div>
            <div className="entity-card-actions">
              <button className="btn-sm btn-secondary" onClick={() => openEdit(s)}>Edit</button>
              <button className="btn-sm btn-danger" onClick={async () => { await api.deleteMcpServer(s.id); load(); }}>Delete</button>
            </div>
          </div>
        ))}
        <div className="entity-card add-card" onClick={() => openEdit()}>
          <span className="add-card-icon">+</span>
          <span className="add-card-label">Add MCP Server</span>
        </div>
      </div>

      <Drawer open={!!mcpEdit} title={mcpEdit?.id ? "Edit MCP Server" : "Add MCP Server"} onClose={() => setMcpEdit(null)}>
        {mcpEdit && (
          <div className="drawer-form">
            <label>Name</label>
            <input value={mcpEdit.name || ""} onChange={(e) => setMcpEdit({ ...mcpEdit, name: e.target.value })} placeholder="e.g. git-perf" />

            <label>Transport</label>
            <select
              value={mcpEdit.transport || "streamable_http"}
              onChange={(e) => setMcpEdit({ ...mcpEdit, transport: e.target.value })}
            >
              <option value="streamable_http">streamable_http（远程，推荐）</option>
              <option value="sse">sse（远程，旧）</option>
              <option value="stdio">stdio（本地进程）</option>
            </select>

            {isRemote ? (
              <>
                <label>URL</label>
                <input
                  value={mcpEdit.url || ""}
                  onChange={(e) => setMcpEdit({ ...mcpEdit, url: e.target.value })}
                  placeholder="http://host.docker.internal:8090/mcp"
                />
                <label>Headers（可选）</label>
                {headerPairs.map((p, i) => (
                  <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4 }}>
                    <input style={{ flex: 1 }} value={p.key} onChange={(e) => updateHeaderPair(i, "key", e.target.value)} placeholder="Authorization" />
                    <input style={{ flex: 1 }} value={p.value} onChange={(e) => updateHeaderPair(i, "value", e.target.value)} placeholder="Bearer …" />
                    <button className="btn-sm btn-danger" onClick={() => setHeaderPairs((prev) => prev.filter((_, j) => j !== i))}>x</button>
                  </div>
                ))}
                <button className="btn-sm btn-secondary" onClick={() => setHeaderPairs((p) => [...p, { key: "", value: "" }])} style={{ marginBottom: 8 }}>+ Header</button>
              </>
            ) : (
              <>
                <label>Command</label>
                <input value={mcpEdit.command || ""} onChange={(e) => setMcpEdit({ ...mcpEdit, command: e.target.value })} placeholder="npx / uvx / python" />

                <label>Args</label>
                {(mcpEdit.args || []).map((arg, i) => (
                  <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4 }}>
                    <input style={{ flex: 1 }} value={arg} onChange={(e) => updateArg(i, e.target.value)} placeholder={`arg ${i + 1}`} />
                    <button className="btn-sm btn-danger" onClick={() => removeArg(i)}>x</button>
                  </div>
                ))}
                <button className="btn-sm btn-secondary" onClick={addArg} style={{ marginBottom: 8 }}>+ Add Arg</button>

                <label>Env</label>
                {envPairs.map((p, i) => (
                  <div key={i} style={{ display: "flex", flexDirection: "column", gap: 2, marginBottom: 6 }}>
                    <div style={{ display: "flex", gap: 4 }}>
                      <input style={{ flex: 1 }} value={p.key} onChange={(e) => updateEnvPair(i, "key", e.target.value)} placeholder="KEY" />
                      <input style={{ flex: 1 }} value={p.value} onChange={(e) => updateEnvPair(i, "value", e.target.value)} placeholder={p.hint || "VALUE"} />
                      <button className="btn-sm btn-danger" onClick={() => removeEnvPair(i)}>x</button>
                    </div>
                    {p.hint && !p.value && <span style={{ fontSize: 11, color: "var(--text-secondary)", paddingLeft: 4 }}>{p.hint}</span>}
                  </div>
                ))}
                <button className="btn-sm btn-secondary" onClick={addEnvPair} style={{ marginBottom: 8 }}>+ Add Env</button>
              </>
            )}

            <div className="drawer-actions">
              <button onClick={save}>保存</button>
              <button className="btn-secondary" onClick={() => setMcpEdit(null)}>取消</button>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
