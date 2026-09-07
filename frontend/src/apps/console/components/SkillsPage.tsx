import { useEffect, useMemo, useState } from "react";
import type { Skill } from "../../../types";
import { api } from "../../../api/os";
import Drawer from "../../../components/Drawer";

interface RegistryRepo {
  name: string;
  url: string;
  description: string;
}

function sourceLabel(url: string): string {
  if (!url) return "本机创建";
  try { return new URL(url).hostname; } catch { return "Git source"; }
}

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [edit, setEdit] = useState<Partial<Skill> | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<RegistryRepo[]>([]);
  const [searchDone, setSearchDone] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [gitUrl, setGitUrl] = useState("");
  const [importingUrl, setImportingUrl] = useState("");
  const [importError, setImportError] = useState("");

  const load = () => api.listSkills().then(setSkills);
  useEffect(() => { load(); }, []);
  useEffect(() => {
    api.searchSkillRegistry("")
      .then((response) => setSearchResults(response.repos))
      .catch(() => setSearchError("DiOS 推荐目录暂时不可用"))
      .finally(() => setSearchDone(true));
  }, []);

  const gitCount = useMemo(() => skills.filter((skill) => !!skill.source_url).length, [skills]);

  const doSearch = async () => {
    setSearchDone(false);
    setSearchError("");
    try {
      const response = await api.searchSkillRegistry(searchQuery.trim());
      setSearchResults(response.repos);
    } catch (error) {
      setSearchResults([]);
      setSearchError(error instanceof Error ? error.message : "目录暂时不可用");
    } finally {
      setSearchDone(true);
    }
  };

  const importFromUrl = async (url: string) => {
    if (!url) return;
    setImportingUrl(url);
    setImportError("");
    try {
      await api.importSkillFromGit(url);
      setGitUrl("");
      await load();
    } catch (error) {
      setImportError(error instanceof Error ? error.message : String(error));
    } finally {
      setImportingUrl("");
    }
  };

  const openEdit = (skill?: Skill) => setEdit(skill ? { ...skill } : { name: "", description: "", source_url: "", content: "" });
  const save = async () => {
    if (!edit?.name?.trim()) return;
    const payload = { name: edit.name, description: edit.description, source_url: edit.source_url, content: edit.content };
    if (edit.id) await api.updateSkill(edit.id, payload);
    else await api.createSkill(payload);
    setEdit(null);
    load();
  };

  return (
    <div className="registry-page">
      <header className="registry-page-header">
        <div>
          <div className="registry-title-line">
            <h3>Skill Packages</h3>
            <span className="registry-standard-badge">Agent Skills</span>
            <span className="registry-standard-badge local">DiOS curated</span>
          </div>
          <p>Skill 提供可移植的操作说明和资源；它帮助 Agent 选择做法，但不能扩大工具权限。</p>
        </div>
        <div className="registry-kpis">
          <div><strong>{skills.length}</strong><span>已注册</span></div>
          <div><strong>{gitCount}</strong><span>Git 导入</span></div>
          <div><strong>{skills.length - gitCount}</strong><span>本机创建</span></div>
        </div>
      </header>

      <section className="registry-block">
        <div className="registry-section-heading">
          <div><h4>从 Git 注册 Skill</h4><p>导入前检查仓库中的 SKILL.md，并读取标准 frontmatter。</p></div>
          <span className="registry-source-pill">SKILL.md</span>
        </div>
        <div className="registry-search-row">
          <input placeholder="Git repository URL" value={gitUrl} onChange={(event) => setGitUrl(event.target.value)} onKeyDown={(event) => event.key === "Enter" && importFromUrl(gitUrl.trim())} />
          <button className="btn-sm" onClick={() => importFromUrl(gitUrl.trim())} disabled={!!importingUrl || !gitUrl.trim()}>{importingUrl && importingUrl === gitUrl.trim() ? "导入中…" : "检查并注册"}</button>
        </div>
        {importError && <p className="registry-error">{importError}</p>}
      </section>

      <section className="registry-block">
        <div className="registry-section-heading">
          <div><h4>DiOS 推荐目录</h4><p>Agent Skills 规定包格式，但没有统一公共 Registry；此处是 DiOS 维护的来源目录。</p></div>
          <span className="registry-source-pill curated">curated sources</span>
        </div>
        <div className="registry-search-row">
          <input placeholder="按名称或用途搜索" value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} onKeyDown={(event) => event.key === "Enter" && doSearch()} />
          <button className="btn-sm btn-secondary" onClick={doSearch}>搜索目录</button>
        </div>
        {searchError && <p className="registry-error">{searchError}</p>}
        {searchResults.length > 0 && (
          <div className="registry-results standardized">
            {searchResults.map((repo) => {
              const registered = skills.some((skill) => skill.source_url === repo.url);
              return (
                <article key={repo.url} className="registry-result-item">
                  <div className="registry-result-info">
                    <div className="registry-result-title"><span className="registry-result-name">{repo.name}</span><span className="registry-status">Git</span></div>
                    <span className="registry-result-desc">{repo.description}</span>
                    <span className="registry-result-cmd">{repo.url}</span>
                  </div>
                  <button className={registered ? "btn-sm btn-secondary" : "btn-sm"} onClick={() => importFromUrl(repo.url)} disabled={registered || !!importingUrl}>{registered ? "已注册" : importingUrl === repo.url ? "导入中…" : "注册"}</button>
                </article>
              );
            })}
          </div>
        )}
        {searchDone && searchResults.length === 0 && !searchError && <div className="registry-inline-empty">没有匹配的 Skill</div>}
      </section>

      <section className="registry-block">
        <div className="registry-section-heading">
          <div><h4>已注册 Skills</h4><p>注册后的 Skill 可在 Agent 配置中显式分配。</p></div>
          <button className="btn-sm btn-secondary" onClick={() => openEdit()}>手动创建</button>
        </div>
        {skills.length === 0 ? (
          <div className="registry-inline-empty">尚未注册 Skill，可从 Git 导入或手动创建。</div>
        ) : (
          <div className="registry-card-grid">
            {skills.map((skill) => (
              <article key={skill.id} className="registry-resource-card">
                <div className="registry-resource-head"><span className="entity-card-name">{skill.name}</span><span className="registry-status ready">已注册</span></div>
                <div className="registry-resource-description">{skill.description || "无描述"}</div>
                <div className="registry-card-footer">
                  <span>{sourceLabel(skill.source_url)}</span>
                  <div><button className="btn-sm btn-secondary" onClick={() => openEdit(skill)}>编辑</button><button className="btn-sm btn-danger" onClick={async () => { await api.deleteSkill(skill.id); load(); }}>删除</button></div>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <aside className="registry-policy-note"><strong>Agent Skills 约束</strong><span>名称使用小写字母、数字和连字符；description 应同时说明能力与适用场景。allowed-tools 仍需经过 DiOS 授权策略。</span></aside>

      <Drawer open={!!edit} title={edit?.id ? "编辑 Skill" : "创建 Skill"} onClose={() => setEdit(null)}>
        {edit && (
          <div className="drawer-form">
            <label>Name</label>
            <input value={edit.name || ""} onChange={(event) => setEdit({ ...edit, name: event.target.value })} placeholder="e.g. code-review" />
            <label>Description</label>
            <textarea value={edit.description || ""} onChange={(event) => setEdit({ ...edit, description: event.target.value })} placeholder="说明它能做什么，以及何时使用" rows={3} />
            <label>Source URL</label>
            <input value={edit.source_url || ""} onChange={(event) => setEdit({ ...edit, source_url: event.target.value })} placeholder="https://github.com/..." />
            <label>Content (SKILL.md)</label>
            <textarea value={edit.content || ""} onChange={(event) => setEdit({ ...edit, content: event.target.value })} placeholder="---\nname: code-review\ndescription: ...\n---" rows={14} className="mono" />
            <div className="drawer-actions"><button onClick={save}>保存</button><button className="btn-secondary" onClick={() => setEdit(null)}>取消</button></div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
