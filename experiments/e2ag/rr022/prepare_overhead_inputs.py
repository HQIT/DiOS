"""Stage public workload snapshots and an allowlisted source archive for testing."""
import argparse
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
from performance_pretrial import development_input
from run_paired_tool_pilot import ROOT, HERE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with tarfile.open(args.output, 'w:gz') as tar:
        def add_bytes(name, raw):
            info = tarfile.TarInfo(name)
            info.size = len(raw); info.mode = 0o644
            tar.addfile(info, io.BytesIO(raw))
            manifest[name] = hashlib.sha256(raw).hexdigest()
        for folder in [ROOT / 'backend/app', HERE]:
            for file in sorted(folder.glob('**/*.py') if folder.name == 'app' else folder.glob('*.py')):
                add_bytes(file.relative_to(ROOT).as_posix(), file.read_bytes())
        for kind in ['issue', 'pr']:
            job, example = development_input(kind)
            job = copy.deepcopy(job)
            raw = Path(job['catalog'][0]['cache_path']).read_bytes()
            add_bytes('inputs/' + kind + '.json', raw)
            for item in job['catalog']:
                item['cache_path'] = '/work/inputs/' + kind + '.json'
            add_bytes('inputs/' + kind + '-job.json', json.dumps({'job': job, 'example': example}, ensure_ascii=True).encode())
        add_bytes('manifest.json', json.dumps(manifest, sort_keys=True, indent=2).encode())
    print(json.dumps({'archive': str(args.output), 'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(),
                      'files': len(manifest), 'bytes': args.output.stat().st_size}))


if __name__ == '__main__':
    main()
