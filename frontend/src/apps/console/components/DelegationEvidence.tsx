import type { WorkAuthorization } from "../../../api/runtime";
import { t, languageTag } from "../../../lib/i18n";
import SourceRevocation from "./SourceRevocation";

export default function DelegationEvidence({ value, observedAt }: { value: WorkAuthorization["delegation"]; observedAt: number }) {
  if (!value) return null;
  const invalid = value.chain.some(link => link.state !== "active" || link.authorization_epoch !== link.captured_epoch || link.expires_at <= observedAt);
  return <details data-testid="delegation-chain" style={{ overflowWrap: "anywhere" }}>
    <summary>{t("授权委派链")}</summary>
    {invalid && <p className="tr-notice">{t("当前委派链已失效，历史执行记录保留。")}</p>}
    <ol>{value.chain.map(link => <li key={link.id}>
      <p><strong>{link.depth === 0 ? t("根授权") : t("第 {0} 层委派", [link.depth])}</strong> · {t(link.state === "revoked" ? "已撤销" : link.expires_at <= observedAt ? "已过期" : "有效")}</p>
      <p>{t("受托方：")}{link.actor}</p>
      <SourceRevocation event={link.source_event} />
      {link.revocation && <p className="tr-muted" data-testid="delegation-revocation">
        {t("委派撤销记录：")}{new Date(link.revocation.created_at * 1000).toLocaleString(languageTag())}
        <br /><code>{link.revocation.id}</code>
      </p>}
      {link.source_check && <p className="tr-muted" data-testid="delegation-source-check">
        {t("签发身份巡检：")}{t(({ valid: "来源有效", revoked: "来源已失效", unavailable: "身份服务暂不可用", pending: "等待检查" })[link.source_check.outcome])}
        {link.source_check.checked_at != null && <> · {t("最近检查：")}{new Date(link.source_check.checked_at * 1000).toLocaleString(languageTag())}</>}
        {link.state === "active" && link.expires_at > observedAt && link.source_check.outcome !== "revoked" && <>
          <br />{t("下次检查：")}{new Date(link.source_check.next_check_at * 1000).toLocaleString(languageTag())}
          {link.source_check.stale && <> · {t("观测已过期")}</>}
        </>}
      </p>}
      <p className="tr-muted">{t("准入代次 {0} · 当前代次 {1}", [link.captured_epoch, link.authorization_epoch])}<br />{t("有效至")}{new Date(link.expires_at * 1000).toLocaleString(languageTag())}</p>
      <p className="tr-muted"><code>{link.id}</code></p>
    </li>)}</ol>
  </details>;
}
