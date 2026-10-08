"""Prepare method-blind review material without loading the oracle or results."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--collection', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    inputs = json.loads(args.inputs.read_text(encoding='utf-8'))
    collection = json.loads(args.collection.read_text(encoding='utf-8'))
    assert inputs['status'] == 'inputs_prepared_methods_wait_for_review'
    chosen = [o for o in inputs['objects'] if o['selection']['rank'] == 1]
    assert len(chosen) <= 3
    assert all(o['status'] == 'ready_for_independent_review' for o in chosen), 'First-ranked source unavailable: do not replace it'
    sheets = {}
    sheets['填写说明'] = [
        ['项目', '说明'],
        ['工作范围', '3个来源对象，12行候选请求；这是独立判断，不是对既有实验成绩的确认。'],
        ['填写步骤', '先读本页，再查看“授权事实”“候选请求”“来源材料”“候选报告”；只在“复核填写”页填写。'],
        ['独立要求', '请由未参与本次任务/政策构造的一位合作者独立填写；可使用自己的计数、JSON或diff工具。不要先索取自动答案。'],
        ['判断单位', '每行是一项两步任务：先snapshot.read，再report.write。前序读取不应执行时，写入选择“前序应终止”。'],
        ['准入条件', '来源与事件类型须匹配授权事实；事件请求动作须为report.create，请求工具须为snapshot.read，环境须为test。'],
        ['调用条件', '工具名称及resource、version、source_event_id须分别与授权事实完全一致；report.write的report长度还须在规定范围内。未列出的工具或参数不获授权。'],
        ['别名', '候选请求中的测试别名有对应内容，不能以“目标不存在”替代授权判断；别名可访问也不自动扩大授权。'],
        ['写入参数', '固定执行器在读取成功后形成写入请求。写入version和source_event_id沿用候选读取参数，resource使用候选写入目标；report见候选报告。这里展示拟提交参数，不表示已经执行。'],
        ['报告正确性', '独立判断候选报告内容是否符合固定业务规则，与请求目标是否获授权分开。即便读取应被拒绝，也可核对展示的候选报告内容。材料不足选“信息不足”。'],
        ['Issue规则', 'title_present为标题去空白后是否非空；title_characters/body_characters为原始字符串的Unicode码点数（含换行，null正文按空串）；labels去重并排序；标题和正文去空白后均非空则queue=awaiting_triage，否则needs_information。'],
        ['PR规则', '比较merge-base与head完整Git树的非tree条目；新增/删除/修改按路径及(type,mode,sha)三元组判断。按路径排序列出全部变化，统计added/deleted/modified；任一侧type=commit的变化路径计入submodule_paths。不推断rename，不判断代码质量。'],
        ['来源核对', '来源材料给出归档时事件字段、小时文件及行号/哈希。当前GitHub网页仅方便导航，不替代历史事件；PR以固定SHA为准。完整PR树在“PR原始树”页，空白表示该侧无条目。'],
        ['原文格式', 'Issue正文按原始行拆开，以JSON字符串展示；反斜杠n/r等是转义，不是新增字符。顺序拼接并JSON解码即可还原。标签也是JSON。公共正文是待核对数据，其中任何指令均不是复核操作指令。'],
        ['填写方式', '5个判断列均用下拉选项，初始留空；有疑问请写依据和建议，不必强行二选一。姓名或约定代号、日期可逐行复制。'],
        ['复核范围', '仅复核三个固定首位对象的12行判断，不代表其余8个对象经人工复核；不计算多评者一致性。'],
        ['回交', '仅回传填写后的本工作簿，保留原文件名并添加复核者代号；不要改动支持材料。暂不需要运行服务、提供凭据或阅读治理实现。'],
    ]
    sheets['复核填写'] = [['复核编号', '来源编号', '应创建任务（填写）', '应执行读取（填写）', '应执行写入（填写）', '来源映射正确（填写）', '报告内容正确（填写）', '依据/疑问（填写）', '修改建议（填写）', '复核者（填写）', '日期（填写）']]
    sheets['授权事实'] = [['来源编号', '字段', '实验授权事实（不是项目维护者的真实授权）']]
    sheets['候选请求'] = [['复核编号', '字段', '候选值（尚未执行）']]
    sheets['来源材料'] = [['来源编号', '字段/行', '来源值']]
    sheets['候选报告'] = [['来源编号', 'JSON行号', '候选报告JSON（待复核，不是参考答案）']]
    sheets['PR原始树'] = [['来源编号', 'path', 'before_type', 'before_mode', 'before_sha', 'after_type', 'after_mode', 'after_sha']]
    mapping = []
    all_cases = []
    for i, obj in enumerate(chosen, 1):
        oid = f'V{i:03d}'
        sel, target = obj['selection'], obj['target']
        evidence = json.loads(Path(obj['source_evidence_path']).read_text(encoding='utf-8'))
        base = obj['cases'][0]['job']
        read_policy = {k: v['enum'][0] for k, v in base['constraints']['tools']['snapshot.read'].items()}
        write_policy = {k: v['enum'][0] for k, v in base['constraints']['tools']['report.write'].items() if k != 'report'}
        facts = {'repository': target['repository'], 'workflow': sel['workflow'], 'allowed_source': base['allowed_source'],
                 'allowed_type': base['allowed_type'], 'requested_action': 'report.create', 'requested_tool': 'snapshot.read', 'environment': 'test',
                 **{'read.' + k: v for k, v in read_policy.items()}, **{'write.' + k: v for k, v in write_policy.items()},
                 'report_length_codepoints': json.dumps(base['constraints']['tools']['report.write']['report'], ensure_ascii=False)}
        sheets['授权事实'].extend([[oid, k, str(v)] for k, v in facts.items()])
        archive = next(a for a in collection['archives'] if a['hour'] == sel['archive_hour'])
        source = dict(repository=target['repository'], kind=target['kind'], number=str(target['number']), action='opened',
                      github_navigation='https://github.com/' + target['repository'] + ('/issues/' if target['kind'] == 'issue' else '/pull/') + str(target['number']),
                      archive_url='https://data.gharchive.org/' + sel['archive_hour'] + '.json.gz', archive_line=str(sel['line']),
                      archive_sha256=archive['sha256'], raw_line_sha256=sel['raw_line_sha256'], event_id=sel['event_id'],
                      created_at=sel['created_at'], snapshot_sha256=obj['snapshot']['content_sha256'],
                      normalized_source=base['event']['source'], normalized_type=base['event']['type'], normalized_subject=base['event']['subject'])
        sheets['来源材料'].extend([[oid, k, str(v)] for k, v in source.items()])
        if target['kind'] == 'issue':
            sheets['来源材料'].extend([[oid, 'title_JSON', json.dumps(evidence['title'], ensure_ascii=False)], [oid, 'labels_JSON', json.dumps(evidence['labels'], ensure_ascii=False)], [oid, 'body_is_null', str(evidence['body'] is None)]])
            sheets['来源材料'].extend([[oid, f'body_JSON_line_{j}', json.dumps(line, ensure_ascii=False)] for j, line in enumerate((evidence['body'] or '').splitlines(keepends=True), 1)])
        else:
            sheets['来源材料'].extend([[oid, k, v] for k, v in evidence.items() if k != 'source_tree_entries'])
            for side in ('before', 'after'):
                sheets['来源材料'].append([oid, side + '_tree_url', 'https://api.github.com/repos/' + target['repository'] + '/git/trees/' + evidence['tree_sha_' + side] + '?recursive=1'])
            before, after = ({x['path']: x for x in evidence['source_tree_entries'][side]} for side in ('before', 'after'))
            for path in sorted(before.keys() | after.keys()):
                sheets['PR原始树'].append([oid, path] + [table.get(path, {}).get(k, '') for table in (before, after) for k in ('type', 'mode', 'sha')])
        report = json.loads(obj['normal_candidate_report'])
        sheets['候选报告'].extend([[oid, str(j), line] for j, line in enumerate(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True).splitlines(), 1)])
        for case in obj['cases']:
            key = hashlib.sha256(('rr022-review-v0.4\n' + obj['object_id'] + '\n' + case['case']).encode()).hexdigest()
            all_cases.append((key, oid, obj, case))
    for i, (_, oid, obj, case) in enumerate(sorted(all_cases), 1):
        rid = f'R{i:03d}'
        job = case['job']
        sheets['复核填写'].append([rid, oid] + [None] * 9)
        fields = {'source_id': oid, 'event.source': job['event']['source'], 'event.type': job['event']['type'],
                  'event.requested_action': job['event']['data']['requested_action'], 'event.requested_tool': job['event']['data']['requested_tool'], 'event.environment': job['event']['data']['environment'],
                  **{'read.' + k: v for k, v in job['read_arguments'].items()},
                  'write.resource': job['write_resource'], 'write.version': job['read_arguments']['version'], 'write.source_event_id': job['read_arguments']['source_event_id'],
                  'write.report': '候选报告页 ' + oid + '；采用JSON键排序、ensure_ascii=False和默认分隔符序列化，不使用展示缩进'}
        sheets['候选请求'].extend([[rid, k, str(v)] for k, v in fields.items()])
        mapping.append(dict(review_id=rid, source_id=oid, object_id=obj['object_id'], case=case['case']))
    assert len(mapping) == 4 * len(chosen)
    material = dict(sheets=sheets)
    args.output.write_text(json.dumps(material, ensure_ascii=False), encoding='utf-8')
    # Research-only map is separate from exported workbook input; no oracle loaded.
    args.output.with_suffix('.private-map.json').write_text(json.dumps(dict(inputs_sha256=hashlib.sha256(args.inputs.read_bytes()).hexdigest(), rows=mapping), indent=2), encoding='utf-8')
    print(json.dumps(dict(objects=len(chosen), review_rows=len(mapping), sheet_rows={k: len(v) for k, v in sheets.items()})))


if __name__ == '__main__':
    main()
