#!/usr/bin/env bash
# 一键创建"代码开发协作"场景所需的 3 个智能体、skills 注册和订阅。
#
# 变更说明（相对旧版）：
# - 不再依赖 GitHub MCP Server，改用 git_platform skill（git + curl 调 REST API）
# - 通过 Agent.env 注入 GIT_PLATFORM_TOKEN / BASE_URL / REPO 等凭据
# - 自动注册 send_chat_message 和 git_platform 两个 Skill（若不存在）
#
# 前提：
# 1. DiOS backend 已启动并监听 http://localhost:8080
# 2. 已在 UI 添加至少一个 LLM Model（记下 name）
# 3. 已在 UI 添加 git_webhook Connector（配好 secret）—— 接收 GitHub webhook 用
#
# 用法：
#   MODEL_NAME=<llm-name> \
#   GIT_PLATFORM_TOKEN=<github_pat> \
#   GITHUB_REPO=<owner/repo> \
#   ./scripts/seed_codedev_scenario.sh
#
# 可选：GIT_PLATFORM_BASE_URL（默认 https://api.github.com），GIT_USER_NAME，GIT_USER_EMAIL

set -euo pipefail

BASE="${DIOS_API:-http://localhost:8080}"
MODEL_NAME="${MODEL_NAME:?请设置 MODEL_NAME（对应 DiOS 里 LLM Model 的 name）}"
GIT_PLATFORM_TOKEN="${GIT_PLATFORM_TOKEN:?请设置 GIT_PLATFORM_TOKEN（GitHub Personal Access Token，repo 权限）}"
GITHUB_REPO="${GITHUB_REPO:?请设置 GITHUB_REPO（格式 owner/repo）}"
GIT_PLATFORM_BASE_URL="${GIT_PLATFORM_BASE_URL:-https://api.github.com}"
GIT_USER_NAME="${GIT_USER_NAME:-dios-bot}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-bot@dios.local}"

# ── 工具函数 ──
json_get() { python3 -c "import sys, json; print(json.load(sys.stdin)$1)"; }

# ── Step 0: 注册 Skills（DB 中缺失时自动补） ──
echo "=== 注册 Skills 到 DB（若不存在） ==="
register_skill() {
  local name="$1" desc="$2" path="$3"
  local existing
  existing=$(curl -s "$BASE/api/os/skills" | python3 -c \
    "import sys, json; arr=json.load(sys.stdin); print(next((s['id'] for s in arr if s['name']=='$name'), ''))")
  if [[ -n "$existing" ]]; then
    echo "  skill $name 已存在（id=$existing），跳过"
    return
  fi
  local content=""
  if [[ -f "$path" ]]; then
    content=$(python3 -c "import json; print(json.dumps(open('$path').read()))")
  else
    content='""'
  fi
  curl -s -X POST "$BASE/api/os/skills" -H "Content-Type: application/json" \
    -d "{\"name\":\"$name\",\"description\":\"$desc\",\"content\":$content}" >/dev/null
  echo "  注册 skill: $name"
}

# 相对 repo 根目录
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
register_skill "send_chat_message" "Agent 主动投递消息到 Chat 会话" "$ROOT_DIR/workspace/skills/send_chat_message/SKILL.md"
register_skill "git_platform" "git + curl 调 Git 平台 REST API" "$ROOT_DIR/workspace/skills/git_platform/SKILL.md"
register_skill "dios_admin" "通过 dios CLI 受控配置 DiOS 资源" "$ROOT_DIR/workspace/skills/dios_admin/SKILL.md"

# ── 通用 env 块（供各 agent 共用） ──
common_env() {
  cat <<EOF
"env": {
  "GIT_PLATFORM_BASE_URL": "$GIT_PLATFORM_BASE_URL",
  "GIT_PLATFORM_TOKEN": "$GIT_PLATFORM_TOKEN",
  "GIT_REPO": "$GITHUB_REPO",
  "GIT_USER_NAME": "$GIT_USER_NAME",
  "GIT_USER_EMAIL": "$GIT_USER_EMAIL"
}
EOF
}

