regression = executable('regression', 'regression.c', driver_sources,
  c_args: ['-UNDEBUG'],
  include_directories: [root_inc, include_directories('../src')],
  dependencies: [libva_dep, libdrm_dep, threads_dep],
  link_args: ['-Wl,--wrap=ioctl', '-Wl,--wrap=poll', '-Wl,--wrap=__poll_chk'])
foreach case : ['decode-error', 'submission-error', 'export-error', 'reuse-timeout', 'reader-timeout',
                'refwait-timeout', 'export-timeout', 'flush-error',
                'image-dimensions', 'odd-image-copy', 'image-bounds', 'buffer-zero',
                'context-lifetime', 'context-error', 'context-timeout',
                'output-grown', 'request-timeout',
                'bitstream-overflow', 'derive-bounds', 'invalid-poll']
  test(case, regression, args: [case], timeout: 10)
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

picture = executable('picture', 'picture.c', driver_sources,
  c_args: ['-UNDEBUG'],
  include_directories: [root_inc, include_directories('../src')],
  dependencies: [libva_dep, libdrm_dep, threads_dep])
foreach case : ['render-errors', 'begin-errors', 'target-lifetime', 'reference-owner', 'context-parameters']
  test('picture-' + case, picture, args: [case], timeout: 10)
endforeach

python3 = find_program('python3')
test('support-matrix', python3, args: files('support-matrix.py'), timeout: 30)
test('corpus-manifest', python3, args: [files('corpus.py'), 'self-test'], timeout: 60)
test('corpus-integrity', python3, args: files('corpus-integrity.py'), timeout: 30)
test('conformance-result', python3, args: [files('compare-results.py'), '--self-test'], timeout: 30)
test('hwguard', python3, args: [files('hwguard.py'), '--self-test'], timeout: 60)
test('rps-e-research', python3, args: files('rps-e-research.py'), timeout: 15)
test('hevc-concurrent', python3, args: [files('hevc-concurrent.py'), '--self-test'], timeout: 15)
test('hevc-concurrency-research', python3, args: files('hevc-concurrency-research.py'), timeout: 15)
