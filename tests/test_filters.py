import unittest

from files2gdrive.filters import FilterSet


class FilterSetTests(unittest.TestCase):
    def test_extensions_and_hidden_files_at_every_depth(self):
        filters = FilterSet(["*.tmp"])
        for path in ("a.tmp", "a/b.tmp", ".hidden.tmp", ".config/a.tmp", ".tmp"):
            with self.subTest(path=path):
                self.assertTrue(filters.excludes(path))
        self.assertFalse(filters.excludes("a.tmp.txt"))
        self.assertFalse(filters.excludes("a.TMP"))

    def test_question_mark_matches_exactly_one_character(self):
        filters = FilterSet(["foto?.jpg"])
        self.assertTrue(filters.excludes("album/foto1.jpg"))
        self.assertTrue(filters.excludes("fotoé.jpg"))
        self.assertFalse(filters.excludes("foto.jpg"))
        self.assertFalse(filters.excludes("foto12.jpg"))

    def test_path_patterns_are_root_relative(self):
        filters = FilterSet(["logs/*.log"])
        self.assertTrue(filters.excludes("logs/server.log"))
        self.assertFalse(filters.excludes("other/logs/server.log"))
        self.assertFalse(filters.excludes("logs/sub/server.log"))
        self.assertFalse(FilterSet(["logs/?.log"]).excludes("logs/sub/x.log"))

    def test_globstar_crosses_zero_or_multiple_directory_levels(self):
        filters = FilterSet(["**/cache/"])
        for path in ("cache", "a/cache", "a/b/cache"):
            with self.subTest(path=path):
                self.assertTrue(filters.excludes(path, is_dir=True))
                self.assertTrue(filters.excludes(path + "/nested/data"))
                self.assertFalse(filters.excludes(path, is_dir=False))
        files = FilterSet(["logs/**/*.log"])
        self.assertTrue(files.excludes("logs/root.log"))
        self.assertTrue(files.excludes("logs/a/b/deep.log"))
        self.assertFalse(files.excludes("other/logs/root.log"))

    def test_trailing_slash_matches_root_folder_and_descendants(self):
        filters = FilterSet(["cache/"])
        self.assertTrue(filters.excludes("cache", is_dir=True))
        self.assertTrue(filters.excludes("cache/child/deep"))
        self.assertFalse(filters.excludes("cache"))
        self.assertFalse(filters.excludes("nested/cache", is_dir=True))

    def test_name_rule_can_exclude_directory_and_its_descendants(self):
        filters = FilterSet(["node_modules"])
        self.assertTrue(filters.excludes("nested/node_modules", is_dir=True))
        self.assertTrue(filters.excludes("nested/node_modules/pkg/file.js"))
        self.assertFalse(filters.excludes("node_modules_backup/file.js"))

    def test_global_and_local_rules_are_unioned_without_negation(self):
        global_rules = ["*.tmp"]
        local_rules = ["build/", "!important.tmp"]
        filters = FilterSet([*global_rules, *local_rules])
        self.assertTrue(filters.excludes("cache/a.tmp"))
        self.assertTrue(filters.excludes("build/output"))
        self.assertTrue(filters.excludes("important.tmp"))

    def test_case_sensitivity_is_configurable(self):
        self.assertFalse(FilterSet(["*.pdf"]).excludes("Doc.PDF"))
        self.assertTrue(FilterSet(["*.pdf"], ignore_case=True).excludes("Doc.PDF"))
        self.assertTrue(FilterSet(["CACHE/"], ignore_case=True).excludes("cache/a"))

    def test_other_shell_and_regex_characters_are_literal(self):
        filters = FilterSet(["[ab].txt", "a+.txt", "file\\name"])
        self.assertTrue(filters.excludes("[ab].txt"))
        self.assertTrue(filters.excludes("a+.txt"))
        self.assertTrue(filters.excludes("file\\name"))
        self.assertFalse(filters.excludes("a.txt"))
        self.assertFalse(filters.excludes("aa.txt"))

    def test_linux_names_with_newlines_are_supported(self):
        self.assertTrue(FilterSet(["*.tmp"]).excludes("a\nb.tmp"))
        self.assertTrue(FilterSet(["**"]).excludes("a\nb/file"))

    def test_empty_rules_do_not_exclude_and_whitespace_is_literal(self):
        self.assertFalse(FilterSet([""]).excludes("file"))
        self.assertTrue(FilterSet([" name "]).excludes(" name "))
        self.assertFalse(FilterSet([" name "]).excludes("name"))

    def test_unsafe_paths_and_patterns_are_rejected(self):
        for value in ("", "/", "/absolute", "../outside", "a/../b", "./file", "a//b", "a\x00b"):
            with self.subTest(value=value):
                self.assertRaises(ValueError, FilterSet([]).excludes, value)
        for pattern in ("/absolute", "../outside", "a/../*", "a//b", "/", "a\x00b"):
            with self.subTest(pattern=pattern):
                self.assertRaises(ValueError, FilterSet, [pattern])


if __name__ == "__main__":
    unittest.main()
