#!/usr/bin/env bash
# 配置 AI4R 场景（仅配置现有 Agent，不创建）
# - 统一通过 dios cli 操作（不直接 curl）
# - 配置 Writer / Engineer / Reviewer 的 system_prompt 与订阅
# - 订阅按 ai4r/* 幂等收敛

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${DIOS_API:-http://localhost:8080}"
DIOS_CLI_PATH="${DIOS_CLI_PATH:-$ROOT_DIR/cli/dios}"

WRITER_NAME="${WRITER_NAME:-Writer}"
ENGINEER_NAME="${ENGINEER_NAME:-Engineer}"
REVIEWER_NAME="${REVIEWER_NAME:-Reviewer}"
AI4R_SOURCE_PATTERN="${AI4R_SOURCE_PATTERN:-ai4r/*}"

dios_request() {
  local method="$1"
  local path="$2"
  local data="${3:-}"

  local out=""
  local code=0
  if [[ -n "$data" ]]; then
    set +e
    out=$(python3 "$DIOS_CLI_PATH" request --method "$method" --path "$path" --base-url "$BASE_URL" --data "$data" 2>&1)
    code=$?
    set -e
  else
    set +e
    out=$(python3 "$DIOS_CLI_PATH" request --method "$method" --path "$path" --base-url "$BASE_URL" 2>&1)
    code=$?
    set -e
  fi

  # 高风险动作：自动执行 token-only 二次确认
  if [[ "$code" -eq 3 ]]; then
    local token
    token="$(python3 -c 'import json,sys;
text=sys.stdin.read().strip();
tok="";
if text:
    try:
        payload=json.loads(text);
        tok=(payload.get("confirm_token") or payload.get("token") or "");
    except Exception:
        tok="";
sys.stdout.write(tok)' <<<"$out")"
    if [[ -z "$token" ]]; then
      echo "ERROR: pending_hil but token missing for $method $path" >&2
      echo "$out" >&2
      return 1
    fi
    set +e
    out=$(python3 "$DIOS_CLI_PATH" request --confirm-token "$token" --base-url "$BASE_URL" 2>&1)
    code=$?
    set -e
  fi

  if [[ "$code" -ne 0 ]]; then
    echo "ERROR: dios request failed: $method $path" >&2
    echo "$out" >&2
    return "$code"
  fi

  printf '%s\n' "$out"
}

get_agent_id_by_name() {
  local name="$1"
  local agents_json
  agents_json="$(dios_request GET /api/os/agents)"
  AGENTS_JSON="$agents_json" AGENT_NAME="$name" python3 -c 'import json, os;
arr=json.loads(os.environ.get("AGENTS_JSON") or "[]");
name=os.environ.get("AGENT_NAME") or "";
for item in arr:
    if item.get("name")==name:
        print(item.get("id","")); raise SystemExit(0)
print("")'
}

update_prompt() {
  local agent_id="$1"
  local prompt="$2"
  local payload
  payload=$(python3 - <<PY
import json
print(json.dumps({"system_prompt": ${prompt@Q}}, ensure_ascii=False))
PY
)
  dios_request PUT "/api/os/agents/$agent_id" "$payload" >/dev/null
}

update_reasoning_caps() {
  local agent_id="$1"
  local payload
  payload=$(python3 - <<PY
import json
print(json.dumps({
  "capabilities": {
    "reasoning": {
      "recursion_limit": 120,
      "max_tool_rounds": 20
    }
  }
}, ensure_ascii=False))
PY
)
  dios_request PUT "/api/os/agents/$agent_id" "$payload" >/dev/null
}

upsert_ai4r_subscription() {
  local agent_id="$1"
  local event_types_json="$2"

  local subs_json
  subs_json="$(dios_request GET "/api/os/agents/$agent_id/subscriptions")"

  local plan_json
  plan_json=$(python3 - <<PY
import json
subs = json.loads(${subs_json@Q})
desired = sorted(json.loads(${event_types_json@Q}))
source = ${AI4R_SOURCE_PATTERN@Q}

keep = None
stale = []
for s in subs:
    if s.get("source_pattern") != source:
        continue
    et = sorted(s.get("event_types") or [])
    if keep is None and et == desired:
        keep = s["id"]
    elif keep is None:
        keep = s["id"]
    else:
        stale.append(s["id"])

print(json.dumps({"keep": keep, "stale": stale}, ensure_ascii=False))
PY
)

  local keep_id
  keep_id=$(printf '%s' "$plan_json" | python3 -c "import sys,json; print((json.load(sys.stdin).get('keep') or ''))")

  local stale_ids
  stale_ids=$(printf '%s' "$plan_json" | python3 -c "import sys,json; print(' '.join(json.load(sys.stdin).get('stale') or []))")

  local payload
  payload=$(python3 - <<PY
import json
print(json.dumps({
    "source_pattern": ${AI4R_SOURCE_PATTERN@Q},
    "event_types": json.loads(${event_types_json@Q}),
    "filter_rules": {},
    "enabled": True
}, ensure_ascii=False))
PY
)

  if [[ -z "$keep_id" ]]; then
    dios_request POST "/api/os/agents/$agent_id/subscriptions" "$payload" >/dev/null
  else
    dios_request PUT "/api/os/agents/$agent_id/subscriptions/$keep_id" "$payload" >/dev/null
  fi

  if [[ -n "$stale_ids" ]]; then
    for sid in $stale_ids; do
      dios_request DELETE "/api/os/agents/$agent_id/subscriptions/$sid" >/dev/null
    done
  fi
}

echo "=== 配置 AI4R 场景（dios cli） ==="
echo "Base URL: $BASE_URL"

WRITER_ID="$(get_agent_id_by_name "$WRITER_NAME")"
ENGINEER_ID="$(get_agent_id_by_name "$ENGINEER_NAME")"
REVIEWER_ID="$(get_agent_id_by_name "$REVIEWER_NAME")"

if [[ -z "$WRITER_ID" || -z "$ENGINEER_ID" || -z "$REVIEWER_ID" ]]; then
  echo "ERROR: 缺少目标 Agent，请先创建后再配置。" >&2
  echo "Writer($WRITER_NAME): ${WRITER_ID:-<missing>}" >&2
  echo "Engineer($ENGINEER_NAME): ${ENGINEER_ID:-<missing>}" >&2
  echo "Reviewer($REVIEWER_NAME): ${REVIEWER_ID:-<missing>}" >&2
  exit 1
fi

WRITER_PROMPT='你是 AI4R Writer（事件驱动执行者）。请先从输入中识别事件类型，然后只做该事件要求的一步，并在最后停止，不要循环思考。\n- 若事件是 ai4r.topic.proposed：生成简短选题结构到 /workspace/projects/<project_id>/manuscript/topic_structure.md，然后调用 publish_event 发布 ai4r.experiment.requested。\n- 若事件是 ai4r.experiment.completed：整合实验结果并输出 draft.md，然后调用 publish_event 发布 ai4r.draft.submitted。\n- 若事件是 ai4r.review.completed 且 decision=changes_requested：修订 draft.md，然后调用 publish_event 再次发布 ai4r.draft.submitted。\n要求：每次任务最多调用一次 publish_event；发布后在最终答复里写“事件已发布: <event_type>”，然后结束。'
ENGINEER_PROMPT='你是 AI4R Engineer（事件驱动执行者）。仅处理 ai4r.experiment.requested：产出一个最小实验结果文件到 /workspace/projects/<project_id>/experiments/result.md（含 artifact_refs 与 version），然后调用 publish_event 发布 ai4r.experiment.completed。要求：每次任务最多调用一次 publish_event；发布后立即结束。'
REVIEWER_PROMPT='你是 AI4R Reviewer（事件驱动执行者）。仅处理 ai4r.draft.submitted：给出简短评审结论并写入 /workspace/projects/<project_id>/manuscript/review.md，然后调用 publish_event 发布 ai4r.review.completed（decision 取 approved 或 changes_requested）。要求：每次任务最多调用一次 publish_event；发布后立即结束。'

echo "=== 更新 system_prompt ==="
update_prompt "$WRITER_ID" "$WRITER_PROMPT"
update_prompt "$ENGINEER_ID" "$ENGINEER_PROMPT"
update_prompt "$REVIEWER_ID" "$REVIEWER_PROMPT"
echo "  Writer/Engineer/Reviewer prompts 已更新"

echo "=== 更新 reasoning 能力参数 ==="
update_reasoning_caps "$WRITER_ID"
update_reasoning_caps "$ENGINEER_ID"
update_reasoning_caps "$REVIEWER_ID"
echo "  Writer/Engineer/Reviewer reasoning 已更新（recursion_limit=120, max_tool_rounds=20）"

echo "=== 配置订阅（幂等） ==="
upsert_ai4r_subscription "$WRITER_ID" '["ai4r.topic.proposed","ai4r.experiment.completed","ai4r.review.completed"]'
upsert_ai4r_subscription "$ENGINEER_ID" '["ai4r.experiment.requested"]'
upsert_ai4r_subscription "$REVIEWER_ID" '["ai4r.draft.submitted"]'

echo ""
echo "=== 完成 ==="
echo "Writer:   $WRITER_ID ($WRITER_NAME)"
echo "Engineer: $ENGINEER_ID ($ENGINEER_NAME)"
echo "Reviewer: $REVIEWER_ID ($REVIEWER_NAME)"
echo "Source:   $AI4R_SOURCE_PATTERN"
