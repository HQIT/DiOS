"""Artifact and run-accounting QA for the October 7 revision."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
from PIL import Image, ImageDraw
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'experiments/e2ag/rr022/results'
OUT = ROOT / 'docs/paper/revision-rr022/revision-v1.1.43-qa-final-20261007.json'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
read = lambda p: json.loads(p.read_text(encoding='utf-8'))

result = read(RESULTS / 'formal-holdout-20261007-v1.json')
check = read(RESULTS / 'formal-holdout-check-20261007-v1.json')
assert check['status'] == 'passed' and result['source_integrity_after'] is True
assert sha(RESULTS / 'formal-holdout-20261007-v1.json') == check['result_sha256']
objects = read(RESULTS / 'holdout-inputs-20260930-v1.json')['objects']
expected_controls = {(o['object_id'], c) for o in objects for c in ('other_read_object', 'wrong_read_version', 'other_write_object')}
assert {(r['object_id'], r['case']) for r in result['ungated']} == expected_controls
ids = [t['id'] for r in result['rows'] for t in r['observed']['tasks']]
assert len(set(ids)) == len(ids) == 141
summary = {}
for obj in objects:
    rows = [r for r in result['rows'] if r['object_id'] == obj['object_id'] and r['group'] == 'main']
    assert len(rows) == 12
    key = obj['selection']['repository'] + ':' + obj['target']['kind']
    counts = summary.setdefault(key, Counter())
    counts.update(objects=1, runs=len(rows), normal_reports=sum(len(r['observed']['reports']) for r in rows if r['case'] == 'normal'))
pdfs = []
for kind, name in [('clean', 'E2AG-paper-authors-v1.1.43-working-20261007.pdf'),
                   ('marked', 'E2AG-paper-authors-v1.1.43-marked-from-v1.1.39-20261007.pdf')]:
    path = ROOT / 'output/pdf' / name
    pdf = PdfReader(path)
    texts = [p.extract_text() for p in pdf.pages]
    text = '\n'.join(texts)
    assert '\ufffd' not in text
    assert all(len(t.strip()) > 150 for t in texts)
    for word in ('132', '65536', 'ArcadeDB', 'Kibana', 'GH Archive'):
        assert word in text
    if kind == 'clean':
        assert not any(word in text for word in ('本版', '本轮', '待独立复核', '装饰性符号', '围绕脚本数量'))
    pages = sorted((ROOT / 'tmp/pdfs/e2ag-v1.1.43-final' / kind).glob('page-*.png'))
    assert len(pages) == len(pdf.pages)
    sheets = []
    for start in range(0, len(pages), 12):
        canvas = Image.new('RGB', (1080, 2040), 'white')
        draw = ImageDraw.Draw(canvas)
        for n, page in enumerate(pages[start:start + 12]):
            im = Image.open(page).convert('RGB')
            im.thumbnail((352, 485))
            x, y = (n % 3) * 360, (n // 3) * 510
            canvas.paste(im, (x + (360 - im.width) // 2, y + 22))
            draw.text((x + 10, y + 4), f'{kind} page {start+n+1}', fill='black')
        output = ROOT / 'tmp/pdfs/e2ag-v1.1.43-final' / f'{kind}-contact-{start//12+1}.png'
        canvas.save(output)
        sheets.append(str(output))
    pdfs.append(dict(path=str(path.relative_to(ROOT)), sha256=sha(path), pages=len(pdf.pages),
                     replacement_characters=0, cjk_characters=len(re.findall('[\u4e00-\u9fff]', text)), contact_sheets=sheets,
                     section_pages={term:[n for n,t in enumerate(texts,1) if term in t] for term in ('6.5.6', '6.6.3', '6.7', '7.1', '表13', '表16')}))
baseline = ROOT / 'output/pdf/E2AG-paper-authors-v1.1.39-20260817.pdf'
assert sha(baseline) == '1a9228c691bf4e89b4b2e2d61ec47c9efbad822c113154bb17acbde7476b82be'
source = ROOT / 'docs/paper/latex/e2ag-body-v1.1.43.tex'
audit = subprocess.check_output([str(ROOT / 'backend/.venv/Scripts/python.exe'), '-X', 'utf8',
    'C:/Users/sugar/.codex/skills/revise-systems-paper/scripts/audit_latex_prose.py', str(source), '--json'], encoding='utf-8')
output = dict(status='automated_checks_passed_visual_review_pending', result_sha256=check['result_sha256'],
              strata={k:dict(v) for k,v in summary.items()}, unique_task_ids=len(ids), unique_snapshots=len({o['snapshot']['content_sha256'] for o in objects}),
              pdfs=pdfs, baseline_unchanged=True, prose_candidates=json.loads(audit))
assert not OUT.exists()
OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(output, ensure_ascii=True, indent=2))
