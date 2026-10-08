"""Read-only source/result/PDF checks; write new QA outputs, never run experiments."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from PIL import Image, ImageChops, ImageDraw
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / 'docs/paper/revision-rr022'
TEX = ROOT / 'docs/paper/latex'
QA = DOC / 'bundle-v1.1.47-qa-20261008.json'
RENDER = ROOT / 'tmp/pdfs/v47'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
read = lambda p: json.loads(p.read_text(encoding='utf-8'))
assert not QA.exists()
manifest = read(DOC / 'bundle-v1.1.47-manifest-20261008.json')
old = (TEX / 'e2ag-body-v1.1.46.tex').read_text(encoding='utf-8')
new = (TEX / 'e2ag-body-v1.1.47.tex').read_text(encoding='utf-8')
unchanged = {}
for env in ('table', 'equation', 'align', 'algorithm'):
    pattern = r'\\begin\{' + env + r'\}.*?\\end\{' + env + r'\}'
    before, after = re.findall(pattern, old, re.S), re.findall(pattern, new, re.S)
    if env == 'table':
        before = [s for s in before if 'tab:exploratory-rr022' not in s]
    assert [s.replace('[htbp]', '[!htb]') for s in before] == [s.replace('[htbp]', '[!htb]') for s in after], env
    unchanged[env] = len(after)
assert old[old.index('\\begin{thebibliography}'): ] == new[new.index('\\begin{thebibliography}'): ]
assert re.findall(r'\\input\{fig-[^}]+\}', old) == re.findall(r'\\input\{fig-[^}]+\}', new)
current = (DOC / 'response-to-reviewers-v1.1.47.md').read_text(encoding='utf-8')
previous = (DOC / 'response-to-reviewers-v1.1.46-r2.md').read_text(encoding='utf-8')
assert re.findall(r'^> .+$', current, re.M) == re.findall(r'^> .+$', previous, re.M)
assert len(re.findall(r'^> .+$', current, re.M)) == 8
assert '三种状态' not in current
assert sha(DOC / 'response-to-reviewers-v1.1.47.md') == manifest['response_source_sha256']
assert current == (DOC / 'response-to-reviewers.md').read_text(encoding='utf-8')
for word in ('本版', '本轮', '不声称', '不宣称', '如实报告'):
    assert word not in new, word

results = ROOT / 'experiments/e2ag/rr022/results'
holdout = read(results / 'formal-holdout-check-20261007-v1.json')
assert holdout['status'] == 'passed' and not holdout['failures']
assert (holdout['objects'], holdout['main_runs'], holdout['diagnostic_runs']) == (11, 132, 18)
assert sha(results / 'formal-holdout-20261007-v1.json') == holdout['result_sha256']
hetero = read(ROOT / 'experiments/e2ag/heterogeneity/results/paired-matrix-completed-audit-20261007-v1.json')
assert hetero['status'] == 'passed' and not hetero['failures'] and hetero['case_count'] == 42
freeze = read(Path(hetero['source_freeze']))
assert all(sha(ROOT / p) == v for p, v in freeze['source_sha256'].items())
assert sha(ROOT / 'output/pdf/E2AG-paper-authors-v1.1.39-20260817.pdf') == manifest['submitted_pdf_sha256']

overhead = results/'overhead-server-20261008'
formal = read(overhead/'formal-v3/results.json')
analysis = read(overhead/'formal-analysis-v3.json')
assert formal['finished'] and formal['source_unchanged'] and len(formal['rows'])==40
assert all(r['qualification_expected'] and r['verification']['passed'] and not r['summary']['errors'] for r in formal['rows'])
assert sum(r['summary']['measured_attempts'] for r in formal['rows'])==103389
sys.path.insert(0,str(ROOT/'experiments/e2ag/rr022'))
from render_overhead_tables import render as render_tables
table_check = Path(tempfile.mkdtemp(prefix='tables-v47-',dir=ROOT/'tmp'))
render_tables(analysis,table_check)
for path in table_check.glob('*.tex'):
    assert path.read_bytes()==(TEX/path.name).read_bytes()
for r in formal['rows']:
    raw = read(overhead/'formal-v3'/Path(r['directory']).name/'client-raw.json')
    measured = [v for v in raw['records'] if v['phase']=='measurement']
    assert len(measured)==r['summary']['measured_attempts']
    assert all(v['success'] for v in measured)
    window = next(w for w in raw['windows'] if w['phase']=='measurement')
    cutoff = window['start_ns']+int(window['configured_seconds']*1e9)
    assert sum(v['end_ns']<=cutoff for v in measured)==r['summary']['completed_inside_window']
log = (ROOT/'output/pdf/E2AG-paper-authors-v1.1.47-working-20261008.log').read_text(encoding='utf-8',errors='replace')
assert not re.search(r'Overfull|undefined references|Reference .* undefined|Citation .* undefined|Missing character:',log)

components = manifest['components']
groups = [(name, ROOT / component['file']) for name, component in zip(('response', 'marked', 'clean'), components)]
groups.append(('merged', ROOT / manifest['output']))
def render(group):
    name, path = group
    directory = RENDER / name
    directory.mkdir(parents=True, exist_ok=False)
    subprocess.run(['pdftoppm', '-r', '85', '-png', str(path), str(directory / 'page')], check=True, capture_output=True)
    return sorted(directory.glob('page-*.png'))
with ThreadPoolExecutor(max_workers=3) as pool:
    rendered = dict(zip([g[0] for g in groups], pool.map(render, groups)))
merged = PdfReader(ROOT / manifest['output'])
assert len(merged.pages) == manifest['pages'] == len(rendered['merged'])
checks, links, font_checks, sheets = [], [], {}, []
for (name, path), component in zip(groups, components):
    assert sha(path) == component['sha256']
    reader = PdfReader(path)
    assert len(reader.pages) == len(rendered[name])
    offset = component['start_page'] - 1
    for i, page in enumerate(reader.pages):
        assert page.extract_text() == merged.pages[offset + i].extract_text()
        assert '\ufffd' not in page.extract_text()
        a, b = Image.open(rendered[name][i]).convert('RGB'), Image.open(rendered['merged'][offset+i]).convert('RGB')
        assert a.size == b.size and ImageChops.difference(a,b).getbbox() is None
        checks.append(offset+i+1)
        for ref in page.get('/Annots', []):
            action = ref.get_object().get('/A', {})
            if action.get('/S') == '/URI':
                links.append((offset+i, 'uri', str(action['/URI'])))
            else:
                assert action.get('/S') == '/GoTo'
                target = reader.named_destinations[action['/D']]
                links.append((offset+i, 'goto', offset+reader.get_destination_page_number(target)))
    fonts = subprocess.check_output(['pdffonts', str(path)], text=True)
    assert all('yes' in line for line in fonts.splitlines()[2:] if line.strip())
    font_checks[name] = fonts
    for start in range(0, len(rendered[name]), 6):
        sheet = Image.new('RGB', (1800, 1760), 'white')
        draw = ImageDraw.Draw(sheet)
        for j,p in enumerate(rendered[name][start:start+6]):
            im = Image.open(p).convert('RGB'); im.thumbnail((590, 840))
            x,y = j%3*600, j//3*880
            sheet.paste(im,(x,y+25)); draw.text((x+10,y+5),f'{name} {start+j+1}',fill='black')
        target=RENDER/f'{name}-contact-{start//6+1}.png'; sheet.save(target); sheets.append(str(target))
actual=[]
for i,page in enumerate(merged.pages):
    for ref in page.get('/Annots',[]):
        action=ref.get_object().get('/A',{})
        if action.get('/S')=='/URI': actual.append((i,'uri',str(action['/URI'])))
        else:
            assert action.get('/S')=='/GoTo'
            actual.append((i,'goto',merged.get_page_number(action['/D'][0].get_object())))
assert links==actual
assert [merged.get_destination_page_number(x)+1 for x in merged.outline] == [c['start_page'] for c in components]
report={'status':'automated_checks_passed_visual_review_pending','unchanged_source_blocks':unchanged,
        'formal_cells_verified':40,'formal_measured_successes':103389,'generated_tables_match_statistics':True,
        'formal_source_unchanged':True,'overhead_analysis_sha256':sha(overhead/'formal-analysis-v3.json'),
        'references_and_figure_inputs_unchanged':True,'reviewer_quotes_unchanged':8,
        'historical_holdout_result_hash_verified':True,'heterogeneity_source_files_unchanged':len(freeze['source_sha256']),
        'submitted_baseline_unchanged':True,'text_and_pixels_preserved':checks,'internal_links':sum(x[1]=='goto' for x in links),
        'external_links':sum(x[1]=='uri' for x in links),'fonts':font_checks,'contact_sheets':sheets}
QA.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='fonts'},ensure_ascii=True,indent=2))
