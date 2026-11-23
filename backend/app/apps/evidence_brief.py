"""Reference L3 application: collect authorized evidence for human review.

No customer rules or model dependency. Called by a registered Worker profile;
knowledge credentials stay in the E2AG gateway. No business approval is implied.
"""
import asyncio
import json
import os
import uuid
from app.runtime.database import RuntimeErrorCode
from app.runtime.store import digest
from app.runtime.tool_channel_client import ToolChannelClient
from app.runtime.acp_bridge import emit
from app.services.knowledge import version

KIND='knowledge-evidence'


def validate_input(value):
    fields={'id','name','filename','source','sha256','task_kind','project_id','query','tool'}
    if not isinstance(value,dict) or set(value)!=fields:raise RuntimeErrorCode('INVALID_WORK_INPUT')
    if any(not isinstance(v,str) or not v or len(v)>2000 for v in value.values()):raise RuntimeErrorCode('INVALID_WORK_INPUT')
    if value['task_kind']!=KIND or value['sha256']!=digest({k:v for k,v in value.items() if k!='sha256'}):
        raise RuntimeErrorCode('INPUT_DIGEST_MISMATCH')
    return value


def markdown_text(value):
    # Treat provider metadata and source text as data; no remote image/link loads.
    return ''.join(('\\'+c) if c in '\\`*_{}[]<>()!#|' else c for c in value)


def evidence_result(value,response,goal):
    validate_input(value)
    try:
        if response.get('isError') is not False:raise ValueError()
        content=response['content']
        if len(content)!=1 or content[0]['type']!='text':raise ValueError()
        data=json.loads(content[0]['text'])
        if data['project_id']!=value['project_id'] or data['query']!=value['query']:raise ValueError()
        rows=data['documents']
        if not isinstance(rows,list) or not 1<=len(rows)<=3:raise ValueError()
        citations=[];sections=['# 资料依据与待确认事项',markdown_text(goal),'状态：待业务人员复核','## 已取得的资料依据'];seen=set()
        for row in rows:
            if row['project_id']!=value['project_id'] or row['active'] is not True or row['digest']!=version(row) or row['id'] in seen:raise ValueError()
            seen.add(row['id'])
            for k in ('id','title','version','source','locator','updated_at','text','digest'):
                if not isinstance(row[k],str) or not row[k]:raise ValueError()
            if len(row['text'].encode())>8192:raise ValueError()
            citations.append({k:row[k] for k in ('id','project_id','title','version','source','locator','updated_at','digest','text')})
            # Quote source text so it is visibly evidence, not an application instruction.
            sections.extend([f'### {len(citations)}. {markdown_text(row["title"])}',
                f'来源：{markdown_text(row["source"])} · {markdown_text(row["locator"])}',f'版本：{markdown_text(row["version"])} · 更新：{markdown_text(row["updated_at"])}',
                f'内容与来源指纹：`{row["digest"]}`','> '+markdown_text(row['text']).replace('\n','\n> ')])
        sections.extend(['## 待业务人员确认','确认资料适用范围、当前有效性和缺失信息后，再作业务判断。',
                         '本成果为资料摘录；配方、业务结论和后续操作由上层应用与业务人员确认。'])
        result=dict(summary=f'已取得 {len(rows)} 份授权资料，待业务复核',citations=citations,
                    evidence=[r['id']+'@'+r['digest'] for r in rows],markdown='\n\n'.join(sections),
                    review_status='pending_review',validation='source-versions-checked',retrieval=data['retrieval'],
                    truncated=bool(data['truncated']),project_id=value['project_id'])
        if len(json.dumps(result,ensure_ascii=False).encode())>60000:raise ValueError()
        return result
    except (KeyError,ValueError,TypeError):raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT') from None


async def execute(payload,client):
    value=validate_input(payload.get('sample'))
    goal=payload.get('goal')
    if not isinstance(goal,str) or not 1<=len(goal)<=2000:raise RuntimeErrorCode('INVALID_WORK_INPUT')
    sid='evidence-'+str(uuid.uuid4());emit({'type':'session','session_id':sid});await client.ready(sid)
    emit({'type':'output','text':'等待资料检索授权，取得来源与版本后生成核查成果。'})
    arguments={'query':value['query']}
    response=await client.call(value['tool'],arguments,'evidence-search')
    await client.request('verify_required',{'requirements':[{'tool':value['tool'],'arguments':arguments}]})
    result=evidence_result(value,response,goal)
    emit({'type':'result','session_id':sid,'result':result})
    return result


def main():
    try:
        raw=os.environ['DIOS_WORK_INPUT']
        if len(raw.encode())>16384:raise RuntimeErrorCode('INVALID_WORK_INPUT')
        asyncio.run(execute(json.loads(raw),ToolChannelClient(os.environ['DIOS_TOOL_SOCKET'])))
        return 0
    except Exception as exc:
        from app.runtime.faults import CATEGORIES
        code=getattr(exc,'code','EVIDENCE_INSUFFICIENT')
        emit({'type':'error','code':code if code in CATEGORIES else 'EVIDENCE_INSUFFICIENT'});return 2

if __name__=='__main__':raise SystemExit(main())
