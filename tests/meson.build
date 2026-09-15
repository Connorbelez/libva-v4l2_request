regression = executable('regression', 'regression.c', driver_sources,
  c_args: ['-UNDEBUG'],
  include_directories: [root_inc, include_directories('../src')],
  dependencies: [libva_dep, libdrm_dep, threads_dep],
  link_args: ['-Wl,--wrap=ioctl', '-Wl,--wrap=poll'])
foreach case : ['decode-error', 'export-error', 'reuse-timeout', 'reader-timeout',
                'refwait-timeout', 'export-timeout', 'flush-error',
                'image-dimensions', 'odd-image-copy', 'image-bounds', 'buffer-zero',
                'context-lifetime', 'context-error', 'context-timeout',
                'output-grown', 'request-timeout',
                'bitstream-overflow', 'derive-bounds']
  test(case, regression, args: [case], timeout: 10)
endforeach

if conf.get('HAVE_V4L2_CTRL_HEVC') == 1
  parser = executable('hevc-parser', 'hevc-parser.c', driver_sources,
    c_args: ['-UNDEBUG'],
    include_directories: [root_inc, include_directories('../src')],
    dependencies: [libva_dep, libdrm_dep, threads_dep])
  test('hevc-parser', parser, timeout: 30)
endif
