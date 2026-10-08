"""Read-only source/evidence checks before final performance-result integration.

Does not certify citation truth, PDF layout, or completeness of reviewer responses.
Writes a new audit artifact only; refuses existing output files.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(output):
    paper = ROOT/'docs/paper/latex/e2ag-body-v1.1.47.tex'
    previous = paper.with_name('e2ag-body-v1.1.46.tex')
    response = ROOT/'docs/paper/revision-rr022/response-to-reviewers.md'
    text, old = paper.read_text(encoding='utf-8'), previous.read_text(encoding='utf-8')
    expanded = text
    for relative in re.findall(r'\\input\{([^}]+)\}', text):
        expanded += '\n' + (paper.parent/relative).read_text(encoding='utf-8')
    labels = re.findall(r'\\label\{([^}]+)\}', expanded)
    refs = re.findall(r'\\(?:eqref|ref)\{([^}]+)\}', expanded)
    bib = re.findall(r'\\bibitem\{([^}]+)\}', text)
    citations = [key.strip() for group in re.findall(r'\\cite\{([^}]+)\}', text) for key in group.split(',')]
    body = response.read_text(encoding='utf-8')
    sections = re.findall(r'^### (R[12]\.[1-4])[^\n]*\n(.*?)(?=^### |^## |\Z)', body, re.M|re.S)
    holdout = ROOT/'experiments/e2ag/rr022/results/formal-holdout-20261007-v1.json'
    checked = holdout.with_name('formal-holdout-check-20261007-v1.json')
    hetero = ROOT/'experiments/e2ag/heterogeneity/results/paired-matrix-completed-audit-20261007-v1.json'
    h = json.loads(checked.read_text(encoding='utf-8'))
    m = json.loads(hetero.read_text(encoding='utf-8'))
    checks = {
        'eight_ordered_responses': [key for key,_ in sections] == [f'R{r}.{n}' for r in [1,2] for n in range(1,5)],
        'each_response_has_quote_answer_location': all(all(x in value for x in ['> ', '作者答复：', '修改位置：']) for _,value in sections),
        'no_duplicate_labels': all(v==1 for v in Counter(labels).values()),
        'all_cross_references_defined': not set(refs)-set(labels),
        'all_citation_keys_defined': not set(citations)-set(bib),
        'bibliography_unchanged': text.split('\\begin{thebibliography}',1)[1] == old.split('\\begin{thebibliography}',1)[1],
        'no_revision_narrative_patterns': not re.search(r'本版|本轮|扩展前后|修复后|最初采用|早期消融', text),
        'holdout_result_matches_checked_hash': digest(holdout)==h['result_sha256'],
        'holdout_evidence_passed': h['status']=='passed' and not h['failures'],
        'holdout_counts': (h['objects'],h['main_runs'],h['diagnostic_runs'],h['ungated_requests'])==(11,132,18,33),
        'three_equal_baseline_results': all(h['summary']['main:'+key]['correct_reports']==11 and h['summary']['main:'+key]['rejected_variants']==33 for key in ['e2ag','opa','agentspec_host']),
        'heterogeneity_evidence_passed': m['status']=='passed' and m['case_count']==42 and not m['failures'],
    }
    for env in ['table','algorithm','equation','align']:
        pattern = r'\\begin\{'+env+r'\}.*?\\end\{'+env+r'\}'
        checks[env+'_unchanged'] = re.findall(pattern,old,re.S)==re.findall(pattern,text,re.S)
    report = {'checks': checks, 'status': 'passed' if all(checks.values()) else 'failed',
              'unresolved_references': sorted(set(refs)-set(labels)),
              'undefined_citations': sorted(set(citations)-set(bib)),
              'sources': {str(p.relative_to(ROOT)): digest(p) for p in [paper,previous,response,holdout,checked,hetero]},
              'not_checked': ['external citation entailment', 'final rendered pages and layout', 'formal overhead results'],
              'pending': ['R1.3 and R2.3 performance evidence', 'synchronized response/marked/clean PDFs']}
    with output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args().output)
