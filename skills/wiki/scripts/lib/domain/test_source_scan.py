"""Tests for source_scan domain logic."""

from __future__ import annotations

import unittest

from lib.domain.source_scan import (
    LARGE_FILE_THRESHOLD,
    SourceCandidate,
    SourceCategory,
    ScanResult,
    classify_source_files,
    _is_binary,
    _in_denied_dir,
    _score_file,
)


class TestIsBinary(unittest.TestCase):
    def test_common_binary_extensions(self):
        for ext in [".png", ".jpg", ".zip", ".exe", ".pyc", ".wasm"]:
            self.assertTrue(_is_binary(f"foo/bar{ext}"), f"should be binary: {ext}")

    def test_source_extensions_not_binary(self):
        for ext in [".py", ".ts", ".js", ".go", ".rs", ".rb", ".java"]:
            self.assertFalse(_is_binary(f"foo/bar{ext}"), f"should not be binary: {ext}")

    def test_no_extension(self):
        self.assertFalse(_is_binary("Makefile"))

    def test_case_insensitive(self):
        self.assertTrue(_is_binary("image.PNG"))


class TestInDeniedDir(unittest.TestCase):
    def test_node_modules(self):
        self.assertTrue(_in_denied_dir("node_modules/foo/bar.js"))

    def test_pycache(self):
        self.assertTrue(_in_denied_dir("src/__pycache__/module.cpython-311.pyc"))

    def test_egg_info(self):
        self.assertTrue(_in_denied_dir("mypackage.egg-info/PKG-INFO"))

    def test_normal_path(self):
        self.assertFalse(_in_denied_dir("src/models/user.py"))

    def test_git_dir(self):
        self.assertTrue(_in_denied_dir(".git/config"))

    def test_denied_segment_in_filename(self):
        self.assertFalse(_in_denied_dir("src/build_helper.py"))


class TestScoreFile(unittest.TestCase):
    def test_migration_dir_match(self):
        score = _score_file("db/migrations/001_create_users.py", SourceCategory.SCHEMA)
        self.assertGreater(score, 0.0)

    def test_routes_file_pattern(self):
        score = _score_file("src/router.ts", SourceCategory.ROUTES)
        self.assertGreater(score, 0.0)

    def test_test_file_pattern(self):
        score = _score_file("tests/test_user.py", SourceCategory.TESTS)
        self.assertGreater(score, 0.0)

    def test_spec_file_pattern(self):
        score = _score_file("src/components/Button.spec.tsx", SourceCategory.TESTS)
        self.assertGreater(score, 0.0)

    def test_jest_test_file(self):
        score = _score_file("src/__tests__/utils.test.js", SourceCategory.TESTS)
        self.assertGreater(score, 0.0)

    def test_entry_main(self):
        score = _score_file("src/main.py", SourceCategory.ENTRY)
        self.assertGreater(score, 0.0)

    def test_manage_py(self):
        score = _score_file("manage.py", SourceCategory.ENTRY)
        self.assertGreater(score, 0.0)

    def test_no_match(self):
        score = _score_file("README.md", SourceCategory.SCHEMA)
        self.assertEqual(score, 0.0)

    def test_dir_and_file_match_stacks(self):
        score = _score_file("migrations/001_create_users.py", SourceCategory.SCHEMA)
        self.assertGreaterEqual(score, 0.4)

    def test_both_dir_and_file_pattern_match(self):
        score = _score_file("models/user.schema.py", SourceCategory.SCHEMA)
        self.assertGreaterEqual(score, 0.8)

    def test_score_never_exceeds_one(self):
        score = _score_file("models/user.schema.py", SourceCategory.SCHEMA)
        self.assertLessEqual(score, 1.0)

    def test_validator_file(self):
        score = _score_file("src/user.validator.ts", SourceCategory.RULES)
        self.assertGreater(score, 0.0)

    def test_enum_file(self):
        score = _score_file("src/status.enum.ts", SourceCategory.STATE)
        self.assertGreater(score, 0.0)

    def test_django_urls(self):
        score = _score_file("myapp/urls.py", SourceCategory.ROUTES)
        self.assertGreater(score, 0.0)

    def test_rails_controller(self):
        score = _score_file("app/controllers/users_controller.rb", SourceCategory.ROUTES)
        self.assertGreater(score, 0.0)

    def test_go_cmd_entry(self):
        score = _score_file("cmd/server.go", SourceCategory.ENTRY)
        self.assertGreater(score, 0.0)

    def test_constants_file(self):
        score = _score_file("src/constants.ts", SourceCategory.RULES)
        self.assertGreater(score, 0.0)

    def test_rspec_file(self):
        score = _score_file("spec/models/user_spec.rb", SourceCategory.TESTS)
        self.assertGreater(score, 0.0)


