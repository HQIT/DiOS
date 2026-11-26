import type { WorkAuthorization } from "../../../api/runtime";
import { t, languageTag } from "../../../lib/i18n";

export default function SourceRevocation({ event }: { event: WorkAuthorization["source_event"] }) {
  if (!event) return null;
  const time = (value: number) => new Date(value * 1000).toLocaleString(languageTag());
  return <details className="tr-notice" data-testid="source-logout" style={{ overflowWrap: "anywhere" }}>
    <summary>{t("身份来源撤销记录")}</summary>
    <p>{t("已观测来源事件时间：")}{time(event.issued_at)}</p>
    <p>{t("首次记录：")}{time(event.first_received_at ?? event.received_at)}</p>
    <p>{t("最近接收：")}{time(event.last_received_at ?? event.received_at)} · {t("已记录 {0} 个事件", [event.event_count ?? 1])}</p>
    <p className="tr-muted">{t("时间范围仅包含已收到的事件；执行停止状态见任务记录。")}</p>
    <code>{event.id}</code>
  </details>;
}