# ── Step 1: 主智能体（service） ──
echo "=== 创建主智能体 (service) ==="
MAIN_ID=$(curl -s -X POST "$BASE/api/os/agents" \
  -H "Content-Type: application/json" \
  -d "$(cat <<EOF
{
  "name": "Master",
  "mode": "service",
  "group": "代码开发",
  "role": "agent",
  "description": "主调度与系统配置入口：分解需求、驱动开发链路、必要时执行受控 DiOS 配置。",
  "model": "$MODEL_NAME",
  "system_prompt": "你是代码开发协作的主调度者（仓库：$GITHUB_REPO）。\n\n工作流：\n1. 用户在 Chat 发起需求时，参照 git_platform skill 的 REST 示例，用 curl 向 \$GIT_PLATFORM_BASE_URL/repos/\$GIT_REPO/issues 创建一个 Issue（title/body 清晰）。告知用户 Issue 链接后即结束本轮对话——开发智能体会自动接手。\n2. 当收到 git.pull_request.closed 且 merged=true 的事件时，说明对应 PR 已合并，请：\n   a) 通读 PR 的 title/body/commit 记录\n   b) 用 send_chat_message skill 调 POST http://backend:8000/api/apps/chat/sessions/{session_id}/deliveries，content 写一段简洁的完成通知；session_id 从事件 context_id 或 data 中获取。\n\n你不参与代码细节评审。不要把 GIT_PLATFORM_TOKEN 写入 Chat 或输出。",
  "skills": ["send_chat_message", "git_platform", "dios_admin"],
  "capabilities": {
    "dios_admin": {
      "enabled": true,
      "scopes": ["agents.*", "subscriptions.*", "connectors.*", "models.*", "mcp-servers.*", "skills.*", "events.read"],
      "risk_policy": {"high_risk": "hil_required"}
    }
  },
  "mcp_server_ids": [],
  $(common_env)
}
EOF
)" | json_get "['id']")
echo "  Master: $MAIN_ID"

# ── Step 2: 开发智能体（task） ──
echo "=== 创建开发智能体 (task) ==="
DEV_ID=$(curl -s -X POST "$BASE/api/os/agents" \
  -H "Content-Type: application/json" \
  -d "$(cat <<EOF
{
  "name": "CodeDev-Developer",
  "mode": "task",
  "group": "代码开发",
  "role": "agent",
  "description": "根据 Issue 或 review 意见做代码开发，提交 PR。",
  "model": "$MODEL_NAME",
  "system_prompt": "你是代码开发者（仓库：$GITHUB_REPO）。参照 git_platform skill 的命令模板。\n\n两种触发情形：\n1. 收到 git.issue.opened 事件：\n   - 从 data.issue.title/body 提取开发任务\n   - 用 https://x-access-token:\$GIT_PLATFORM_TOKEN@github.com/\$GIT_REPO.git clone 到 /workspace/repo\n   - 配置 git user.name/email（使用 \$GIT_USER_NAME / \$GIT_USER_EMAIL）\n   - 创建分支 issue-<number>\n   - 按需求改代码，commit（清晰的 message），push\n   - curl POST \$GIT_PLATFORM_BASE_URL/repos/\$GIT_REPO/pulls 创建 PR，body 里写 \"Closes #<number>\"\n\n2. 收到 git.pull_request_review.submitted 且 state=changes_requested：\n   - 从 data 读 pull_request.number 和 head.ref 分支\n   - 阅读 review comments（可用 curl 拉）\n   - checkout 对应分支，修改代码，commit push（会触发 synchronize，评审会再审）\n\n完成后若事件 data 中包含 session_id，调用 send_chat_message skill 给用户发送进度通知（包含 PR 链接或 commit）。\n最终在 task_result.md 输出：PR 编号、commit hash、变更摘要。永远不要把 GIT_PLATFORM_TOKEN 写入文件或消息。",
  "skills": ["git_platform", "send_chat_message"],
  "mcp_server_ids": [],
  $(common_env)
}
EOF
)" | json_get "['id']")
echo "  CodeDev-Developer: $DEV_ID"

