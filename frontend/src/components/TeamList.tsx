import { useEffect, useState } from "react";
import type { Team } from "../types";
import { api } from "../api/client";
import Drawer from "./Drawer";
import ModelSelect from "./ModelSelect";

interface Props {
  onSelect: (team: Team) => void;
  selected?: Team;
}

export default function TeamList({ onSelect, selected }: Props) {
  const [teams, setTeams] = useState<Team[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [model, setModel] = useState("");

  const load = () => api.listTeams().then(setTeams);
  useEffect(() => { load(); }, []);

  const openCreate = () => {
    setName("");
    setDesc("");
    setModel("");
    setDrawerOpen(true);
  };

  const handleCreate = async () => {
    if (!name.trim()) return;
    await api.createTeam({ name: name.trim(), description: desc.trim(), default_model: model.trim() });
    setDrawerOpen(false);
    load();
  };

  const handleDelete = async (id: string) => {
    await api.deleteTeam(id);
    load();
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Teams</h2>
        <button className="btn-sm" onClick={openCreate}>+ 新建</button>
      </div>
      <ul className="item-list">
        {teams.length === 0 && <li className="empty-hint">暂无团队，点击上方新建</li>}
        {teams.map((t) => (
          <li key={t.id} className={selected?.id === t.id ? "active" : ""}>
            <span className="item-name" onClick={() => onSelect(t)}>{t.name}</span>
            <span className="item-meta">{t.default_model || "未设置模型"}</span>
            <button className="btn-sm btn-danger" onClick={() => handleDelete(t.id)}>删除</button>
          </li>
        ))}
      </ul>

      <Drawer open={drawerOpen} title="新建团队" onClose={() => setDrawerOpen(false)}>
        <div className="drawer-form">
          <label>团队名称 *</label>
          <input placeholder="例如：论文写作团队" value={name} onChange={(e) => setName(e.target.value)} />

          <label>描述</label>
          <textarea placeholder="团队用途说明（可选）" value={desc} onChange={(e) => setDesc(e.target.value)} rows={3} />

          <label>默认模型</label>
          <ModelSelect value={model} onChange={setModel} emptyLabel="不指定" />

          <div className="drawer-actions">
            <button onClick={handleCreate}>创建</button>
            <button className="btn-secondary" onClick={() => setDrawerOpen(false)}>取消</button>
          </div>
        </div>
      </Drawer>
    </div>
  );
}
