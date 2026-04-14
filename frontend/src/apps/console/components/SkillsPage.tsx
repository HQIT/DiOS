import { useEffect, useState } from "react";
import type { Skill } from "../../../types";
import { api } from "../../../api/os";
import Drawer from "../../../components/Drawer";

interface RegistryRepo {
  name: string;
  url: string;
  description: string;
}

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [edit, setEdit] = useState<Partial<Skill> | null>(null);

  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<RegistryRepo[]>([]);
  const [searchDone, setSearchDone] = useState(false);

  const [gitUrl, setGitUrl] = useState("");
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState("");

  const load = () => api.listSkills().then(setSkills);
  useEffect(() => { load(); }, []);

  // 初始加载推荐列表
  useEffect(() => {
    api.searchSkillRegistry("").then((res) => {
      setSearchResults(res.repos);
      setSearchDone(true);
    });
  }, []);

  const doSearch = async () => {
    setSearchDone(false);
    try {
      const res = await api.searchSkillRegistry(searchQuery.trim());
      setSearchResults(res.repos);
    } catch {
      setSearchResults([]);
    } finally {
      setSearchDone(true);
    }
  };

  const importFromUrl = async (url: string) => {
    setImporting(true);
    setImportError("");
    try {
      await api.importSkillFromGit(url);
      setGitUrl("");
      load();
    } catch (err) {
      setImportError(err instanceof Error ? err.message : String(err));
    } finally {
      setImporting(false);
    }
  };

  const openEdit = (s?: Skill) => {
    if (s) setEdit({ ...s });
    else setEdit({ name: "", description: "", source_url: "", content: "" });
  };

  const save = async () => {
    if (!edit?.name?.trim()) return;
    const payload = { name: edit.name, description: edit.description, source_url: edit.source_url, content: edit.content };
    if (edit.id) await api.updateSkill(edit.id, payload);
    else await api.createSkill(payload);
    setEdit(null);
    load();
  };

  return (
    <div className="panel">
      <p className="text-muted" style={{ marginBottom: 12 }}>
        OS 严选 Skills 仓库。从 Git 仓库导入 Skill，Agent 可按需选用。
      </p>

      {/* Git Import */}
      <div className="registry-search">
        <h4 className="catalog-section-title">Import from Git</h4>
        <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
          <input
            style={{ flex: 1 }}
            placeholder="Git repo URL, e.g. https://github.com/user/skill-repo"
            value={gitUrl}
            onChange={(e) => setGitUrl(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && gitUrl.trim()) importFromUrl(gitUrl.trim()); }}
          />
          <button className="btn-sm" onClick={() => importFromUrl(gitUrl.trim())} disabled={importing || !gitUrl.trim()}>
            {importing ? "Importing..." : "Import"}
          </button>
        </div>
        {importError && <p style={{ color: "var(--color-danger)", fontSize: 12 }}>{importError}</p>}
      </div>

      {/* Registry / Recommended */}
      <div className="registry-search" style={{ marginTop: 16 }}>
        <h4 className="catalog-section-title">Recommended Skills</h4>
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <input
            style={{ flex: 1 }}
            placeholder="Search recommended skills..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") doSearch(); }}
          />
          <button className="btn-sm" onClick={doSearch}>Search</button>
        </div>
        {searchResults.length > 0 && (
          <div className="registry-results">
            {searchResults.map((repo) => {
              const alreadyImported = skills.some((s) => s.source_url === repo.url);
              return (
                <div key={repo.url} className="registry-result-item">
                  <div className="registry-result-info">
                    <span className="registry-result-name">{repo.name}</span>
                    <span className="registry-result-desc">{repo.description}</span>
                    <span className="registry-result-cmd">{repo.url}</span>
                  </div>
                  {alreadyImported ? (
                    <span className="text-muted" style={{ fontSize: 11, whiteSpace: "nowrap" }}>Imported</span>
                  ) : (
                    <button className="btn-sm" onClick={() => importFromUrl(repo.url)} disabled={importing}>
                      {importing ? "..." : "Import"}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {searchDone && searchResults.length === 0 && (
          <p className="text-muted">No matching skills found</p>
        )}
      </div>

      {/* Installed Skills */}
      <h4 className="catalog-section-title" style={{ marginTop: 20 }}>Installed Skills</h4>
      <div className="card-grid">
        {skills.map((s) => (
          <div key={s.id} className="entity-card">
            <div className="entity-card-header">
              <span className="entity-card-name">{s.name}</span>
            </div>
            <div className="entity-card-meta">
              <span>{s.description || "No description"}</span>
              {s.source_url && (
                <span className="mono" style={{ fontSize: 11, marginTop: 4, display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {s.source_url}
                </span>
              )}
            </div>
            <div className="entity-card-actions">
              <button className="btn-sm btn-secondary" onClick={() => openEdit(s)}>Edit</button>
              <button className="btn-sm btn-danger" onClick={async () => { await api.deleteSkill(s.id); load(); }}>Delete</button>
            </div>
          </div>
        ))}
        <div className="entity-card add-card" onClick={() => openEdit()}>
          <span className="add-card-icon">+</span>
          <span className="add-card-label">Add Skill Manually</span>
        </div>
      </div>

      {/* Edit Drawer */}
      <Drawer open={!!edit} title={edit?.id ? "Edit Skill" : "Add Skill"} onClose={() => setEdit(null)}>
        {edit && (
          <div className="drawer-form">
            <label>Name</label>
            <input value={edit.name || ""} onChange={(e) => setEdit({ ...edit, name: e.target.value })} placeholder="e.g. code-review" />

            <label>Description</label>
            <input value={edit.description || ""} onChange={(e) => setEdit({ ...edit, description: e.target.value })} placeholder="What this skill does" />

            <label>Source URL</label>
            <input value={edit.source_url || ""} onChange={(e) => setEdit({ ...edit, source_url: e.target.value })} placeholder="https://github.com/..." />

            <label>Content (SKILL.md)</label>
            <textarea
              value={edit.content || ""}
              onChange={(e) => setEdit({ ...edit, content: e.target.value })}
              placeholder="Skill instructions in markdown..."
              rows={12}
              style={{ fontFamily: "monospace", fontSize: 13, resize: "vertical" }}
            />

            <div className="drawer-actions">
              <button onClick={save}>保存</button>
              <button className="btn-secondary" onClick={() => setEdit(null)}>取消</button>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
