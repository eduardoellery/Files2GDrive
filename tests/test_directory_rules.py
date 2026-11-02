from pathlib import Path
import unittest

from files2gdrive.directory_rules import DirectoryRules


def rule(path, included=True): return dict(path=path, included=included)


class DirectoryRulesTests(unittest.TestCase):
    def test_deepest_override_including_reincluded_grandchild_wins(self):
        rules = DirectoryRules([rule('/home/me/Docs'), rule('/home/me/Docs/Work', False),
                                rule('/home/me/Docs/Work/Keep')])
        self.assertTrue(rules.includes('/home/me/Docs/file.txt'))
        self.assertFalse(rules.includes('/home/me/Docs/Work/file.txt'))
        self.assertTrue(rules.includes('/home/me/Docs/Work/Keep/file.txt'))
        self.assertEqual(rules.state('/home/me/Docs'), 'partial')
        self.assertEqual(rules.state('/home/me/Docs/Work'), 'partial')
        self.assertEqual(rules.state('/home/me/Docs/Work/Keep'), 'included')

    def test_similar_prefixes_and_linux_case_are_distinct(self):
        rules = DirectoryRules([rule('/home/me/Docs')])
        self.assertFalse(rules.includes('/home/me/Docs-old/file'))
        self.assertFalse(rules.includes('/home/me/docs/file'))
        self.assertTrue(rules.includes('/home/me/Docs/.hidden/file'))

    def test_canonical_order_and_redundant_rules(self):
        rules = DirectoryRules([rule('/home/me/Z/child'), rule('/home/me/None', False),
                                rule('/home/me/Z'), rule('/home/me/A'), rule('/home/me/Z')])
        self.assertEqual(rules.rules, [rule('/home/me/A'), rule('/home/me/Z')])

    def test_redundant_rules_keep_their_descendants_effective(self):
        rules = DirectoryRules([rule('/a'), rule('/a/b'), rule('/a/b/c', False)])
        self.assertEqual(rules.rules, [rule('/a'), rule('/a/b/c', False)])
        self.assertEqual(rules.state('/a/b'), 'partial')

    def test_parent_toggle_clears_descendant_overrides(self):
        rules = DirectoryRules([rule('/a'), rule('/a/b', False), rule('/a/b/c')])
        self.assertEqual(rules.with_toggle('/a', True), [rule('/a')])
        self.assertEqual(rules.with_toggle('/a', False), [])

    def test_child_toggle_becomes_exception_then_compacts_on_reselect(self):
        rules = DirectoryRules([rule('/a')])
        excluded = rules.with_toggle('/a/b', False)
        self.assertEqual(excluded, [rule('/a'), rule('/a/b', False)])
        self.assertEqual(DirectoryRules(excluded).with_toggle('/a/b', True), [rule('/a')])
        self.assertEqual(rules.rules, [rule('/a')])

    def test_home_restricts_edits_but_can_be_queried(self):
        rules = DirectoryRules([rule('/home/me/.config')], home=Path('/home/me'))
        self.assertEqual(rules.state('/home/me'), 'partial')
        self.assertEqual(rules.state('/home/other'), 'excluded')
        for path in ('/home/me', '/home/other', '/'):
            with self.assertRaises(ValueError): rules.with_toggle(path, True)
            with self.assertRaises(ValueError): DirectoryRules([rule(path)], home='/home/me')

    def test_blocked_tree_is_always_excluded_and_parent_is_partial(self):
        rules = DirectoryRules([rule('/home/me/Projects')], blocked_paths=['/home/me/Projects/private'])
        self.assertFalse(rules.includes('/home/me/Projects/private/state.sqlite3'))
        self.assertEqual(rules.state('/home/me/Projects'), 'partial')
        self.assertEqual(rules.state('/home/me/Projects/private'), 'excluded')
        self.assertTrue(rules.includes('/home/me/Projects/private-other'))
        with self.assertRaises(ValueError): rules.with_toggle('/home/me/Projects/private', True)
        with self.assertRaises(ValueError): DirectoryRules([rule('/home/me/Projects/private/a')], blocked_paths=rules.blocked_paths)

    def test_blocked_paths_collapse_descendants_without_changing_rules(self):
        rules = DirectoryRules([rule('/a')], blocked_paths=['/a/private/deep', '/a/private'])
        self.assertEqual(rules.blocked_paths, ('/a/private',))
        self.assertEqual(rules.rules, [rule('/a')])
        self.assertEqual(rules.with_toggle('/a/private', False), [rule('/a'), rule('/a/private', False)])

    def test_invalid_or_noncanonical_paths_fail_without_normalization(self):
        for path in ('relative', '', '//home/me', '/a//b', '/a/', '/a/../b', '/a/./b', '/a\x00b', b'/a'):
            with self.subTest(path=path), self.assertRaises(ValueError): DirectoryRules([rule(path)])
        self.assertTrue(DirectoryRules([rule('/')]).includes('/any/path'))

    def test_conflicting_duplicate_paths_and_invalid_rule_shapes_fail(self):
        with self.assertRaises(ValueError): DirectoryRules([rule('/a'), rule('/a', False)])
        for rules in (None, '/a', {}, [dict(path='/a')], [dict(path='/a', included=1)],
                      [dict(path='/a', included=True, extra='ignored')]):
            with self.subTest(rules=rules), self.assertRaises(ValueError): DirectoryRules(rules)

    def test_detached_public_rules_cannot_change_inclusion(self):
        rules = DirectoryRules([rule('/a')])
        exported = rules.rules; exported[0]['included'] = False
        self.assertTrue(rules.includes('/a/file'))
        self.assertEqual(rules.state('/unknown'), 'excluded')
