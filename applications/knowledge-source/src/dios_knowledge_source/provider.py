"""Replaceable read-only knowledge provider; ACL enforced before ranking or output.

Operator-managed JSON is the lightweight local adapter. Retrieval identity comes
from a gateway credential, never from tool arguments. Customer workflows live
outside this provider. A version grant pins both content and provenance.
"""
import argparse
import hashlib
import json
import secrets
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dios_sdk import WorkloadError as RuntimeErrorCode
from dios_sdk import digest

INPUT_SCHEMA = {"type":"object","properties":{"query":{"type":"string","minLength":1,"maxLength":500}},"required":["query"],"additionalProperties":False}


def document(path, limit):
    raw=Path(path).read_bytes()
    if len(raw)>limit:raise RuntimeErrorCode('KNOWLEDGE_CONFIGURATION_INVALID')
    value=json.loads(raw.decode('utf-8'))
    if not isinstance(value,dict) or value.get('version')!=1:raise RuntimeErrorCode('KNOWLEDGE_CONFIGURATION_INVALID')
    return value


def version(row):
    return digest({k:v for k,v in row.items() if k!='digest'})


class FileKnowledgeProvider:
    def __init__(self,catalog,grants):self.catalog,self.grants=Path(catalog),Path(grants)

    def identity(self,token):
        if not isinstance(token,str) or not token:raise RuntimeErrorCode('KNOWLEDGE_ACCESS_DENIED')
        hashed=hashlib.sha256(token.encode()).hexdigest()
        rows=document(self.grants,131072).get('grants',[])
        matches=[r for r in rows if secrets.compare_digest(str(r.get('token_sha256','')),hashed)]
        if len(matches)!=1 or matches[0].get('enabled') is not True:raise RuntimeErrorCode('KNOWLEDGE_ACCESS_DENIED')
        grant=matches[0]
        if not isinstance(grant.get('documents'),dict) or not all(isinstance(grant.get(k),str) and grant[k] for k in ('subject','project_id')):
            raise RuntimeErrorCode('KNOWLEDGE_CONFIGURATION_INVALID')
        return grant

    def search(self,token,arguments):
        if (not isinstance(arguments,dict) or set(arguments)!={'query'} or
                not isinstance(arguments['query'],str) or not 1<=len(arguments['query'])<=500):
            raise RuntimeErrorCode('INVALID_TOOL_ARGUMENTS')
        grant=self.identity(token)
        rows=document(self.catalog,4*1024*1024).get('documents',[])
        if not isinstance(rows,list):raise RuntimeErrorCode('KNOWLEDGE_CONFIGURATION_INVALID')
        authorized=[];seen=set()
        for row in rows:
            # No titles, counts or snippets from unauthorized records leave this boundary.
            if row.get('project_id')!=grant['project_id'] or row.get('id') not in grant['documents']:continue
            if row['id'] in seen:raise RuntimeErrorCode('KNOWLEDGE_CONFIGURATION_INVALID')
            seen.add(row['id'])
            if row.get('active') is not True:continue
            if any(not isinstance(row.get(k),str) or not row[k] or len(row[k])>4000 for k in ('id','project_id','title','version','source','locator','updated_at')):
                raise RuntimeErrorCode('KNOWLEDGE_CONFIGURATION_INVALID')
            if not isinstance(row.get('text'),str) or not 0<len(row['text'].encode())<=8192:
                raise RuntimeErrorCode('KNOWLEDGE_CONFIGURATION_INVALID')
            if row.get('digest')!=version(row) or row['digest']!=grant['documents'][row['id']]:
                raise RuntimeErrorCode('KNOWLEDGE_VERSION_CHANGED')
            authorized.append(row)
        terms=arguments['query'].casefold().split()
        ranked=[]
        for row in authorized:
            hay=(row['title']+' '+row['text']).casefold()
            score=sum(t in hay for t in terms)
            if score:ranked.append((score,row))
        ranked.sort(key=lambda x:(-x[0],x[1]['id']))
        # Re-read revocable ACL before release, including changes to pinned versions.
        if grant!=self.identity(token):raise RuntimeErrorCode('KNOWLEDGE_ACCESS_DENIED')
        return {'project_id':grant['project_id'],'query':arguments['query'],
                'documents':[r for _,r in ranked[:3]],'truncated':len(ranked)>3,
                'retrieval':'literal-terms-v1'}


def handler(provider):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,value=None):
            raw=json.dumps(value,ensure_ascii=False).encode('utf-8') if value is not None else b''
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)))
            self.end_headers();self.wfile.write(raw)
        def do_POST(self):
            try:
                if self.path!='/mcp':self.reply(404);return
                auth=self.headers.get('Authorization','')
                token=auth[7:] if auth.startswith('Bearer ') else ''
                provider.identity(token)
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=65536:self.reply(413);return
                value=json.loads(self.rfile.read(size).decode('utf-8'))
                if not isinstance(value,dict) or value.get('jsonrpc')!='2.0':self.reply(400);return
                method=value.get('method')
                if method=='notifications/initialized' and 'id' not in value:self.reply(202);return
                if 'id' not in value:self.reply(400);return
                if method=='initialize':
                    result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'dios-knowledge','version':'1'}}
                elif method=='tools/list':
                    result={'tools':[{'name':'search','description':'Retrieve authorized source versions','inputSchema':INPUT_SCHEMA}]}
                elif method=='tools/call' and value.get('params',{}).get('name')=='search':
                    try:
                        found=provider.search(token,value['params'].get('arguments'))
                        result={'content':[{'type':'text','text':json.dumps(found,ensure_ascii=False)}],'isError':False}
                    except RuntimeErrorCode as exc:
                        result={'content':[{'type':'text','text':exc.code}],'isError':True}
                else:self.reply(400);return
                self.reply(200,{'jsonrpc':'2.0','id':value['id'],'result':result})
            except RuntimeErrorCode:self.reply(403)
            except (ValueError,TypeError,KeyError):self.reply(400)
            except Exception:self.reply(503)
    return Handler


def main():
    p=argparse.ArgumentParser();p.add_argument('--catalog',required=True);p.add_argument('--grants',required=True)
    p.add_argument('--port',type=int,default=13185);a=p.parse_args()
    ThreadingHTTPServer(('127.0.0.1',a.port),handler(FileKnowledgeProvider(a.catalog,a.grants))).serve_forever()

if __name__=='__main__':main()
