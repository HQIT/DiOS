import { useEffect, useState } from "react";
import type { Agent, Team } from "../types";
import { api } from "../api/client";
import Drawer from "./Drawer";
import ModelSelect from "./ModelSelect";

interface Props {
  team: Team;
}

const EMPTY: Partial<Agent> = { name: "", role: "sub", description: "", model: "", system_prompt: "", skills: [], mcp_config_path: "" };

export default function AgentList({ team }: Props) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [editing, setEditing] = useState<Partial<Agent> | null>(null);
  const [editId, setEditId] = useState<string | null>(null);

  const load = () => api.listAgents(team.id).then(setAgents);
  useEffect(() => { load(); }, [team.id]);

  const startEdit = (a?: Agent) => {
    if (a) {
      setEditId(a.id);
      setEditing({ ...a });
    } else {
      setEditId(null);
      setEditing({ ...EMPTY });
    }
  };

  const save = async () => {
    if (!editing?.name?.trim()) return;
    const data = {
      name: editing.name,
      role: editing.role || "sub",
      description: editing.description || "",
      model: editing.model || "",
      system_prompt: editing.system_prompt || "",
      skills: editing.skills || [],
      mcp_config_path: editing.mcp_config_path || "",
    };
    if (editId) {
      await api.updateAgent(team.id, editId, data);
    } else {
      await api.createAgent(team.id, data);
    }
    setEditing(null);
    setEditId(null);
    load();
  };

  const handleDelete = async (id: string) => {
    await api.deleteAgent(team.id, id);
    load();
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Agents - {team.name}</h2>
        <button className="btn-sm" onClick={() => startEdit()}>+ 添加 Agent</button>
      </div>

      <ul className="item-list">
        {agents.length === 0 && <li className="empty-hint">暂无 Agent，点击上方添加</li>}
        {agents.map((a) => (
          <li key={a.id}>
            <span className={`role-badge role-${a.role}`}>{a.role}</span>
            <span className="item-name" onClick={() => startEdit(a)}>{a.name}</span>
            <span className="item-meta">{a.model || "default"}</span>
            <button className="btn-sm btn-danger" onClick={() => handleDelete(a.id)}>删除</button>
          </li>
        ))}
      </ul>

      <Drawer open={!!editing} title={editId ? "编辑 Agent" : "添加 Agent"} onClose={() => setEditing(null)}>
        {editing && (
          <div className="drawer-form">
            <label>名称 *</label>
            <input placeholder="例如：researcher" value={editing.name || ""} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />

            <label>角色</label>
            <select value={editing.role || "sub"} onChange={(e) => setEditing({ ...editing, role: e.target.value as "main" | "sub" })}>
              <option value="main">Main（主 Agent）</option>
              <option value="sub">Sub（子 Agent）</option>
            </select>

            <label>模型</label>
            <ModelSelect value={editing.model || ""} onChange={(v) => setEditing({ ...editing, model: v })} emptyLabel="使用团队默认模型" />

            <label>描述</label>
            <input placeholder="该 Agent 的职责说明" value={editing.description || ""} onChange={(e) => setEditing({ ...editing, description: e.target.value })} />

            <label>系统提示词</label>
            <textarea placeholder="System prompt" value={editing.system_prompt || ""} onChange={(e) => setEditing({ ...editing, system_prompt: e.target.value })} rows={4} />

            <div className="drawer-actions">
              <button onClick={save}>保存</button>
              <button className="btn-secondary" onClick={() => setEditing(null)}>取消</button>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
