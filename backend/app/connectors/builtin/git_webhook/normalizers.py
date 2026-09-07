"""GitHub / GitLab / Gitea webhook 适配器。

所有平台统一映射到 git.* 事件类型，订阅方无需关心具体平台。
"""

from __future__ import annotations

import hashlib
import hmac

from app.connectors.contracts import BaseWebhookAdapter, CloudEvent
from app.connectors.events import make_event

# ── GitHub ──

GITHUB_EVENT_MAP: dict[tuple[str, str], str] = {
    ("issues", "opened"): "git.issue.opened",
    ("issues", "closed"): "git.issue.closed",
    ("issues", "reopened"): "git.issue.reopened",
    ("issues", "edited"): "git.issue.edited",
    ("issue_comment", "created"): "git.issue.comment_created",
    ("pull_request", "opened"): "git.pull_request.created",
    ("pull_request", "closed"): "git.pull_request.closed",
    ("pull_request", "synchronize"): "git.pull_request.synchronize",
    ("pull_request", "reopened"): "git.pull_request.reopened",
    ("pull_request", "edited"): "git.pull_request.edited",
    ("pull_request_review", "submitted"): "git.pull_request.review_submitted",
    ("pull_request_review_comment", "created"): "git.pull_request.review_comment_created",
    ("push", ""): "git.push",
}


class GitHubNormalizer(BaseWebhookAdapter):
    platform = "github"

    def detect(self, headers: dict[str, str]) -> bool:
        return "x-github-event" in headers

    def verify_signature(self, headers: dict[str, str], body: bytes, secret: str) -> bool:
        if not secret:
            return True
        sig_header = headers.get("x-hub-signature-256", "")
        if not sig_header.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig_header[7:], expected)

    def normalize(self, headers: dict[str, str], payload: dict) -> CloudEvent:
        gh_event = headers.get("x-github-event", "")
        action = payload.get("action", "")
        repo = payload.get("repository", {}).get("full_name", "unknown")

        event_type = GITHUB_EVENT_MAP.get(
            (gh_event, action),
            GITHUB_EVENT_MAP.get((gh_event, ""), f"github.{gh_event}.{action}"),
        )

        subject = ""
        if "issue" in payload:
            subject = f"issue/{payload['issue']['number']}"
        elif "pull_request" in payload:
            subject = f"pr/{payload['pull_request']['number']}"
        elif "ref" in payload:
            subject = payload["ref"]

        return make_event(
            source=f"github/{repo}",
            event_type=event_type,
            subject=subject,
            data=payload,
        )


# ── GitLab ──

GITLAB_EVENT_MAP: dict[str, str] = {
    "Issue Hook": "git.issue.opened",
    "Merge Request Hook": "git.pull_request.created",
    "Push Hook": "git.push",
    "Tag Push Hook": "git.push",
    "Note Hook": "git.comment.created",
    "Pipeline Hook": "gitlab.pipeline",
    "Job Hook": "gitlab.job",
}


class GitLabNormalizer(BaseWebhookAdapter):
    platform = "gitlab"

    def detect(self, headers: dict[str, str]) -> bool:
        return "x-gitlab-event" in headers

    def verify_signature(self, headers: dict[str, str], body: bytes, secret: str) -> bool:
        if not secret:
            return True
        token = headers.get("x-gitlab-token", "")
        return hmac.compare_digest(token, secret)

    def normalize(self, headers: dict[str, str], payload: dict) -> CloudEvent:
        gl_event = headers.get("x-gitlab-event", "")
        project = payload.get("project", {})
        repo = project.get("path_with_namespace", "unknown")

        base_type = GITLAB_EVENT_MAP.get(gl_event, f"gitlab.{gl_event}")

        # 细化 Merge Request 事件
        event_type = base_type
        if gl_event == "Merge Request Hook":
            action = payload.get("object_attributes", {}).get("action", "")
            mr_action_map = {
                "open": "git.pull_request.created",
                "reopen": "git.pull_request.reopened",
                "close": "git.pull_request.closed",
                "merge": "git.pull_request.closed",
                "update": "git.pull_request.synchronize",
                "approved": "git.pull_request.review_submitted",
            }
            event_type = mr_action_map.get(action, base_type)

        # 细化 Issue 事件
        if gl_event == "Issue Hook":
            action = payload.get("object_attributes", {}).get("action", "")
            issue_action_map = {
                "open": "git.issue.opened",
                "reopen": "git.issue.reopened",
                "close": "git.issue.closed",
                "update": "git.issue.edited",
            }
            event_type = issue_action_map.get(action, base_type)

        subject = ""
        obj = payload.get("object_attributes", {})
        if obj.get("iid"):
            kind = "mr" if "merge" in gl_event.lower() else "issue"
            subject = f"{kind}/{obj['iid']}"
        elif "ref" in payload:
            subject = payload["ref"]

        return make_event(
            source=f"gitlab/{repo}",
            event_type=event_type,
            subject=subject,
            data=payload,
        )


# ── Gitea ──

GITEA_EVENT_MAP: dict[tuple[str, str], str] = {
    ("issues", "opened"): "git.issue.opened",
    ("issues", "closed"): "git.issue.closed",
    ("issues", "reopened"): "git.issue.reopened",
    ("issues", "edited"): "git.issue.edited",
    ("issue_comment", "created"): "git.issue.comment_created",
    ("pull_request", "opened"): "git.pull_request.created",
    ("pull_request", "closed"): "git.pull_request.closed",
    ("pull_request", "synchronized"): "git.pull_request.synchronize",
    ("pull_request", "reopened"): "git.pull_request.reopened",
    ("pull_request", "edited"): "git.pull_request.edited",
    ("pull_request_approved", ""): "git.pull_request.review_submitted",
    ("pull_request_review_comment", "created"): "git.pull_request.review_comment_created",
    ("push", ""): "git.push",
}


class GiteaNormalizer(BaseWebhookAdapter):
    platform = "gitea"

    def detect(self, headers: dict[str, str]) -> bool:
        return "x-gitea-event" in headers

    def verify_signature(self, headers: dict[str, str], body: bytes, secret: str) -> bool:
        if not secret:
            return True
        sig_header = headers.get("x-gitea-signature", "")
        if not sig_header:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig_header, expected)

    def normalize(self, headers: dict[str, str], payload: dict) -> CloudEvent:
        gt_event = headers.get("x-gitea-event", "")
        action = payload.get("action", "")
        repo = payload.get("repository", {}).get("full_name", "unknown")

        event_type = GITEA_EVENT_MAP.get(
            (gt_event, action),
            GITEA_EVENT_MAP.get((gt_event, ""), f"gitea.{gt_event}.{action}"),
        )

        subject = ""
        if "issue" in payload:
            subject = f"issue/{payload['issue']['number']}"
        elif "pull_request" in payload:
            subject = f"pr/{payload['pull_request']['number']}"
        elif "ref" in payload:
            subject = payload["ref"]

        return make_event(
            source=f"gitea/{repo}",
            event_type=event_type,
            subject=subject,
            data=payload,
        )
