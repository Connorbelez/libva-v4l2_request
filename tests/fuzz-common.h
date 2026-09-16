/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Shared no-device fuzz budgets. Harness faults print "fuzz-harness:" and
 * _exit(99); sanitizer traps remain driver/parser defects. */
#ifndef FUZZ_COMMON_H
#define FUZZ_COMMON_H

#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/time.h>
#include <unistd.h>

#define FUZZ_MAX_INPUT		65536u
#define FUZZ_HARNESS_EXIT	99
#define FUZZ_ALLOC_BUDGET	(16u * 1024u * 1024u)

static inline void fuzz_harness_abort(const char *why)
{
	fprintf(stderr, "fuzz-harness: %s\n", why);
	_exit(FUZZ_HARNESS_EXIT);
}

static inline size_t fuzz_cap_size(size_t size)
{
	return size > FUZZ_MAX_INPUT ? FUZZ_MAX_INPUT : size;
}

/* libFuzzer defines this and supplies its own -timeout. Alarm only in replay. */
#ifndef FUZZING_BUILD_MODE_UNSAFE_FOR_PRODUCTION
static void fuzz_on_alarm(int sig)
{
	(void)sig;
	fuzz_harness_abort("timeout");
}

static inline void fuzz_begin_budget(void)
{
	struct rlimit core = {0, 0};
	struct itimerval it = {0};

	(void)setrlimit(RLIMIT_CORE, &core);
	signal(SIGALRM, fuzz_on_alarm);
	it.it_value.tv_sec = 1;
	(void)setitimer(ITIMER_REAL, &it, NULL);
}

static inline void fuzz_end_budget(void)
{
	struct itimerval it = {0};

	(void)setitimer(ITIMER_REAL, &it, NULL);
}
#else
static inline void fuzz_begin_budget(void)
{
	struct rlimit core = {0, 0};

	(void)setrlimit(RLIMIT_CORE, &core);
}

static inline void fuzz_end_budget(void)
{
}
#endif

#endif
