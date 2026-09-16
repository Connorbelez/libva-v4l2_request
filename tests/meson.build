regression = executable('regression', 'regression.c', driver_sources,
  c_args: ['-UNDEBUG'],
  include_directories: [root_inc, include_directories('../src')],
  dependencies: [libva_dep, libdrm_dep, threads_dep],
  link_args: ['-Wl,--wrap=ioctl', '-Wl,--wrap=poll', '-Wl,--wrap=__poll_chk'])
regression_cases = ['decode-error', 'submission-error', 'export-error', 'reuse-timeout', 'reader-timeout',
                    'refwait-timeout', 'export-timeout', 'flush-error',
                    'image-dimensions', 'odd-image-copy', 'buffer-zero',
                    'context-lifetime', 'context-error', 'context-timeout',
                    'output-grown', 'request-timeout',
                    'bitstream-overflow', 'derive-bounds', 'invalid-poll']
# image-bounds needs the P010 capture format from Linux 6.0 UAPI headers.
if conf.get('HAVE_V4L2_PIX_FMT_P010') == 1
  regression_cases += 'image-bounds'
endif
foreach case : regression_cases
  test(case, regression, args: [case], timeout: 10)
endforeach

diagnostics = executable('diagnostics', 'diagnostics.c', driver_sources,
  c_args: ['-UNDEBUG'],
  include_directories: [root_inc, include_directories('../src')],
  dependencies: [libva_dep, libdrm_dep, threads_dep],
  link_args: ['-Wl,--wrap=ioctl', '-Wl,--wrap=poll', '-Wl,--wrap=__poll_chk'])
foreach case : ['categories', 'context-identity', 'redaction', 'rate-limit', 'overhead']
  test('diag-' + case, diagnostics, args: [case], timeout: 30)
endforeach

if conf.get('HAVE_V4L2_CTRL_HEVC') == 1
  parser = executable('hevc-parser', 'hevc-parser.c', driver_sources,
    c_args: ['-UNDEBUG'],
    include_directories: [root_inc, include_directories('../src')],
    dependencies: [libva_dep, libdrm_dep, threads_dep])
  test('hevc-parser', parser, timeout: 30)
endif

if conf.get('HAVE_V4L2_CTRL_VP9') == 1
  vp9 = executable('vp9', 'vp9.c', driver_sources,
    c_args: ['-UNDEBUG'],
    include_directories: [root_inc, include_directories('../src')],
    dependencies: [libva_dep, libdrm_dep, threads_dep])
  foreach case : ['malformed', 'state', 'parser', 'references']
    test('vp9-' + case, vp9, args: [case], timeout: 30)
  endforeach
endif

if conf.get('HAVE_V4L2_CTRL_H264') == 1
  submission = executable('h264-submission', 'h264-submission.c', driver_sources,
    c_args: ['-UNDEBUG'],
    include_directories: [root_inc, include_directories('../src')],
    dependencies: [libva_dep, libdrm_dep, threads_dep])
  foreach case : ['incomplete', 'slice-count', 'references']
    test('h264-' + case, submission, args: [case], timeout: 10)
  endforeach
  foreach case : ['h264-parser', 'h264-profile']
    check = executable(case, case + '.c', driver_sources,
      c_args: ['-UNDEBUG'],
      include_directories: [root_inc, include_directories('../src')],
      dependencies: [libva_dep, libdrm_dep, threads_dep],
      link_args: case == 'h264-profile' ? ['-Wl,--wrap=ioctl'] : [])
    test(case, check, timeout: 30)
  endforeach
endif

api_lifecycle = executable('api-lifecycle', 'api-lifecycle.c', driver_sources,
  c_args: ['-UNDEBUG'],
  include_directories: [root_inc, include_directories('../src')],
  dependencies: [libva_dep, libdrm_dep, threads_dep])
foreach case : ['surface-duplicates', 'surface-parameters', 'surface-null-output',
                'surface-query-null', 'surface-export-null', 'surface-attributes-null',
                'config-parameters', 'config-null-output', 'config-query-null',
                'buffer-null-output', 'buffer-null-map', 'buffer-context',
                'buffer-owner', 'buffer-recycled-context', 'image-buffer-lifetime',
                'image-derived-null', 'image-query-null', 'context-abort-reuse',
                'context-isolation']
  test('api-' + case, api_lifecycle, args: [case], timeout: 10)
endforeach

picture = executable('picture', 'picture.c', driver_sources,
  c_args: ['-UNDEBUG'],
  include_directories: [root_inc, include_directories('../src')],
  dependencies: [libva_dep, libdrm_dep, threads_dep])
foreach case : ['render-errors', 'begin-errors', 'target-lifetime', 'reference-owner', 'context-parameters']
  test('picture-' + case, picture, args: [case], timeout: 10)
endforeach

if conf.get('HAVE_V4L2_PIX_FMT_P010') == 1 and (conf.get('HAVE_V4L2_CTRL_H264') == 1 or conf.get('HAVE_V4L2_CTRL_HEVC') == 1)
  surface_probe = executable('surface-probe', 'surface-probe.c', driver_sources,
    c_args: ['-UNDEBUG'],
    include_directories: [root_inc, include_directories('../src')],
    dependencies: [libva_dep, libdrm_dep, threads_dep],
    link_args: ['-Wl,--wrap=ioctl'])
  test('surface-probe', surface_probe, timeout: 10)
endif

python3 = find_program('python3')
test('support-matrix', python3, args: files('support-matrix.py'), timeout: 30)
test('corpus-manifest', python3, args: [files('corpus.py'), 'self-test'], timeout: 60)
test('corpus-integrity', python3, args: files('corpus-integrity.py'), timeout: 30)
test('conformance-result', python3, args: [files('compare-results.py'), '--self-test'], timeout: 30)
test('conformance-runner', python3, args: [files('conformance-runner.py')], timeout: 30)
test('diag-schema', python3, args: [files('diag-schema.py'), diagnostics], timeout: 30)
test('hwguard', python3, args: [files('hwguard.py'), '--self-test'], timeout: 60)
test('rps-e-research', python3, args: files('rps-e-research.py'), timeout: 15)
test('hevc-concurrent', python3, args: [files('hevc-concurrent.py'), '--self-test'], timeout: 15)
test('hevc-concurrency-research', python3, args: files('hevc-concurrency-research.py'), timeout: 15)

# Offline fixtures and wiring audit for the CI aggregate gate.  The audit
# checks the actual .github/workflows/checks.yml, so every environment this
# suite runs in revalidates the required-check wiring.
test('ci-aggregate-fixtures', python3, args: [files('ci_check.py'), 'fixtures'], timeout: 30)
test('ci-workflow-audit', python3, args: [files('ci_check.py'), 'audit', '--workflow', workflow_yml], timeout: 15)
