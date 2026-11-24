import { t as translateUI } from "../lib/i18n";
import { useEffect, useState, type ReactNode } from "react";
import { apiPrefix } from "../lib/apiBase";
import { getAccessToken, setAccessToken, clearAccessToken, setBrowserSession, SESSION_EXPIRED_EVENT } from "../lib/auth";
import { AuthSessionContext } from "../lib/authSession";

import PreferencesControls from "./PreferencesControls";

export default function AccessGate({ children }: { children: ReactNode }) {
  const [checked, setChecked] = useState(false);
  const [unlocked, setUnlocked] = useState(false);
  const [input, setInput] = useState("");
  const [error, setError] = useState("");
  const [browser, setBrowser] = useState(false);
  const [principal, setPrincipal] = useState(false);
  const [label, setLabel] = useState("");

  async function checkBrowserIdentity() {
    try {
      const res = await fetch(`${apiPrefix()}/os/runtime/identity`, { cache: "no-store" });
      if (!res.ok) {
        setUnlocked(false);
        setError(res.status === 401 ? "" : "身份服务暂不可用，请稍后重试。");
        return;
      }
      const identity = await res.json();
      if (identity.mode !== "principal") throw new Error("Expected principal");
      setPrincipal(true);
      setLabel(identity.principal.id);
      if (!window.location.hash.startsWith("#console/tasks")) window.location.hash = "console/tasks";
      setError("");
      setUnlocked(true);
    } catch {
      setUnlocked(false);
      setError("无法连接服务，请稍后重试");
    }
  }

  useEffect(() => {
    const expired = () => { setUnlocked(false); setError("登录已过期，请重新登录。"); };
    window.addEventListener(SESSION_EXPIRED_EVENT, expired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, expired);
  }, []);

  useEffect(() => {
    fetch(`${apiPrefix()}/auth/status`, { cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) {
          // status 本身应公开；若失败则仍要求登录，避免误放行后 API 全 401
          return;
        }
        const d = (await r.json()) as { access_token_required?: boolean; mode?: string };
        const sso = d.mode === "browser_sso";
        setBrowserSession(sso);
        setBrowser(sso);
        if (sso) { await checkBrowserIdentity(); return; }
        if (!d.access_token_required) setUnlocked(true);
        const stored = getAccessToken();
        if (stored) {
          const identityResponse = await fetch(`${apiPrefix()}/os/runtime/identity`, { headers: { "X-DiOS-Access-Token": stored }, cache: "no-store" });
          if (identityResponse.ok) {
            const identity = await identityResponse.json();
            setPrincipal(identity.mode === "principal");
            setLabel(identity.principal?.id || "");
            setUnlocked(true);
          } else if (identityResponse.status === 401) clearAccessToken();
          else {
            const fallback = await fetch(`${apiPrefix()}/os/models`, { headers: { "X-DiOS-Access-Token": stored }, cache: "no-store" });
            if (fallback.ok) setUnlocked(true);
          }
        }
      })
      .catch(() => {
        /* 探测失败时仍显示登录页，由用户输入 token 重试 */
      })
      .finally(() => setChecked(true));
  }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (browser) return;
    const value = input.trim();
    if (!value) {
      setError("请输入 Access Token");
      return;
    }
    setError("");
    setAccessToken(value);
    try {
      let res = await fetch(`${apiPrefix()}/os/runtime/identity`, {
        headers: { "X-DiOS-Access-Token": value },
      });
      if (res.ok) {
        const identity = await res.json();
        setPrincipal(identity.mode === "principal");
        if (identity.mode === "principal") { setLabel(identity.principal.id); window.location.hash = "console/tasks"; }
      } else {
        const failure = await res.clone().json().catch(() => ({}));
        if (["AUTH_PROVIDER_UNAVAILABLE", "AUTH_PRINCIPAL_REVOKED", "AUTH_PRINCIPAL_EXPIRED"].includes(failure.detail)) {
          clearAccessToken();
          setError(failure.detail === "AUTH_PROVIDER_UNAVAILABLE" ? "身份服务暂不可用，请稍后重试。" : "来源授权已失效，请重新获取授权。");
          return;
        }
        res = await fetch(`${apiPrefix()}/os/models`, { headers: { "X-DiOS-Access-Token": value } });
      }
      if (res.status === 401) {
        clearAccessToken();
        setError("Access Token 无效");
        return;
      }
      if (!res.ok) {
        clearAccessToken();
        setError(translateUI("验证失败 ({0})", [res.status]));
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
        <span style={{ fontSize: 13, color: "var(--muted, #8b9cb3)" }}>{translateUI("加载中…")}</span>
      </div>
    );
  }

  if (unlocked) {
    const signOut = async () => {
      try {
        const result = await fetch("/oauth2/sign_out", { method: "POST", headers: { "X-CSRF-Token": "1" }, redirect: "manual" });
        if (result.type !== "opaqueredirect" && !result.ok) throw new Error("Logout failed");
        clearAccessToken();
        setPrincipal(false); setLabel(""); setError(""); setUnlocked(false);
        window.location.hash = "console/tasks";
      } catch { setError("退出失败，请重试。"); }
    };
    return <AuthSessionContext.Provider value={{ browser, principal, label, signOut: browser ? signOut : undefined }}>
      {error && <div role="alert">{translateUI(error)}</div>}
      {children}
    </AuthSessionContext.Provider>;
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
        <PreferencesControls />
        <h2 style={{ margin: "0 0 8px", fontSize: 18 }}>DiOS</h2>
        <p style={{ margin: "0 0 20px", fontSize: 13, color: "var(--muted, #8b9cb3)" }}>{translateUI(browser ? "使用企业账号登录" : "请输入 Access Token 以继续")}</p>
        {!browser && <>
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
        </>}
        {error && (
          <p style={{ margin: "0 0 12px", fontSize: 13, color: "#f87171" }}>{translateUI(error)}</p>
        )}
        {browser ? <>
          <a className="button" href="/oauth2/start?rd=%2Fdios%2F" style={{ display: "block", padding: "12px 0" }}>{translateUI("企业登录")}</a>
          {error && <button type="button" onClick={() => void checkBrowserIdentity()}>{translateUI("重试")}</button>}
        </> : <button
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
        >{translateUI("进入")}</button>}
      </form>
    </div>
  );
}
