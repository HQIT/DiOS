"""Read-only evidence and rendered-component audit for the v1.1.44 bundle.

Writes a new QA ledger and contact sheet; does not run experiments or edit PDFs.
"""
import hashlib
import json
from pathlib import Path
import re

from PIL import Image, ImageChops, ImageDraw
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / 'docs/paper/revision-rr022'
RESULT = ROOT / 'experiments/e2ag/rr022/results'
QA = DOC / 'bundle-v1.1.44-qa-r4-20261007.json'
RENDER = ROOT / 'tmp/pdfs/bundle-v1.1.44'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
read = lambda p: json.loads(p.read_text(encoding='utf-8'))

assert not QA.exists(), 'Refuse to overwrite prior QA'
manifest = read(DOC / 'bundle-v1.1.44-manifest-r4-20261007.json')
assert sha(DOC / 'response-to-reviewers.md') == manifest['response_source_sha256']
current = (DOC / 'response-to-reviewers.md').read_text(encoding='utf-8')
old = (DOC / 'response-to-reviewers-v1.1.44-r3-archive.md').read_text(encoding='utf-8')
assert re.findall(r'^> (.+)$', current, re.M) == re.findall(r'^> (.+)$', old, re.M)
assert len(re.findall(r'^> (.+)$', current, re.M)) == 8
assert manifest['answer_paragraph_counts'] == {
    'R1.1': 2, 'R1.2': 2, 'R1.3': 1, 'R1.4': 2,
    'R2.1': 2, 'R2.2': 2, 'R2.3': 2, 'R2.4': 2,
}
assert manifest['all_response_paragraphs_verified']
old_answers = re.findall(r'作者答复（供作者确认，未提交）：(.*?)\n\n修改位置与证据：', old, re.S)
new_answers = re.findall(r'作者答复：(.*?)\n\n修改位置：', current, re.S)
assert len(old_answers) == len(new_answers) == 8
old_length = sum(len(re.sub(r'\s+', '', x)) for x in old_answers)
new_length = sum(len(re.sub(r'\s+', '', x)) for x in new_answers)
assert new_length < old_length
for banned in ('供作者确认', '未提交', '审阅草案', '不声称', '不宣称', '如实报告', '部分响应', '不能由结果一致自动推出'):
    assert banned not in current


check = read(RESULT / 'formal-holdout-check-20261007-v1.json')
assert check['status'] == 'passed' and not check['failures']
assert (check['objects'], check['main_runs'], check['diagnostic_runs'], check['ungated_requests']) == (11, 132, 18, 33)
assert sha(RESULT / 'formal-holdout-20261007-v1.json') == check['result_sha256']
assert sha(ROOT / 'experiments/e2ag/rr022/check_formal_holdout.py') == check['checker_sha256']
for method in ('e2ag', 'opa', 'agentspec_host'):
    assert check['summary']['main:' + method] == {
        'runs': 44, 'normal_completed': 11, 'rejected_variants': 33,
        'correct_reports': 11, 'incorrect_content_detected': 0,
    }
    assert check['summary']['diagnostic:' + method]['incorrect_content_detected'] == 3
freeze = read(RESULT / 'formal-execution-freeze-20261007-v1.json')
assert len(freeze['source_sha256']) == 174
# Later heterogeneity implementation changes do not retroactively change the
# recorded holdout execution. Verify its saved integrity result, not current code.
assert read(RESULT / 'formal-holdout-20261007-v1.json')['source_integrity_after'] is True
changed_since_holdout = [name for name, expected in freeze['source_sha256'].items()
                         if sha(ROOT / name) != expected]
hetero_path = ROOT / 'experiments/e2ag/heterogeneity/results/paired-matrix-completed-audit-20261007-v1.json'
hetero = read(hetero_path)
assert hetero['status'] == 'passed' and not hetero['failures']
assert hetero['case_count'] == len(hetero['cases']) == 42
from collections import Counter
assert dict(Counter(c['config'] for c in hetero['cases'])) == {
    'R0': 8, 'R1': 8, 'R2': 8, 'R3': 8, 'R4': 8, 'R5': 2}
assert all(not c['problems'] for c in hetero['cases'])
hfreeze = read(Path(hetero['source_freeze']))
assert len(hfreeze['source_sha256']) == 83
assert all(sha(ROOT / name) == expected for name, expected in hfreeze['source_sha256'].items())


relations = read(RESULT / 'evidence-relations-20261001-v2.json')
assert relations['passed'] and len(relations['semantic_counterexamples']) == 19
assert all(c['passed'] and c['result']['hash_valid'] and not c['result']['valid']
           for c in relations['semantic_counterexamples'])
assert len(relations['snapshots']) == 3
assert all(s['unchanged'] and sha(ROOT / s['database']) == s['sha256']
           and all(r['result']['valid'] for r in s['results'])
           for s in relations['snapshots'])

bundle = ROOT / manifest['output']
assert sha(bundle) == manifest['output_sha256']
merged = PdfReader(bundle)
assert len(merged.pages) == manifest['pages'] == 47
rendered = sorted((RENDER / 'merged-r4').glob('page-*.png'))
assert len(rendered) == 47
pixel_checks, text_checks, source_links, merged_links = [], [], [], []
render_dirs = [RENDER / 'response-r4', RENDER / 'marked',
               RENDER / 'clean']
