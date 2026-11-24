import type { WorkAuthorization } from "../../../api/runtime";
import { t, languageTag } from "../../../lib/i18n";

export default function DelegationEvidence({ value, observedAt }: { value: WorkAuthorization["delegation"]; observedAt: number }) {
  if (!value) return null;
  const invalid = value.chain.some(link => link.state !== "active" || link.authorization_epoch !== link.captured_epoch || link.expires_at <= observedAt);
  return <details data-testid="delegation-chain" style={{ overflowWrap: "anywhere" }}>
    <summary>{t("授权委派链")}</summary>
    {invalid && <p className="tr-notice">{t("当前委派链已失效，历史执行记录保留。")}</p>}
    <ol>{value.chain.map(link => <li key={link.id}>
      <p><strong>{link.depth === 0 ? t("根授权") : t("第 {0} 层委派", [link.depth])}</strong> · {t(link.state === "revoked" ? "已撤销" : link.expires_at <= observedAt ? "已过期" : "有效")}</p>
      <p>{t("受托方：")}{link.actor}</p>
      <p className="tr-muted">{t("准入代次 {0} · 当前代次 {1}", [link.captured_epoch, link.authorization_epoch])}<br />{t("有效至")}{new Date(link.expires_at * 1000).toLocaleString(languageTag())}</p>
      <p className="tr-muted"><code>{link.id}</code></p>
    </li>)}</ol>
  </details>;
}
