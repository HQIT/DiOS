import copy
import json
import unittest
from pr_manifest_workload import manifest_report


def entry(path, sha='a', mode='100644', kind='blob'):
    return dict(path=path, sha=sha * 40, mode=mode, type=kind)


def run(before, after):
    value = dict(repository='test/repo', number=1, base='base', head='head', merge_base='ancestor',
                 before=dict(truncated=False, tree=before), after=dict(truncated=False, tree=after))
    return manifest_report(json.dumps(value).encode())


class ManifestTests(unittest.TestCase):
    def test_no_change(self):
        self.assertEqual(run([entry('a')], [entry('a')])['changes'], [])

    def test_added_deleted_modified(self):
        report = run([entry('del'), entry('mod')], [entry('new'), entry('mod', 'b')])
        self.assertEqual(report['counts'], dict(added=1, deleted=1, modified=1, submodule_paths=0))
        self.assertEqual([c['path'] for c in report['changes']], ['del', 'mod', 'new'])

    def test_mode_only(self):
        self.assertEqual(run([entry('a')], [entry('a', mode='100755')])['counts']['modified'], 1)

    def test_rename_is_delete_plus_add(self):
        report = run([entry('old')], [entry('new')])
        self.assertEqual([c['change'] for c in report['changes']], ['added', 'deleted'])

    def test_symlink_and_submodule(self):
        report = run([], [entry('link', mode='120000'), entry('sub', mode='160000', kind='commit')])
        self.assertEqual(report['counts']['submodule_paths'], 1)
        self.assertEqual(report['counts']['added'], 2)

    def test_directory_not_a_file_change(self):
        self.assertEqual(run([], [entry('dir', kind='tree', mode='040000')])['changes'], [])

    def test_duplicate_or_unknown_mode(self):
        for values in ([entry('a'), entry('a')], [entry('a', mode='100000')]):
            with self.assertRaises(ValueError):
                run([], values)

    def test_truncated_rejected(self):
        value = dict(before=dict(truncated=True, tree=[]), after=dict(truncated=False, tree=[]))
        with self.assertRaises(ValueError):
            manifest_report(json.dumps(value).encode())

    def test_unicode_paths_and_input_order(self):
        values = [entry('中文.md'), entry('a/b')]
        self.assertEqual(run([], values), run([], list(reversed(values))))


if __name__ == '__main__':
    unittest.main()
