"""git_webhook Connector 声明。"""

from __future__ import annotations

from typing import Any

from app.connectors.contracts import (
    CAPABILITY_WEBHOOK,
    ConnectorManifest,
    EventSourceDecl,
    EventTypeDecl,
)
from app.connectors.builtin.git_webhook.normalizers import (
    GITEA_EVENT_MAP,
    GITHUB_EVENT_MAP,
    GITLAB_EVENT_MAP,
    GiteaNormalizer,
    GitHubNormalizer,
    GitLabNormalizer,
)

PLATFORMS = ("github", "gitlab", "gitea")

_DESCRIPTIONS: dict[str, str] = {
    "git.push": "代码推送",
    "git.issue.opened": "Issue 创建",
    "git.issue.closed": "Issue 关闭",
    "git.issue.reopened": "Issue 重新打开",
    "git.issue.edited": "Issue 编辑",
    "git.issue.comment_created": "Issue 评论",
    "git.pull_request.created": "PR/MR 创建",
    "git.pull_request.closed": "PR/MR 关闭",
    "git.pull_request.synchronize": "PR/MR 更新",
    "git.pull_request.reopened": "PR/MR 重新打开",
    "git.pull_request.edited": "PR/MR 编辑",
    "git.pull_request.review_submitted": "PR/MR 评审提交",
    "git.pull_request.review_comment_created": "PR/MR 评审评论",
    "git.comment.created": "评论创建",
}


def _event_types() -> tuple[EventTypeDecl, ...]:
    mapped = set(GITHUB_EVENT_MAP.values()) | set(GITLAB_EVENT_MAP.values()) | set(GITEA_EVENT_MAP.values())
    return tuple(EventTypeDecl(type=t, description=_DESCRIPTIONS.get(t, "")) for t in sorted(mapped))


def _source_patterns(instance: Any) -> list[str]:
    instance_type = (getattr(instance, "type", "") or "").strip()
    # 历史数据直接以平台名作为 type
    if instance_type in PLATFORMS:
        return [f"{instance_type}/*"]
    platform = str((getattr(instance, "config", None) or {}).get("platform", "")).strip()
    return [f"{platform}/*"] if platform else []


def _webhook_secrets(instance: Any) -> dict[str, str]:
    config = getattr(instance, "config", None) or {}
    secret = config.get("secret") or ""
    if not secret:
        return {}
    instance_type = (getattr(instance, "type", "") or "").strip()
    if instance_type in PLATFORMS:
        return {instance_type: secret}
    platform = str(config.get("platform", "")).strip()
    return {platform: secret} if platform else {}


MANIFEST = ConnectorManifest(
    type="git_webhook",
    label="Git Webhook",
    description="GitHub / GitLab / Gitea 等 Git 平台回调",
    capabilities=(CAPABILITY_WEBHOOK,),
    aliases=PLATFORMS,
    order=10,
    config_schema={
        "type": "object",
        "required": ["platform"],
        "properties": {
            "platform": {
                "type": "string",
                "enum": list(PLATFORMS),
                "title": "平台",
            },
            "secret": {
                "type": "string",
                "title": "Webhook Secret",
                "description": "留空则跳过签名校验",
            },
        },
    },
    secret_fields=("secret",),
    event_sources=(
        EventSourceDecl(id="git", name="Git Webhook", description="GitHub / GitLab / Gitea 等"),
    ),
    event_types=_event_types(),
    accepted_source_patterns=("github/*", "gitlab/*", "gitea/*"),
    subscribable_categories=("git",),
    source_patterns=_source_patterns,
    webhook_adapters=(GitHubNormalizer(), GitLabNormalizer(), GiteaNormalizer()),
    webhook_secrets=_webhook_secrets,
)
