"""Read-only evidence and rendered-component audit for the v1.1.43 bundle.

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
QA = DOC / 'bundle-v1.1.43-qa-20261007.json'
RENDER = ROOT / 'tmp/pdfs/bundle-v1.1.43'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
read = lambda p: json.loads(p.read_text(encoding='utf-8'))

assert not QA.exists(), 'Refuse to overwrite prior QA'
manifest = read(DOC / 'bundle-v1.1.43-manifest-20261007.json')
assert sha(DOC / 'response-to-reviewers.md') == manifest['response_source_sha256']
current = (DOC / 'response-to-reviewers.md').read_text(encoding='utf-8')
old = (DOC / 'response-to-reviewers-v1.1.42-archived-20261007.md').read_text(encoding='utf-8')
assert re.findall(r'^> (.+)$', current, re.M) == re.findall(r'^> (.+)$', old, re.M)
assert len(re.findall(r'^> (.+)$', current, re.M)) == 8
assert manifest['answer_paragraph_counts'] == {
    'R1.1': 1, 'R1.2': 3, 'R1.3': 1, 'R1.4': 2,
    'R2.1': 1, 'R2.2': 1, 'R2.3': 2, 'R2.4': 1,
}
assert manifest['all_response_paragraphs_verified']

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
assert all(sha(ROOT / name) == expected for name, expected in freeze['source_sha256'].items())

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
assert len(merged.pages) == manifest['pages'] == 44
rendered = sorted((RENDER / 'merged').glob('page-*.png'))
assert len(rendered) == 44
pixel_checks, text_checks, source_links, merged_links = [], [], [], []
render_dirs = [RENDER / 'response', ROOT / 'tmp/pdfs/e2ag-v1.1.43-final/marked',
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
assert [merged.get_destination_page_number(x) + 1 for x in merged.outline] == [1, 4, 26]

sheet = Image.new('RGB', (1500, 740), 'white')
draw = ImageDraw.Draw(sheet)
for n, path in enumerate(sorted((RENDER / 'response').glob('page-*.png'))):
    im = Image.open(path).convert('RGB')
    im.thumbnail((490, 700))
    sheet.paste(im, (n * 500, 30))
    draw.text((n * 500 + 10, 10), f'Response page {n + 1}', fill='black')
sheet.save(RENDER / 'response-contact.png')

baseline = ROOT / 'output/pdf/E2AG-paper-authors-v1.1.39-20260817.pdf'
assert sha(baseline) == manifest['submitted_pdf_sha256']
report = {
    'status': 'automated_checks_passed_visual_review_pending',
    'response_source_sha256': manifest['response_source_sha256'],
    'reviewer_quotes_unchanged': 8,
    'answer_paragraphs_preserved': sum(manifest['answer_paragraph_counts'].values()),
    'formal_result_unchanged_and_counts_verified': True,
    'frozen_source_files_unchanged': 174,
    'historical_relation_checks': {'semantic_counterexamples': 19, 'unchanged_databases': 3},
    'text_and_page_geometry_preserved': text_checks,
    'identical_rendered_pages': pixel_checks,
    'internal_links': sum(x[1] == 'goto' for x in merged_links),
    'external_links': sum(x[1] == 'uri' for x in merged_links),
    'outline_start_pages': [1, 4, 26],
    'submitted_baseline_unchanged': True,
    'response_pdf_sha256': manifest['components'][0]['sha256'],
    'bundle_pdf_sha256': sha(bundle),
}
QA.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report, ensure_ascii=True, indent=2))
