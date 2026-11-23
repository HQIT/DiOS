import { useSyncExternalStore } from "react";
export type Theme = "light" | "dark" | "auto";
export type Language = "zh" | "en" | "auto";
const THEME_KEY = "dios_theme";
const LANGUAGE_KEY = "dios_language";
const listeners = new Set<() => void>();
const media = window.matchMedia("(prefers-color-scheme: dark)");
function stored(key: string) { try { return localStorage.getItem(key); } catch { return null; } }
function themeValue(value: string | null): Theme { return value === "light" || value === "dark" ? value : "auto"; }
function languageValue(value: string | null): Language { return value === "zh" || value === "en" ? value : "auto"; }
let theme = themeValue(stored(THEME_KEY));
let language = languageValue(stored(LANGUAGE_KEY));
let revision = 0;
export function locale(): "zh" | "en" {
  if (language !== "auto") return language;
  return (navigator.languages?.[0] || navigator.language || "en").toLowerCase().startsWith("zh") ? "zh" : "en";
}
function apply() {
  document.documentElement.dataset.theme = theme === "auto" ? (media.matches ? "dark" : "light") : theme;
  document.documentElement.style.colorScheme = document.documentElement.dataset.theme!;
  document.documentElement.lang = locale() === "zh" ? "zh-CN" : "en";
  revision++; listeners.forEach(listener => listener());
}
export function setTheme(value: Theme) { theme = value; try { localStorage.setItem(THEME_KEY, value); } catch { /* memory preference remains active */ } apply(); }
export function setLanguage(value: Language) { language = value; try { localStorage.setItem(LANGUAGE_KEY, value); } catch { /* memory preference remains active */ } apply(); }
export function usePreferences() {
  useSyncExternalStore(listener => { listeners.add(listener); return () => listeners.delete(listener); }, () => revision);
  return { theme, language, locale: locale(), setTheme, setLanguage };
}
media.addEventListener("change", () => { if (theme === "auto") apply(); });
window.addEventListener("languagechange", () => { if (language === "auto") apply(); });
window.addEventListener("storage", event => {
  if (event.key === THEME_KEY || event.key === null) theme = themeValue(stored(THEME_KEY));
  if (event.key === LANGUAGE_KEY || event.key === null) language = languageValue(stored(LANGUAGE_KEY));
  if (event.key === THEME_KEY || event.key === LANGUAGE_KEY || event.key === null) apply();
});
apply();
