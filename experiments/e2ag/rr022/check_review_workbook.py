"""Independent OOXML check: material fidelity, blank answers and no formulas."""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workbook', type=Path, required=True)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    data = json.loads(args.data.read_text(encoding='utf-8'))['sheets']
    report = dict(status='checking', sheets=[], formula_count=0, reviewer_answers=0)
    with zipfile.ZipFile(args.workbook) as archive:
        assert archive.testzip() is None
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(node.itertext()) for node in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', ns)]
        for index, (name, rows) in enumerate(data.items(), 1):
            xml = ET.fromstring(archive.read(f'xl/worksheets/sheet{index}.xml'))
            actual = {}
            for c in xml.findall('.//s:c', ns):
                assert c.find('s:f', ns) is None, f'Unexpected formula {name} {c.attrib}'
                value = c.find('s:v', ns)
                kind = c.attrib.get('t')
                if kind == 's': value = strings[int(value.text)]
                elif kind == 'inlineStr': value = ''.join(c.find('s:is', ns).itertext())
                else: value = value.text if value is not None else ''
                assert kind != 'e', f'Error cell {name} {c.attrib}'
                actual[c.attrib['r']] = value
            for r, row in enumerate(rows, 1):
                for col, expected in enumerate(row):
                    address = chr(65 + col) + str(r)
                    value = actual.get(address, '')
                    if expected is None:
                        assert not value, f'Pre-filled answer {address}'
                    else:
                        assert value in (str(expected), "'" + str(expected)), f'Data mismatch {name}!{address}: {value!r} != {expected!r}'
            validations = xml.findall('.//s:dataValidation', ns)
            if name == '复核填写':
                covered = {v.attrib['sqref'] for v in validations}
                assert covered == {f'{c}2:{c}13' for c in 'CDEFG'}, covered
                assert len(rows) == 13 and len(rows[0]) == 11
                report['blank_reviewer_cells'] = 12 * 9
            report['sheets'].append(dict(name=name, rows=len(rows), validations=len(validations)))
        xmltext = ''.join(archive.read(n).decode('utf-8') for n in archive.namelist() if n.endswith('.xml'))
        for forbidden in ('AgentSpec', 'E2AG', 'OPA', 'other_read_object', 'wrong_read_version', 'other_write_object', 'expected_report', 'C:\\Users\\'):
            assert forbidden not in xmltext, f'Method/label/internal-path leakage: {forbidden}'
    report.update(status='workbook_verified', sha256=hashlib.sha256(args.workbook.read_bytes()).hexdigest(), bytes=args.workbook.stat().st_size)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__': main()