# ── Step 3: 评审智能体（task） ──
echo "=== 创建评审智能体 (task) ==="
REV_ID=$(curl -s -X POST "$BASE/api/os/agents" \
  -H "Content-Type: application/json" \
  -d "$(cat <<EOF
{
  "name": "CodeDev-Reviewer",
  "mode": "task",
  "group": "代码开发",
  "role": "agent",
  "description": "审核 PR：通过则 approve+merge；不通过则 request_changes。",
  "model": "$MODEL_NAME",
  "system_prompt": "你是严格但务实的代码评审者（仓库：$GITHUB_REPO）。参照 git_platform skill。\n\n触发事件：git.pull_request.created 或 git.pull_request.synchronize\n\n流程：\n1. 从 data.pull_request.number 获取 PR 号\n2. curl GET \$GIT_PLATFORM_BASE_URL/repos/\$GIT_REPO/pulls/<num>/files 拉 diff\n3. 检查：是否满足 issue 需求、有无明显 bug / 安全隐患、命名与风格、是否需要测试\n4. 判定：\n   a) 通过：curl POST /pulls/<num>/reviews event=APPROVE，然后 curl PUT /pulls/<num>/merge（merge_method=squash）\n   b) 不通过：curl POST /pulls/<num>/reviews event=REQUEST_CHANGES，body 列出具体问题和期望改法\n\n完成后若事件 data 中包含 session_id，调用 send_chat_message skill 给用户发送评审结论（通过/不通过+原因）。\n禁止：改代码、改需求、无限循环审查同一 PR。task_result.md 输出审查结论。",
  "skills": ["git_platform", "send_chat_message"],
  "mcp_server_ids": [],
  $(common_env)
}
EOF
)" | json_get "['id']")
echo "  CodeDev-Reviewer: $REV_ID"

# ── Step 4: 订阅 ──
echo "=== 创建订阅 ==="

sub() {
  local agent_id="$1" body="$2" label="$3"
  curl -s -X POST "$BASE/api/os/agents/$agent_id/subscriptions" \
    -H "Content-Type: application/json" -d "$body" >/dev/null
  echo "  $label"
}

sub "$MAIN_ID" \
  '{"source_pattern":"github/*","event_types":["git.pull_request.closed"],"filter_rules":{"merged":true},"enabled":true}' \
  "主: pull_request.closed(merged=true)"

sub "$DEV_ID" \
  '{"source_pattern":"github/*","event_types":["git.issue.opened"],"enabled":true}' \
  "开发: issues.opened"

sub "$DEV_ID" \
  '{"source_pattern":"github/*","event_types":["git.pull_request.review_submitted"],"filter_rules":{"state":"changes_requested"},"enabled":true}' \
  "开发: pull_request_review(changes_requested)"

sub "$REV_ID" \
  '{"source_pattern":"github/*","event_types":["git.pull_request.created","git.pull_request.synchronize"],"enabled":true}' \
  "评审: pull_request.opened + synchronize"

echo ""
echo "=== 完成 ==="
echo "Agents:"
echo "  Main (service): $MAIN_ID"
echo "  Dev  (task):    $DEV_ID"
echo "  Rev  (task):    $REV_ID"
echo ""
echo "下一步："
echo "  1. 在 UI 的 Connectors 页面确保已有 git_webhook Connector（记下 secret）"
echo "  2. GitHub 仓库 $GITHUB_REPO 配置 webhook："
echo "     - URL = <DIOS 公网地址>/api/os/events/webhook/github"
echo "     - Secret = 上面 Connector 里的 secret"
echo "     - 事件勾选：Issues / Pull requests / Pull request reviews"
echo "  3. 在 Chat 里打开 CodeDev-Main，发起一个开发需求，观察流转"
