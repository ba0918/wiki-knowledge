"""Integration tests for source_scan CLI."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = str(Path(__file__).resolve().parent / "source_scan.py")


def _setup_wiki_with_manifest(tmpdir: str, slug: str = "testapp") -> Path:
    wiki_root = Path(tmpdir) / ".wiki"
    manifests_dir = wiki_root / ".cache" / "manifests"
    manifests_dir.mkdir(parents=True)

    clone_dir = Path(tmpdir) / "clone"
    clone_dir.mkdir()

    subprocess.run(["git", "init"], cwd=str(clone_dir), capture_output=True)
    (clone_dir / "src").mkdir()
    (clone_dir / "src" / "main.py").write_text("print('hello')")
    (clone_dir / "db").mkdir()
    (clone_dir / "db" / "migrations").mkdir(parents=True)
    (clone_dir / "db" / "migrations" / "001_create_users.py").write_text("CREATE TABLE users")
    (clone_dir / "tests").mkdir()
    (clone_dir / "tests" / "test_user.py").write_text("def test_user(): pass")
    (clone_dir / "README.md").write_text("# Test App")

    subprocess.run(["git", "add", "."], cwd=str(clone_dir), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(clone_dir),
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
    )

    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(clone_dir), capture_output=True, text=True,
    ).stdout.strip()

    manifest = {
        "slug": slug,
        "clone_path": str(clone_dir),
        "revision": rev,
        "docs": [{"path": "README.md", "tier": 1}],
    }
    (manifests_dir / f"{slug}.json").write_text(json.dumps(manifest))

    return wiki_root


class TestSourceScanCLI(unittest.TestCase):
    def test_table_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki_root = _setup_wiki_with_manifest(tmpdir)
            proc = subprocess.run(
                ["python3", SCRIPT, "--wiki-root", str(wiki_root), "--slug", "testapp"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("candidates", proc.stdout)

    def test_json_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki_root = _setup_wiki_with_manifest(tmpdir)
            proc = subprocess.run(
                ["python3", SCRIPT, "--wiki-root", str(wiki_root),
                 "--slug", "testapp", "--format", "json"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(data["slug"], "testapp")
            self.assertIsInstance(data["candidates"], list)
            self.assertIsInstance(data["stats"], dict)

    def test_excludes_doc_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki_root = _setup_wiki_with_manifest(tmpdir)
            proc = subprocess.run(
                ["python3", SCRIPT, "--wiki-root", str(wiki_root),
                 "--slug", "testapp", "--format", "json"],
                capture_output=True, text=True,
            )
            data = json.loads(proc.stdout)
            paths = [c["path"] for c in data["candidates"]]
            self.assertNotIn("README.md", paths)

    def test_category_filter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki_root = _setup_wiki_with_manifest(tmpdir)
            proc = subprocess.run(
                ["python3", SCRIPT, "--wiki-root", str(wiki_root),
                 "--slug", "testapp", "--categories", "tests", "--format", "json"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            categories = {c["category"] for c in data["candidates"]}
            self.assertTrue(categories <= {"tests"})

    def test_missing_manifest_returns_1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = subprocess.run(
                ["python3", SCRIPT, "--wiki-root", str(Path(tmpdir) / ".wiki"),
                 "--slug", "nonexistent"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 1)

    def test_invalid_category_returns_2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki_root = _setup_wiki_with_manifest(tmpdir)
            proc = subprocess.run(
                ["python3", SCRIPT, "--wiki-root", str(wiki_root),
                 "--slug", "testapp", "--categories", "invalid_cat"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 2)

    def test_max_files_respected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki_root = _setup_wiki_with_manifest(tmpdir)
            clone_dir = Path(tmpdir) / "clone" / "tests"
            for i in range(20):
                (clone_dir / f"test_{i}.py").write_text(f"def test_{i}(): pass")
            subprocess.run(["git", "add", "."], cwd=str(Path(tmpdir) / "clone"), capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "add tests"],
                cwd=str(Path(tmpdir) / "clone"),
                capture_output=True,
                env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
                     "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
            )

            proc = subprocess.run(
                ["python3", SCRIPT, "--wiki-root", str(wiki_root),
                 "--slug", "testapp", "--max-files", "5",
                 "--categories", "tests", "--format", "json"],
                capture_output=True, text=True,
            )
            data = json.loads(proc.stdout)
            test_candidates = [c for c in data["candidates"] if c["category"] == "tests"]
            self.assertLessEqual(len(test_candidates), 5)


if __name__ == "__main__":
    unittest.main()
