/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Non-deterministic stub: two calls yield different oracles. Replay identity
 * must treat that as a harness failure (exit 99), not success. */
#include "fuzz-common.h"

#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static int calls;

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
	(void)data;
	(void)size;
	fuzz_oracle_reset();
	fuzz_oracle_last.digest = (uint64_t)++calls;
	return 0;
}

int main(void)
{
	uint8_t byte = 0;
	struct fuzz_oracle a, b;
	pid_t pid;
	int status;

	fuzz_replay_once(&byte, 1, &a);
	fuzz_replay_once(&byte, 1, &b);
	if (a.digest == b.digest) {
		fprintf(stderr, "fuzz-harness: stub oracle did not change\n");
		return 1;
	}

	calls = 0;
	pid = fork();
	if (pid < 0) {
		fprintf(stderr, "fuzz-harness: fork\n");
		return 1;
	}
	if (pid == 0) {
		fuzz_replay_identity(&byte, 1);
		_exit(0);
	}
	if (waitpid(pid, &status, 0) != pid) {
		fprintf(stderr, "fuzz-harness: waitpid\n");
		return 1;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != FUZZ_HARNESS_EXIT) {
		fprintf(stderr, "fuzz-harness: identity returned %d, want %d\n",
			WIFEXITED(status) ? WEXITSTATUS(status) : -1, FUZZ_HARNESS_EXIT);
		return 1;
	}
	return 0;
}
