/** Vite base，生产为 /dios/；拼接 API 前缀 /dios/api */
export function apiPrefix(): string {
  const base = import.meta.env.BASE_URL || "/";
  const normalized = base.endsWith("/") ? base.slice(0, -1) : base;
  return `${normalized}/api`;
}
