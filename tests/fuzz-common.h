/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Shared no-device fuzz budgets. Harness faults print "fuzz-harness:" and
 * _exit(99); sanitizer traps remain driver/parser defects.
 *
 * Replay binaries are compiled with -DFUZZ_REPLAY_BUILD and install a 1s
 * ITIMER_REAL alarm. Campaign binaries are compiled with
 * -DFUZZ_CAMPAIGN_BUILD and must not touch SIGALRM: libFuzzer's -timeout
 * writes its own artifact. -fsanitize=fuzzer does not define
 * FUZZING_BUILD_MODE_UNSAFE_FOR_PRODUCTION on the supported toolchain. */
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

#define FUZZ_ORACLE_VALID		(1u << 0)
#define FUZZ_ORACLE_ERROR		(1u << 1)
#define FUZZ_ORACLE_PRED_WEIGHT		(1u << 2)
#define FUZZ_ORACLE_UE			(1u << 3)

struct fuzz_oracle {
	uint64_t digest;
	uint32_t flags;
	uint32_t nal_unit_type;
	uint32_t extra;
};

extern struct fuzz_oracle fuzz_oracle_last;

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);
void fuzz_oracle_reset(void);
void fuzz_oracle_mix(uint64_t v);
int fuzz_replay_once(const uint8_t *data, size_t size, struct fuzz_oracle *out);
int fuzz_replay_identity(const uint8_t *data, size_t size);

static inline void fuzz_harness_abort(const char *why)
{
	fprintf(stderr, "fuzz-harness: %s\n", why);
	_exit(FUZZ_HARNESS_EXIT);
}

static inline size_t fuzz_cap_size(size_t size)
{
	return size > FUZZ_MAX_INPUT ? FUZZ_MAX_INPUT : size;
}

#ifdef FUZZ_REPLAY_BUILD
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
/* Campaign and any TU without FUZZ_REPLAY_BUILD: leave SIGALRM to libFuzzer. */
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
