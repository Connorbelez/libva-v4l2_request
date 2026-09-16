#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise cache trust across separate acquisitions, without network or media."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import corpus


class Integrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.fixtures = corpus.DEFAULT_FIXTURES
        self.fluster = self.fixtures / 'fluster'
        self.manifest = corpus.load_json(self.fixtures / 'manifest-tiny.json', 'fixture')
        self.index = corpus.SuiteIndex(self.manifest['suites'][0], self.fluster)
        self.vector = 'tiny-422.webm'
        self.ref = self.index.suite_id + '#' + self.vector

    def fetch(self, vector):
        args = corpus.build_parser().parse_args([
            '--manifest', str(self.fixtures / 'manifest-tiny.json'),
            '--pass-sets', str(self.fixtures / 'pass-sets-tiny.json'),
            'fetch', '--fluster', str(self.fluster), '--cache', str(self.cache),
            '--mirror', str(self.fixtures / 'mirror'),
            '--vector', self.index.suite_id + '#' + vector,
        ])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return corpus.cmd_fetch(args)

    def tamper(self):
        path = corpus.asset_path(self.cache, self.index, self.vector)
        path.write_bytes(path.read_bytes() + b'tampered')

    def test_cached_unpinned_asset_is_checked_on_fetch(self):
        self.assertEqual(self.fetch(self.vector), 0)
        self.tamper()
        with self.assertRaisesRegex(corpus.CorpusError, 'hash mismatch'):
            corpus.acquire(self.cache, self.index, self.vector, offline=True)

    def test_new_fetch_does_not_rebless_other_cached_assets(self):
        self.assertEqual(self.fetch(self.vector), 0)
        expected = corpus.load_lock(self.cache)[self.ref]
        self.tamper()
        self.assertEqual(self.fetch('tiny-resize.ivf'), 0)
        self.assertEqual(corpus.load_lock(self.cache)[self.ref], expected)
        issues = corpus.verify_cache(self.manifest, self.cache, self.fluster, 'none')
        self.assertTrue(any('hash mismatch' in issue for issue in issues), issues)

    def test_full_cache_without_unpinned_identities_fails(self):
        for vector in self.index.by_name:
            self.assertEqual(self.fetch(vector), 0)
        (self.cache / 'corpus-lock.json').unlink()
        self.tamper()
        issues = corpus.verify_cache(self.manifest, self.cache, self.fluster, 'all')
        self.assertTrue(any('identity' in issue for issue in issues), issues)

    def test_malformed_lock_is_not_treated_as_absent(self):
        (self.cache / 'corpus-lock.json').write_text('{broken')
        with self.assertRaisesRegex(corpus.CorpusError, 'lock'):
            corpus.load_lock(self.cache)

    def test_changed_unpinned_cache_without_lock_cannot_be_trusted(self):
        self.assertEqual(self.fetch(self.vector), 0)
        (self.cache / 'corpus-lock.json').unlink()
        self.tamper()
        with self.assertRaisesRegex(corpus.CorpusError, 'identity'):
            corpus.acquire(self.cache, self.index, self.vector, offline=True)

    def test_valid_reacquisition_preserves_identity(self):
        self.assertEqual(self.fetch(self.vector), 0)
        expected = corpus.load_lock(self.cache)[self.ref]
        corpus.asset_path(self.cache, self.index, self.vector).unlink()
        self.assertEqual(self.fetch(self.vector), 0)
        self.assertEqual(corpus.load_lock(self.cache)[self.ref], expected)
        self.assertEqual(corpus.verify_cache(self.manifest, self.cache, self.fluster, 'none'), [])


if __name__ == '__main__':
    unittest.main()
