import hashlib,json,subprocess
from pathlib import Path
w=Path('/home/xu/workspace/NANA-OS/workspace/qualification-readonly-20261126');image=json.loads((w/'profile.json').read_text())['launch']['image'];(w/'positive').chmod(0o777);(w/'effects').chmod(0o777)
commands=[['python','-c',"import py_compile; py_compile.compile('/probe-source/sample.py',cfile='/probe-effects/sample.pyc',doraise=True)"],['node','/opt/npm/bin/npm-cli.js','--prefix','/probe-source','run','build']]
for cmd in commands:
 result=subprocess.run(['docker','run','--rm','--network','none','--read-only','--tmpfs','/tmp:rw,noexec,nosuid,size=64m','--user','65534:65534','--env','HOME=/tmp','--mount',f'type=bind,src={w}/source,dst=/probe-source,readonly','--mount',f'type=bind,src={w}/positive,dst=/probe-effects','--entrypoint',cmd[0],image,*cmd[1:]],capture_output=True,timeout=30)
 assert result.returncode==0,result.stderr.decode()
assert (w/'positive/sample.pyc').stat().st_size>0 and (w/'positive/package-ran').read_text()=='synthetic-package-script-ran'
node=subprocess.check_output(['docker','image','inspect','node:24-bookworm-slim','--format','{{.Id}}'],text=True).strip()
(w/'inputs.json').write_text(json.dumps({'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (w/'source').iterdir()},'image':image,'npm_source_image':node,'positive_controls':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (w/'positive').iterdir()}},indent=2));print('Two positive controls passed')