class TestClassifySourceFiles(unittest.TestCase):
    def _make_files(self, paths: list[str], size: int = 1000) -> list[tuple[str, int]]:
        return [(p, size) for p in paths]

    def test_basic_classification(self):
        files = self._make_files([
            "db/migrations/001_create_users.py",
            "src/routes/api.ts",
            "tests/test_user.py",
            "src/main.py",
        ])
        result = classify_source_files(files, slug="myapp", revision="abc123")
        categories = {c.category for c in result.candidates}
        self.assertIn(SourceCategory.SCHEMA, categories)
        self.assertIn(SourceCategory.ROUTES, categories)
        self.assertIn(SourceCategory.TESTS, categories)
        self.assertIn(SourceCategory.ENTRY, categories)

    def test_exclude_paths(self):
        files = self._make_files(["docs/README.md", "src/main.py"])
        result = classify_source_files(
            files, slug="myapp", revision="abc123",
            exclude_paths=frozenset({"src/main.py"}),
        )
        paths = [c.path for c in result.candidates]
        self.assertNotIn("src/main.py", paths)

    def test_binary_excluded(self):
        files = self._make_files(["src/image.png", "src/main.py"])
        result = classify_source_files(files, slug="myapp", revision="abc123")
        paths = [c.path for c in result.candidates]
        self.assertNotIn("src/image.png", paths)

    def test_denied_dir_excluded(self):
        files = self._make_files(["node_modules/express/index.js", "src/main.py"])
        result = classify_source_files(files, slug="myapp", revision="abc123")
        paths = [c.path for c in result.candidates]
        self.assertNotIn("node_modules/express/index.js", paths)

    def test_large_file_warning(self):
        files = [("db/migrations/huge.py", LARGE_FILE_THRESHOLD + 1)]
        result = classify_source_files(files, slug="myapp", revision="abc123")
        self.assertTrue(result.candidates[0].large_file_warning)

    def test_normal_file_no_warning(self):
        files = [("db/migrations/small.py", 1000)]
        result = classify_source_files(files, slug="myapp", revision="abc123")
        self.assertFalse(result.candidates[0].large_file_warning)

    def test_confidence_clamped_to_one(self):
        files = self._make_files(["models/user.schema.py"])
        result = classify_source_files(files, slug="myapp", revision="abc123")
        for c in result.candidates:
            self.assertLessEqual(c.confidence, 1.0)

    def test_max_files_per_category(self):
        files = self._make_files([f"tests/test_{i}.py" for i in range(20)])
        result = classify_source_files(
            files, slug="myapp", revision="abc123",
            max_files_per_category=5,
        )
        test_candidates = [c for c in result.candidates if c.category == SourceCategory.TESTS]
        self.assertEqual(len(test_candidates), 5)

    def test_category_filter(self):
        files = self._make_files([
            "db/migrations/001.py",
            "src/routes/api.ts",
            "tests/test_user.py",
        ])
        result = classify_source_files(
            files, slug="myapp", revision="abc123",
            categories=frozenset({SourceCategory.SCHEMA, SourceCategory.TESTS}),
        )
        categories = {c.category for c in result.candidates}
        self.assertNotIn(SourceCategory.ROUTES, categories)

    def test_slug_and_revision_preserved(self):
        result = classify_source_files([], slug="myapp", revision="abc123def")
        self.assertEqual(result.slug, "myapp")
        self.assertEqual(result.revision, "abc123def")

    def test_empty_file_list(self):
        result = classify_source_files([], slug="myapp", revision="abc123")
        self.assertEqual(len(result.candidates), 0)
        self.assertEqual(result.skipped_count, 0)

    def test_stats_populated(self):
        files = self._make_files([
            "db/migrations/001.py",
            "db/migrations/002.py",
            "tests/test_user.py",
        ])
        result = classify_source_files(files, slug="myapp", revision="abc123")
        self.assertEqual(result.stats["schema"], 2)
        self.assertEqual(result.stats["tests"], 1)

    def test_skipped_count_includes_unclassified(self):
        files = self._make_files(["README.md", "LICENSE", "src/main.py"])
        result = classify_source_files(files, slug="myapp", revision="abc123")
        self.assertGreater(result.skipped_count, 0)

    def test_sorted_by_confidence_desc(self):
        files = self._make_files([
            "src/utils.py",
            "models/user.schema.py",
            "tests/test_user.py",
        ])
        result = classify_source_files(files, slug="myapp", revision="abc123")
        confidences = [c.confidence for c in result.candidates]
        self.assertEqual(confidences, sorted(confidences, reverse=True))

    def test_precedence_on_tie(self):
        files = self._make_files(["src/models/user_test.py"])
        result = classify_source_files(files, slug="myapp", revision="abc123")
        if result.candidates:
            cat = result.candidates[0].category
            self.assertIn(cat, (SourceCategory.SCHEMA, SourceCategory.TESTS))

    def test_fullstack_classification(self):
        files = self._make_files([
            "db/migrations/001_create_users.py",
            "db/migrations/002_add_email.py",
            "app/models/user.py",
            "app/controllers/users_controller.rb",
            "app/views/users/index.html.erb",
            "app/validators/user_validator.rb",
            "app/constants/roles.rb",
            "app/enums/status.rb",
            "spec/models/user_spec.rb",
            "spec/controllers/users_controller_spec.rb",
            "config/routes.rb",
            "bin/rails",
        ])
        result = classify_source_files(files, slug="rails-app", revision="abc123")
        categories = {c.category for c in result.candidates}
        self.assertGreaterEqual(len(categories), 4)


if __name__ == "__main__":
    unittest.main()
