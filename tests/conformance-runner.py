#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise runner classification and summary failures without a decoder."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import conformance

DIGEST = 'ab' * 16


class Runner(unittest.TestCase):
    def run_case(self, *, hardware=True, log='', status=0, summary_error=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = root / 'suite.json'
            suite.write_text(json.dumps({'name': 'TINY', 'test_vectors': [{
                'name': 'sample', 'input_file': 'sample.h264',
                'output_format': 'yuv420p', 'result': DIGEST,
            }]}))
            output = root / 'result'
            argv = ['conformance.py', str(suite), str(root), '--output', str(output)]
            if hardware:
                argv += ['--driver', str(root / 'driver')]

            def decoded(*args, **kwargs):
                # frame-check prints the downloaded/hash format, even when
                # its input was a VAAPI surface. This is its real wire format.
                kwargs['stdout'].write('frame 0 64x48 yuv420p ' + DIGEST + '\nMD5=' + DIGEST + '\n')
                kwargs['stderr'].write(log)
                return Mock(wait=Mock(return_value=status))

            writer = conformance.write_summary
            if summary_error:
                writer = Mock(side_effect=ValueError('injected invalid summary'))
            with patch.object(sys, 'argv', argv), \
                 patch.dict(os.environ, {'LIBVA_HW_GUARD_LEASE': 'offline-fixture'}), \
                 patch.object(conformance, 'git_head', return_value='fixture'), \
                 patch.object(conformance.subprocess, 'check_output', return_value=''), \
                 patch.object(conformance.subprocess, 'run'), \
                 patch.object(conformance.subprocess, 'Popen', side_effect=decoded), \
                 patch.object(conformance, 'write_summary', side_effect=writer), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                code = conformance.main()
            summary = output / 'summary.json'
            return code, json.loads(summary.read_text()) if summary.exists() else None

    def test_downloaded_hardware_pixels_are_not_fallback(self):
        code, result = self.run_case(log='{"schema":"libva-v4l2request.diag/1","msg":"decoding h264"}\n')
        self.assertEqual(code, 0)
        self.assertIsNotNone(result)
        self.assertEqual(result['vectors'][0]['category'], 'hardware_pass')

    def test_software_mode_stays_software(self):
        code, result = self.run_case(hardware=False)
        self.assertEqual(code, 0)
        self.assertEqual(result['vectors'][0]['category'], 'software_pass')

    def test_fallback_cannot_pass_even_with_matching_digest(self):
        code, result = self.run_case(log='Failed setup for format vaapi: hwaccel initialisation returned error.')
        self.assertEqual(code, 1)
        self.assertFalse(result['vectors'][0]['success'])
        self.assertEqual(result['vectors'][0]['category'], 'software_fallback')

    def test_bad_summary_is_a_runner_failure(self):
        code, result = self.run_case(summary_error=True)
        self.assertEqual(code, 2)
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
