"""Build SDK and independent application distributions; no publication or deployment."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile
import tomllib
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args()
    out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
    projects=['sdk/python','applications/knowledge-evidence','applications/knowledge-source','applications/static-review']
    for source in projects:
        subprocess.run([sys.executable,'-m','build','--no-isolation','--outdir',str(out),str(ROOT/source)],check=True)
    def built_wheel(source):
        metadata=tomllib.loads((ROOT/source/'pyproject.toml').read_text())['project']
        name=metadata['name'].replace('-','_');version=metadata['version']
        matches=list(out.glob(name+'-'+version+'-*.whl'))
        if len(matches)!=1:raise ValueError('Ambiguous wheel for '+source)
        return matches[0]
    sdk=built_wheel(projects[0])
    for source in projects[1:]:
        directory=ROOT/source
        app=built_wheel(source)
        metadata=tomllib.loads((directory/'pyproject.toml').read_text())['project']
        assert json.loads((directory/'application.json').read_text())['version']==metadata['version']
        with zipfile.ZipFile(out/(directory.name+'-bundle.zip'),'w',zipfile.ZIP_DEFLATED) as bundle:
            for name in ['Dockerfile','application.json','input-catalog.json']:
                if (directory/name).is_file():bundle.write(directory/name,name)
            for wheel in [sdk,app]:bundle.write(wheel,'wheels/'+wheel.name)
    files=[dict(name=x.name,sha256=hashlib.sha256(x.read_bytes()).hexdigest(),size=x.stat().st_size)
           for x in sorted(out.iterdir()) if x.suffix in {'.whl','.gz','.zip'}]
    (out/'checksums.json').write_text(json.dumps({'files':files},indent=2)+'\n')
    print(json.dumps({'distributions':len(files),'output':str(out)}))
if __name__=='__main__':main()
