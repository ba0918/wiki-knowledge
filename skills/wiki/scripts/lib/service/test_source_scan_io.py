"""Tests for source_scan I/O layer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lib.domain.types import Err, Ok
from lib.service.source_scan_io import (
    DEFAULT_TIMEOUT,
    FileEntry,
    RunResult,
    ScanIOError,
    get_doc_paths_from_manifest,
    list_files_with_sizes,
    load_manifest,
    resolve_clone_path,
)


class FakeRunner:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "", timed_out: bool = False):
        self._stdout = stdout
        self._returncode = returncode
        self._stderr = stderr
        self._timed_out = timed_out
        self.calls: list[dict] = []

    def run(self, args, *, env=None, cwd=None, timeout=DEFAULT_TIMEOUT):
        self.calls.append({"args": list(args), "cwd": cwd, "timeout": timeout})
        return RunResult(
            returncode=self._returncode,
            stdout=self._stdout,
            stderr=self._stderr,
            timed_out=self._timed_out,
        )


class TestLoadManifest(unittest.TestCase):
    def test_loads_valid_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki_root = Path(tmpdir)
            manifests_dir = wiki_root / ".cache" / "manifests"
            manifests_dir.mkdir(parents=True)
            manifest = {"slug": "myapp", "clone_path": "/tmp/clone", "docs": []}
            (manifests_dir / "myapp.json").write_text(json.dumps(manifest))

            result = load_manifest(wiki_root, "myapp")
            self.assertIsInstance(result, Ok)
            self.assertEqual(result.value["slug"], "myapp")

    def test_missing_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = load_manifest(Path(tmpdir), "nonexistent")
            self.assertIsInstance(result, Err)
            self.assertEqual(result.error, ScanIOError.MANIFEST_NOT_FOUND)

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki_root = Path(tmpdir)
            manifests_dir = wiki_root / ".cache" / "manifests"
            manifests_dir.mkdir(parents=True)
            (manifests_dir / "bad.json").write_text("not json{{{")

            result = load_manifest(wiki_root, "bad")
            self.assertIsInstance(result, Err)
            self.assertEqual(result.error, ScanIOError.MANIFEST_PARSE_ERROR)


class TestResolveClonePath(unittest.TestCase):
    def test_valid_clone_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = resolve_clone_path({"clone_path": tmpdir})
            self.assertIsInstance(result, Ok)
            self.assertEqual(result.value, Path(tmpdir))

    def test_missing_clone_path_key(self):
        result = resolve_clone_path({})
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error, ScanIOError.CLONE_PATH_NOT_FOUND)

    def test_nonexistent_clone_path(self):
        result = resolve_clone_path({"clone_path": "/nonexistent/path"})
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error, ScanIOError.CLONE_PATH_NOT_FOUND)


class TestListFilesWithSizes(unittest.TestCase):
    def test_parses_git_ls_files_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_path = Path(tmpdir)
            (clone_path / "src").mkdir()
            (clone_path / "src" / "main.py").write_text("print('hello')")
            (clone_path / "README.md").write_text("# README")

            stdout = "src/main.py\0README.md\0"
            runner = FakeRunner(stdout=stdout)
            result = list_files_with_sizes(clone_path, runner)

            self.assertIsInstance(result, Ok)
            entries = result.value
            self.assertEqual(len(entries), 2)
            paths = {e.path for e in entries}
            self.assertIn("src/main.py", paths)
            self.assertIn("README.md", paths)
            for e in entries:
                self.assertGreater(e.size_bytes, 0)

    def test_git_ls_files_failure(self):
        runner = FakeRunner(returncode=128, stderr="fatal: not a git repository")
        result = list_files_with_sizes(Path("/tmp"), runner)
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error, ScanIOError.LS_FILES_FAILED)

    def test_git_ls_files_timeout(self):
        runner = FakeRunner(returncode=-1, timed_out=True)
        result = list_files_with_sizes(Path("/tmp"), runner)
        self.assertIsInstance(result, Err)

    def test_empty_output(self):
        runner = FakeRunner(stdout="")
        result = list_files_with_sizes(Path("/tmp"), runner)
        self.assertIsInstance(result, Ok)
        self.assertEqual(len(result.value), 0)

    def test_passes_cwd_to_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeRunner(stdout="")
            list_files_with_sizes(Path(tmpdir), runner)
            self.assertEqual(runner.calls[0]["cwd"], tmpdir)


class TestGetDocPathsFromManifest(unittest.TestCase):
    def test_extracts_paths(self):
        manifest = {
            "docs": [
                {"path": "README.md", "tier": 1},
                {"path": "docs/guide.md", "tier": 2},
            ]
        }
        paths = get_doc_paths_from_manifest(manifest)
        self.assertEqual(paths, frozenset({"README.md", "docs/guide.md"}))

    def test_empty_docs(self):
        paths = get_doc_paths_from_manifest({"docs": []})
        self.assertEqual(paths, frozenset())

    def test_missing_docs_key(self):
        paths = get_doc_paths_from_manifest({})
        self.assertEqual(paths, frozenset())

    def test_handles_non_dict_entries(self):
        paths = get_doc_paths_from_manifest({"docs": ["not a dict", 42]})
        self.assertEqual(paths, frozenset())


if __name__ == "__main__":
    unittest.main()
