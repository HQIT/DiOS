import { useEffect, useState, type ReactNode } from "react";
import { apiPrefix } from "../lib/apiBase";
import { getAccessToken, setAccessToken, clearAccessToken } from "../lib/auth";

export default function AccessGate({ children }: { children: ReactNode }) {
  const [checked, setChecked] = useState(false);
  const [unlocked, setUnlocked] = useState(() => !!getAccessToken());
  const [input, setInput] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(`${apiPrefix()}/auth/status`)
      .then(async (r) => {
        if (!r.ok) {
          // status 本身应公开；若失败则仍要求登录，避免误放行后 API 全 401
          return;
        }
        const d = (await r.json()) as { access_token_required?: boolean };
        if (!d.access_token_required) setUnlocked(true);
      })
      .catch(() => {
        /* 探测失败时仍显示登录页，由用户输入 token 重试 */
      })
      .finally(() => setChecked(true));
  }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const value = input.trim();
    if (!value) {
      setError("请输入 Access Token");
      return;
    }
    setError("");
    setAccessToken(value);
    try {
      const res = await fetch(`${apiPrefix()}/os/models`, {
        headers: { "X-DiOS-Access-Token": value },
      });
      if (res.status === 401) {
        clearAccessToken();
        setError("Access Token 无效");
        return;
      }
      if (!res.ok) {
        clearAccessToken();
        setError(`验证失败 (${res.status})`);
        return;
      }
      setUnlocked(true);
    } catch {
      clearAccessToken();
      setError("无法连接服务，请稍后重试");
    }
  }

  if (!checked) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <span style={{ fontSize: 13, color: "var(--muted, #8b9cb3)" }}>加载中…</span>
      </div>
    );
  }

  if (unlocked) {
    return <>{children}</>;
  }

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "var(--bg, #0f1419)",
        padding: 24,
      }}
    >
      <form
        onSubmit={handleSubmit}
        style={{
          width: "100%",
          maxWidth: 360,
          padding: 28,
          borderRadius: 12,
          border: "1px solid var(--border, #2a3441)",
          background: "var(--surface, #1a2332)",
        }}
      >
        <h2 style={{ margin: "0 0 8px", fontSize: 18 }}>DiOS</h2>
        <p style={{ margin: "0 0 20px", fontSize: 13, color: "var(--muted, #8b9cb3)" }}>
          请输入 Access Token 以继续
        </p>
        <input
          type="password"
          autoComplete="off"
          placeholder="Access Token"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          style={{
            width: "100%",
            padding: "10px 12px",
            marginBottom: 12,
            borderRadius: 8,
            border: "1px solid var(--border, #2a3441)",
            background: "var(--bg, #0f1419)",
            color: "inherit",
            fontSize: 14,
          }}
        />
        {error && (
          <p style={{ margin: "0 0 12px", fontSize: 13, color: "#f87171" }}>{error}</p>
        )}
        <button
          type="submit"
          style={{
            width: "100%",
            padding: "10px 0",
            borderRadius: 8,
            border: "none",
            background: "#3b82f6",
            color: "#fff",
            fontSize: 14,
            cursor: "pointer",
          }}
        >
          进入
        </button>
      </form>
    </div>
  );
}
