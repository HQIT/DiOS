"""Read-only MCP source service for isolated Cursor qualification samples."""
import argparse
import hashlib
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--port',type=int,default=13184)
    args=parser.parse_args();root=Path(args.root).resolve()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=65536:self.send_error(413);return
            value=json.loads(self.rfile.read(size))
            if 'id' not in value:self.send_response(202);self.end_headers();return
            if value.get('method')=='initialize':
                result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'readonly-review-samples','version':'1'}}
            elif value.get('method')=='tools/call' and value.get('params',{}).get('name')=='search':
                query=value['params'].get('arguments',{}).get('query');found=None
                for name in ('first-item.py','average.py'):
                    path=root/name
                    if path.is_symlink() or not path.is_file() or path.stat().st_size>65536:continue
                    raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest()
                    if sha==query:found={'filename':'sample.py','sha256':sha,'source':raw.decode('utf-8')};break
                result={'content':[{'type':'text','text':json.dumps(found,ensure_ascii=False) if found else 'SOURCE_NOT_FOUND'}],'isError':found is None}
            else:
                self.send_error(400);return
            raw=json.dumps({'jsonrpc':'2.0','id':value['id'],'result':result},ensure_ascii=False).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()

if __name__=='__main__':main()
