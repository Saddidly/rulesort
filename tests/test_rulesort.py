from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rulesort.models import Config, ConflictError, PlanError
from rulesort.planning import build_plan, load_plan, save_plan, summary
from rulesort.transaction import apply_plan, undo_journal
from rulesort.journal import root_operation_lock
from rulesort.recovery import inspect_journal, recover_journal
import rulesort.transaction as transactions


class RuleSortTests(unittest.TestCase):
    def config(self, root: Path, **extra):
        raw = {"root": str(root), "rules": [{"name": "images", "extensions": ["jpg"], "destination": "images/{year}/{month}"}]}
        raw.update(extra)
        return Config.from_dict(raw, Path.cwd())

    def test_apply_and_undo_are_journaled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "photo.JPG"
            source.write_bytes(b"sample image")
            plan = build_plan(self.config(root))
            self.assertEqual(summary(plan)["planned"], 1)
            plan_path = root / "plan.json"
            save_plan(plan, plan_path)
            journal = apply_plan(load_plan(plan_path))
            moved = root / plan["actions"][0]["destination"]
            self.assertEqual(moved.read_bytes(), b"sample image")
            self.assertTrue(journal.exists())
            undo_journal(journal)
            self.assertEqual(source.read_bytes(), b"sample image")
            self.assertFalse(moved.exists())

    def test_changed_source_is_rejected_without_mutating_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "photo.jpg"
            source.write_bytes(b"original")
            plan = build_plan(self.config(root))
            source.write_bytes(b"changed")
            with self.assertRaises(ConflictError):
                apply_plan(plan)
            self.assertEqual(source.read_bytes(), b"changed")
            self.assertFalse((root / "images").exists())

    def test_existing_destination_is_reported_and_apply_requires_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "photo.jpg"
            source.write_bytes(b"source")
            destination = root / build_plan(self.config(root))["actions"][0]["destination"]
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"keep")
            plan = build_plan(self.config(root))
            self.assertEqual(summary(plan)["conflict"], 1)
            with self.assertRaises(ConflictError):
                apply_plan(plan)
            self.assertEqual(destination.read_bytes(), b"keep")
            self.assertEqual(source.read_bytes(), b"source")

    def test_recursive_scan_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external:
            root = Path(temporary)
            other = Path(external) / "outside.jpg"
            other.write_bytes(b"outside")
            link = root / "external"
            try:
                os.symlink(Path(external), link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            nested = root / "nested"
            nested.mkdir()
            (nested / "inside.jpg").write_bytes(b"inside")
            plan = build_plan(self.config(root, recursive=True))
            sources = {item["source"] for item in plan["actions"]}
            self.assertEqual(sources, {"nested/inside.jpg"})

    def test_casefolded_destination_conflicts_are_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.jpg").write_bytes(b"A")
            nested = root / "nested"
            nested.mkdir()
            (nested / "a.jpg").write_bytes(b"B")
            config = Config.from_dict({"root": str(root), "recursive": True, "rules": [
                {"name": "both", "extensions": ["jpg"], "destination": "Collected"}
            ]}, Path.cwd())
            plan = build_plan(config)
            self.assertEqual(summary(plan)["conflict"], 2)

    def test_undo_refuses_a_changed_moved_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "photo.jpg"
            source.write_bytes(b"original")
            plan = build_plan(self.config(root))
            journal = apply_plan(plan)
            moved = root / plan["actions"][0]["destination"]
            moved.write_bytes(b"edited after apply")
            with self.assertRaises(ConflictError):
                undo_journal(journal)
            self.assertEqual(moved.read_bytes(), b"edited after apply")

    def test_partial_apply_failure_restores_all_staged_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'a.jpg').write_bytes(b'A')
            (root / 'b.jpg').write_bytes(b'B')
            plan = build_plan(self.config(root))
            original = transactions._move_no_replace
            count = 0
            def interrupted(source, destination):
                nonlocal count
                count += 1
                if count == 4:
                    raise OSError('injected commit failure')
                original(source, destination)
            with patch.object(transactions, '_move_no_replace', interrupted):
                with self.assertRaises(Exception):
                    apply_plan(plan)
            self.assertEqual((root / 'a.jpg').read_bytes(), b'A')
            self.assertEqual((root / 'b.jpg').read_bytes(), b'B')

    def test_partial_undo_failure_recovery_completes_undo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'a.jpg').write_bytes(b'A')
            (root / 'b.jpg').write_bytes(b'B')
            plan = build_plan(self.config(root))
            journal = apply_plan(plan)
            original = transactions._move_no_replace
            count = 0
            def interrupted(source, destination):
                nonlocal count
                count += 1
                if count == 4:
                    raise OSError('injected restore failure')
                original(source, destination)
            with patch.object(transactions, '_move_no_replace', interrupted):
                with self.assertRaises(PlanError):
                    undo_journal(journal)
            for action in plan['actions']:
                self.assertTrue((root / action['source']).is_file())
                self.assertFalse((root / action['destination']).exists())
            self.assertEqual(inspect_journal(journal)['state'], 'recovered_undo')

    def _crash_process(self, plan_path: Path, journal: Path | None, match: str, after_unlink: bool) -> subprocess.CompletedProcess:
        source = plan_path.parent / 'photo.jpg'
        mode = match
        destination = ''
        if match.startswith('destination='):
            mode, destination = 'destination', str(plan_path.parent / match.split('=', 1)[1])
        script = f'''\
import os
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).parents[1] / 'src')!r})
from rulesort.planning import load_plan
from rulesort.transaction import apply_plan, undo_journal
plan_path = Path({str(plan_path)!r})
source = os.path.normcase(os.path.abspath({str(source)!r}))
journal = Path({str(journal)!r}) if {journal is not None!r} else None
mode = {mode!r}
destination = os.path.normcase(os.path.abspath({destination!r}))
after = {after_unlink!r}
real_path_unlink = Path.unlink
def matches(path):
    candidate = Path(os.fsdecode(path))
    if mode == 'source':
        return os.path.normcase(os.path.abspath(candidate)) == source
    if mode == 'apply_commit':
        return candidate.parent.name == 'staged' and candidate.parent.parent.parent.name == '.rulesort-transactions'
    if mode == 'undo_restore':
        return candidate.parent.name.startswith('undo-staged-')
    if mode == 'destination':
        return os.path.normcase(os.path.abspath(candidate)) == destination
    return False
def interrupted_unlink(path, *args, **kwargs):
    if matches(path):
        if after:
            real_path_unlink(path, *args, **kwargs)
        os._exit(83 if after else 82)
    return real_path_unlink(path, *args, **kwargs)
Path.unlink = interrupted_unlink
if journal is None:
    apply_plan(load_plan(plan_path))
else:
    undo_journal(journal)
'''
        env = os.environ.copy()
        env['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
        return subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=30)

    def _crash_recovery(self, journal: Path, target: Path, after_unlink: bool) -> subprocess.CompletedProcess:
        script = f'''\
import os
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).parents[1] / 'src')!r})
from rulesort.recovery import recover_journal
target = os.path.normcase(os.path.abspath({str(target)!r}))
real_path_unlink = Path.unlink
after = {after_unlink!r}
def interrupted_unlink(path, *args, **kwargs):
    if os.path.normcase(os.path.abspath(os.fsdecode(path))) == target:
        if after:
            real_path_unlink(path, *args, **kwargs)
        os._exit(83 if after else 82)
    return real_path_unlink(path, *args, **kwargs)
Path.unlink = interrupted_unlink
recover_journal(Path({str(journal)!r}))
'''
        env = os.environ.copy()
        env['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
        return subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=30)

    def _write_plan(self, root: Path) -> Path:
        plan = build_plan(self.config(root))
        path = root / 'plan.json'
        save_plan(plan, path)
        return path

    def _journal_for(self, root: Path) -> Path:
        journals = list((root / '.rulesort-transactions').glob('*/journal.jsonl'))
        self.assertEqual(len(journals), 1)
        return journals[0]

    def test_process_crash_between_apply_link_and_unlink_is_recoverable_and_repeatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            plan_path = self._write_plan(root)
            result = self._crash_process(plan_path, None, 'source', after_unlink=False)
            self.assertEqual(result.returncode, 82, result.stderr)
            journal = self._journal_for(root)
            report = inspect_journal(journal)
            self.assertEqual(report['state'], 'apply_interrupted')
            self.assertTrue(report['recoverable'], report['issues'])
            self.assertEqual(set(report['actions'][0]['locations']), {'source', 'apply_stage'})
            recovered = recover_journal(journal)
            self.assertEqual(recovered['state'], 'recovered_apply')
            self.assertEqual(source.read_bytes(), b'original')
            before = journal.read_bytes()
            self.assertEqual(recover_journal(journal)['state'], 'recovered_apply')
            self.assertEqual(journal.read_bytes(), before)

    def test_process_crash_after_apply_commit_unlink_is_recoverable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            plan_path = self._write_plan(root)
            result = self._crash_process(plan_path, None, 'apply_commit', after_unlink=True)
            self.assertEqual(result.returncode, 83, result.stderr)
            journal = self._journal_for(root)
            self.assertTrue(inspect_journal(journal)['recoverable'])
            recover_journal(journal)
            self.assertEqual(source.read_bytes(), b'original')

    def test_process_crash_between_undo_link_and_unlink_is_recoverable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            plan_path = self._write_plan(root)
            plan = load_plan(plan_path)
            journal = apply_plan(plan)
            destination = root / plan['actions'][0]['destination']
            result = self._crash_process(plan_path, journal, f"destination={plan['actions'][0]['destination']}", after_unlink=False)
            self.assertEqual(result.returncode, 82, result.stderr)
            report = inspect_journal(journal)
            self.assertEqual(report['state'], 'undo_interrupted')
            self.assertTrue(report['recoverable'], report['issues'])
            self.assertIn('undo_stage', report['actions'][0]['locations'])
            recover_journal(journal)
            self.assertEqual(source.read_bytes(), b'original')
            self.assertFalse(destination.exists())

    def test_process_crash_after_undo_restore_unlink_completes_undo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            plan_path = self._write_plan(root)
            plan = load_plan(plan_path)
            journal = apply_plan(plan)
            result = self._crash_process(plan_path, journal, 'undo_restore', after_unlink=True)
            self.assertEqual(result.returncode, 83, result.stderr)
            self.assertTrue(inspect_journal(journal)['recoverable'])
            recover_journal(journal)
            self.assertEqual(source.read_bytes(), b'original')
            self.assertEqual(inspect_journal(journal)['state'], 'recovered_undo')

    def test_recovery_itself_can_be_interrupted_and_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            plan_path = self._write_plan(root)
            result = self._crash_process(plan_path, None, 'source', after_unlink=False)
            self.assertEqual(result.returncode, 82, result.stderr)
            journal = self._journal_for(root)
            interrupted = self._crash_recovery(journal, source, after_unlink=False)
            self.assertEqual(interrupted.returncode, 82, interrupted.stderr)
            self.assertTrue(inspect_journal(journal)['recoverable'])
            self.assertEqual(recover_journal(journal)['state'], 'recovered_apply')
            self.assertEqual(source.read_bytes(), b'original')

    def test_recovery_refuses_changed_content_and_preserves_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            plan_path = self._write_plan(root)
            result = self._crash_process(plan_path, None, 'source', after_unlink=False)
            self.assertEqual(result.returncode, 82, result.stderr)
            journal = self._journal_for(root)
            staged = next((root / '.rulesort-transactions').glob('*/staged/00000000'))
            staged.write_bytes(b'edited')
            report = inspect_journal(journal)
            self.assertFalse(report['recoverable'])
            with self.assertRaises(ConflictError):
                recover_journal(journal)
            self.assertEqual(staged.read_bytes(), b'edited')
            self.assertEqual(source.read_bytes(), b'edited')

    def test_recovery_refuses_unrelated_original_slot_collision_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            plan_path = self._write_plan(root)
            plan = load_plan(plan_path)
            journal = apply_plan(plan)
            result = self._crash_process(plan_path, journal, f"destination={plan['actions'][0]['destination']}", after_unlink=False)
            self.assertEqual(result.returncode, 82, result.stderr)
            source.write_bytes(b'unrelated')
            report = inspect_journal(journal)
            self.assertFalse(report['recoverable'])
            with self.assertRaises(ConflictError):
                recover_journal(journal)
            self.assertEqual(source.read_bytes(), b'unrelated')

    def test_malformed_journal_and_unanchored_path_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'photo.jpg').write_bytes(b'original')
            journal = apply_plan(build_plan(self.config(root)))
            rows = [json.loads(line) for line in journal.read_text().splitlines()]
            rows[0]['actions'][0]['source'] = '.'
            journal.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
            with self.assertRaises(PlanError):
                inspect_journal(journal)
            with tempfile.TemporaryDirectory() as elsewhere:
                copied = Path(elsewhere) / 'journal.jsonl'
                copied.write_bytes(journal.read_bytes())
                with self.assertRaises(PlanError):
                    inspect_journal(copied)

    def test_truncated_final_journal_record_is_refused_without_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'photo.jpg').write_bytes(b'original')
            journal = apply_plan(build_plan(self.config(root)))
            with journal.open('ab') as handle:
                handle.write(b'{"event":"truncated')
            before = journal.read_bytes()
            with self.assertRaises(PlanError):
                recover_journal(journal)
            self.assertEqual(journal.read_bytes(), before)

    def test_hardlinked_planned_sources_are_refused_before_any_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / 'a.jpg'
            second = root / 'b.jpg'
            first.write_bytes(b'same inode')
            try:
                os.link(first, second)
            except OSError:
                self.skipTest('hard links are unavailable')
            config = self.config(root, recursive=True)
            with self.assertRaises(PlanError):
                apply_plan(build_plan(config))
            self.assertEqual(first.read_bytes(), b'same inode')
            self.assertEqual(second.read_bytes(), b'same inode')

    def test_apply_rejects_managed_file_directory_ancestor_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            plan = build_plan(self.config(root, rules=[{
                'name': 'self-nesting', 'extensions': ['jpg'], 'destination': '{name}'
            }]))
            self.assertEqual(plan['actions'][0]['destination'], 'photo.jpg/photo.jpg')
            with self.assertRaises(PlanError):
                apply_plan(plan)
            self.assertEqual(source.read_bytes(), b'original')
            self.assertFalse((root / 'photo.jpg' / 'photo.jpg').exists())

    def test_apply_rejects_existing_file_as_destination_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            blocker = root / 'block'
            blocker.write_bytes(b'keep')
            plan = build_plan(self.config(root, rules=[{
                'name': 'under-file', 'extensions': ['jpg'], 'destination': 'block'
            }]))
            with self.assertRaises(ConflictError):
                apply_plan(plan)
            self.assertEqual(source.read_bytes(), b'original')
            self.assertEqual(blocker.read_bytes(), b'keep')
            self.assertFalse((root / 'block' / 'photo.jpg').exists())

    def test_apply_rejects_planned_target_that_is_another_target_parent(self):
        from rulesort.planning import sha256_file

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actions = []
            for source_name, destination in (('a.jpg', 'T'), ('b.jpg', 'T/file')):
                source = root / source_name
                source.write_bytes(source_name.encode())
                info = source.stat()
                actions.append({
                    'source': source_name,
                    'destination': destination,
                    'size': info.st_size,
                    'mtime_ns': info.st_mtime_ns,
                    'sha256': sha256_file(source),
                    'status': 'planned',
                })
            with self.assertRaises(PlanError):
                apply_plan({'version': 1, 'root': str(root), 'actions': actions})
            self.assertEqual((root / 'a.jpg').read_bytes(), b'a.jpg')
            self.assertEqual((root / 'b.jpg').read_bytes(), b'b.jpg')
            self.assertFalse((root / 'T').exists())

    def test_two_and_three_file_cycles_interrupted_during_commit_recover_original_names(self):
        from rulesort.planning import sha256_file

        for cycle_size in (2, 3):
            with self.subTest(cycle_size=cycle_size), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                names = ('a.jpg', 'b.jpg', 'c.jpg')[:cycle_size]
                actions = []
                expected = {}
                for index, name in enumerate(names):
                    content = f'content-{index}'.encode()
                    source = root / name
                    source.write_bytes(content)
                    expected[name] = content
                    info = source.stat()
                    actions.append({
                        'source': name,
                        'destination': names[(index + 1) % len(names)],
                        'size': info.st_size,
                        'mtime_ns': info.st_mtime_ns,
                        'sha256': sha256_file(source),
                        'status': 'planned',
                    })
                plan_path = root / 'cycle-plan.json'
                save_plan({'version': 1, 'root': str(root), 'actions': actions}, plan_path)
                result = self._crash_process(plan_path, None, 'apply_commit', after_unlink=False)
                self.assertEqual(result.returncode, 82, result.stderr)
                journal = self._journal_for(root)
                report = inspect_journal(journal)
                self.assertEqual(report['state'], 'apply_interrupted')
                self.assertTrue(report['recoverable'], report['issues'])
                self.assertEqual(recover_journal(journal)['state'], 'recovered_apply')
                for name, content in expected.items():
                    self.assertEqual((root / name).read_bytes(), content)

    def test_cli_inspect_and_recover_report_interrupted_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            plan_path = self._write_plan(root)
            crashed = self._crash_process(plan_path, None, 'source', after_unlink=False)
            self.assertEqual(crashed.returncode, 82, crashed.stderr)
            journal = self._journal_for(root)
            env = os.environ.copy()
            env['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')

            inspected = subprocess.run(
                [sys.executable, '-m', 'rulesort.cli', 'inspect', str(journal), '--json'],
                capture_output=True, text=True, env=env, timeout=30,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            inspect_report = json.loads(inspected.stdout)
            self.assertEqual(inspect_report['state'], 'apply_interrupted')
            self.assertTrue(inspect_report['recoverable'])

            recovered = subprocess.run(
                [sys.executable, '-m', 'rulesort.cli', 'recover', str(journal), '--json'],
                capture_output=True, text=True, env=env, timeout=30,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            recover_report = json.loads(recovered.stdout)
            self.assertEqual(recover_report['state'], 'recovered_apply')
            self.assertIn('Recovery completed', recover_report['message'])
            self.assertEqual(source.read_bytes(), b'original')

    def test_terminal_apply_journal_write_error_keeps_valid_applied_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            plan = build_plan(self.config(root))
            real_write = transactions.Journal.write
            raised = False

            def fail_after_terminal_write(journal, event, **data):
                nonlocal raised
                real_write(journal, event, **data)
                if event == 'transaction_applied' and not raised:
                    raised = True
                    raise OSError('injected fsync report after terminal record')

            with patch.object(transactions.Journal, 'write', fail_after_terminal_write):
                with self.assertRaisesRegex(PlanError, 'committed journal state'):
                    apply_plan(plan)
            journal = self._journal_for(root)
            report = inspect_journal(journal)
            self.assertEqual(report['state'], 'applied')
            self.assertEqual(report['issues'], [])
            undo_journal(journal)
            self.assertEqual(source.read_bytes(), b'original')

    def test_terminal_undo_journal_write_error_keeps_valid_undone_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            journal = apply_plan(build_plan(self.config(root)))
            real_write = transactions.Journal.write
            raised = False

            def fail_after_terminal_write(writer, event, **data):
                nonlocal raised
                real_write(writer, event, **data)
                if event == 'undo_complete' and not raised:
                    raised = True
                    raise OSError('injected fsync report after terminal record')

            with patch.object(transactions.Journal, 'write', fail_after_terminal_write):
                with self.assertRaisesRegex(PlanError, 'completed journal state'):
                    undo_journal(journal)
            report = inspect_journal(journal)
            self.assertEqual(report['state'], 'undone')
            self.assertEqual(report['issues'], [])
            self.assertEqual(source.read_bytes(), b'original')

    def test_journal_rejects_apply_terminal_during_active_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'photo.jpg').write_bytes(b'original')
            journal = apply_plan(build_plan(self.config(root)))
            rows = [json.loads(line) for line in journal.read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'transaction_applied')
            rows[-1:] = [
                {'event': 'recovery_started', 'operation': 'apply', 'target': 'originals'},
                {'event': 'transaction_applied'},
            ]
            journal.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
            with self.assertRaisesRegex(PlanError, 'not allowed during active recovery'):
                inspect_journal(journal)

    def test_journal_rejects_undo_terminal_during_active_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'photo.jpg').write_bytes(b'original')
            journal = apply_plan(build_plan(self.config(root)))
            undo_journal(journal)
            rows = [json.loads(line) for line in journal.read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'undo_complete')
            rows.insert(-1, {'event': 'recovery_started', 'operation': 'undo', 'target': 'originals'})
            journal.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
            with self.assertRaisesRegex(PlanError, 'not allowed during active recovery'):
                inspect_journal(journal)

    def test_journal_rejects_events_after_terminal_recovery_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            crashed = self._crash_process(self._write_plan(root), None, 'source', after_unlink=False)
            self.assertEqual(crashed.returncode, 82, crashed.stderr)
            journal = self._journal_for(root)
            recover_journal(journal)
            with journal.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps({'event': 'transaction_applied'}) + '\n')
            with self.assertRaisesRegex(PlanError, 'after terminal recovery completion'):
                inspect_journal(journal)

    def test_journal_with_unusable_inode_identity_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'photo.jpg').write_bytes(b'original')
            journal = apply_plan(build_plan(self.config(root)))
            rows = [json.loads(line) for line in journal.read_text().splitlines()]
            rows[0]['actions'][0]['inode'] = 0
            journal.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
            with self.assertRaises(PlanError):
                inspect_journal(journal)

    def test_lock_excludes_other_process_and_stale_lock_file_is_reusable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = f'''\
import sys, time
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).parents[1] / 'src')!r})
from rulesort.journal import root_operation_lock
with root_operation_lock(Path({str(root)!r})):
    print('LOCKED', flush=True)
    time.sleep(20)
'''
            env = os.environ.copy()
            env['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
            process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'LOCKED')
                with self.assertRaises(ConflictError):
                    with root_operation_lock(root):
                        pass
            finally:
                process.terminate()
                process.wait(timeout=10)
                process.stdout.close()
                process.stderr.close()
            lock_file = root / '.rulesort-transactions' / '.operation.lock'
            self.assertTrue(lock_file.exists())
            with root_operation_lock(root):
                pass

    def test_no_replace_move_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / 'a', root / 'b'
            source.write_bytes(b'A'); target.write_bytes(b'B')
            with self.assertRaises(FileExistsError):
                transactions._move_no_replace(source, target)
            self.assertEqual(source.read_bytes(), b'A')
            self.assertEqual(target.read_bytes(), b'B')

    def test_plan_output_cannot_replace_a_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'photo.jpg'
            source.write_bytes(b'original')
            plan = build_plan(self.config(root))
            with self.assertRaises(Exception):
                save_plan(plan, source)
            self.assertEqual(source.read_bytes(), b'original')


if __name__ == "__main__":
    unittest.main()
