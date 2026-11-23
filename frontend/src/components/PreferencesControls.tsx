import { usePreferences } from "../lib/preferences";
import type { Theme, Language } from "../lib/preferences";
export default function PreferencesControls() {
  const p = usePreferences();
  return <div className="preferences-controls" role="group" aria-label={p.locale === "zh" ? "显示与语言" : "Appearance and language"}>
    <label><span>{p.locale === "zh" ? "主题" : "Theme"}</span>
      <select aria-label={p.locale === "zh" ? "主题" : "Theme"} value={p.theme} onChange={e => p.setTheme(e.target.value as Theme)}>
        <option value="light">Light</option><option value="dark">Dark</option><option value="auto">Auto</option>
      </select>
    </label>
    <label><span>{p.locale === "zh" ? "语言" : "Language"}</span>
      <select aria-label={p.locale === "zh" ? "语言" : "Language"} value={p.language} onChange={e => p.setLanguage(e.target.value as Language)}>
        <option value="auto">Auto</option><option value="zh">中文</option><option value="en">English</option>
      </select>
    </label>
  </div>;
}