for component, image_dir in zip(manifest['components'], render_dirs):
    path = ROOT / component['file']
    assert sha(path) == component['sha256']
    reader = PdfReader(path)
    images = sorted(image_dir.glob('page-*.png'))
    assert len(images) == len(reader.pages)
    offset = component['start_page'] - 1
    for local, page in enumerate(reader.pages):
        actual = merged.pages[offset + local]
        assert page.extract_text() == actual.extract_text()
        assert list(page.mediabox) == list(actual.mediabox)
        text_checks.append(offset + local + 1)
        a, b = Image.open(images[local]).convert('RGB'), Image.open(rendered[offset + local]).convert('RGB')
        assert a.size == b.size, (offset + local + 1, a.size, b.size)
        assert ImageChops.difference(a, b).getbbox() is None, f'Render mismatch on page {offset + local + 1}'
        pixel_checks.append(offset + local + 1)
        for ref in page.get('/Annots', []):
            action = ref.get_object().get('/A', {})
            if action.get('/S') == '/URI':
                source_links.append((offset + local, 'uri', str(action['/URI'])))
            else:
                assert action.get('/S') == '/GoTo'
                dest = reader.named_destinations[action['/D']]
                source_links.append((offset + local, 'goto', offset + reader.get_destination_page_number(dest)))
for number, page in enumerate(merged.pages):
    assert '\ufffd' not in page.extract_text()
    for ref in page.get('/Annots', []):
        action = ref.get_object().get('/A', {})
        if action.get('/S') == '/URI':
            merged_links.append((number, 'uri', str(action['/URI'])))
        else:
            assert action.get('/S') == '/GoTo'
            merged_links.append((number, 'goto', merged.get_page_number(action['/D'][0].get_object())))
assert source_links == merged_links
assert len(merged.outline) == 3
assert [merged.get_destination_page_number(x) + 1 for x in merged.outline] == [1, 4, 28]

# Contact sheets cover every component page. Full-resolution pages are retained.
contact_sheets = []
for kind in ('response-r4',):
    pages = sorted((RENDER / kind).glob('page-*.png'))
    for start in range(0, len(pages), 6):
        sheet = Image.new('RGB', (1800, 1730), 'white')
        draw = ImageDraw.Draw(sheet)
        for n, path in enumerate(pages[start:start + 6]):
            im = Image.open(path).convert('RGB')
            im.thumbnail((590, 825))
            x, y = (n % 3) * 600, (n // 3) * 865
            sheet.paste(im, (x, y + 25))
            draw.text((x + 10, y + 5), f'{kind} page {start + n + 1}', fill='black')
        target = RENDER / f'{kind}-contact-{start//6 + 1}.png'
        sheet.save(target)
        contact_sheets.append(str(target))

# Font embedding and text checks on all components.
import subprocess
font_checks = {}
for component in manifest['components']:
    path = ROOT / component['file']
    listing = subprocess.check_output(['pdffonts', str(path)], text=True)
    assert all('yes' in line for line in listing.splitlines()[2:] if line.strip())
    font_checks[path.name] = listing
clean = PdfReader(ROOT / manifest['components'][2]['file'])
clean_text = '\\n'.join(p.extract_text() for p in clean.pages)
assert not any(word in clean_text for word in ('本版', '本轮', '待独立复核', '正式运行中的启动标记'))
for word in ('42', 'PostgreSQL', 'AMQP', 'ASGI', '132'):
    assert word in clean_text

baseline = ROOT / 'output/pdf/E2AG-paper-authors-v1.1.39-20260817.pdf'
assert sha(baseline) == manifest['submitted_pdf_sha256']
report = {
    'status': 'automated_checks_passed_visual_review_pending',
    'response_source_sha256': manifest['response_source_sha256'],
    'reviewer_quotes_unchanged': 8,
    'reply_characters_before': old_length,
    'reply_characters_after': new_length,
    'answer_paragraphs_preserved': sum(manifest['answer_paragraph_counts'].values()),
    'formal_result_unchanged_and_counts_verified': True,
    'holdout_frozen_files': 174,
    'current_files_changed_since_holdout': changed_since_holdout,
    'heterogeneity_frozen_files_unchanged': 83,
    'heterogeneity_cases': 42,
    'heterogeneity_audit_sha256': sha(hetero_path),
    'contact_sheets': contact_sheets,
    'font_checks': font_checks,
    'historical_relation_checks': {'semantic_counterexamples': 19, 'unchanged_databases': 3},
    'text_and_page_geometry_preserved': text_checks,
    'identical_rendered_pages': pixel_checks,
    'internal_links': sum(x[1] == 'goto' for x in merged_links),
    'external_links': sum(x[1] == 'uri' for x in merged_links),
    'outline_start_pages': [1, 4, 28],
    'submitted_baseline_unchanged': True,
    'response_pdf_sha256': manifest['components'][0]['sha256'],
    'bundle_pdf_sha256': sha(bundle),
}
QA.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report, ensure_ascii=True, indent=2))
