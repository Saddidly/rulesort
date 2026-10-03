from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rulesort.models import Config, ConflictError
from rulesort.planning import build_plan, load_plan, save_plan, summary
from rulesort.transaction import apply_plan, undo_journal
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

    def test_partial_undo_failure_restores_applied_names(self):
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
                with self.assertRaises(Exception):
                    undo_journal(journal)
            for action in plan['actions']:
                self.assertTrue((root / action['destination']).is_file())

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
