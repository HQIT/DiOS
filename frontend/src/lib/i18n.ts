import { messages, englishLabels } from "./messages";
import { locale } from "./preferences";
export function languageTag() { return locale() === "zh" ? "zh-CN" : "en-US"; }
// Called explicitly at presentation boundaries, never over DOM or API payloads.
export function t<T>(value: T, parameters?: unknown[]): T {
  if (typeof value !== "string") return value;
  const key = value.replace(/\s+/g, " ").trim();
  const translated = locale() === "en" ? (messages[key] ?? value) : (englishLabels[key] ?? value);
  return (parameters ? translated.replace(/\{(\d+)\}/g, (_, i: string) => String(parameters[Number(i)] ?? "")) : translated) as T;
}
