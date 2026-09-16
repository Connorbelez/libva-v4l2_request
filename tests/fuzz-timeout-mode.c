/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Prove replay installs SIGALRM and campaign leaves it for libFuzzer. */
#include "fuzz-common.h"

int main(void)
{
	void (*after)(int);

#ifdef FUZZ_REPLAY_BUILD
	fuzz_begin_budget();
	after = signal(SIGALRM, SIG_DFL);
	if (after == SIG_DFL || after == SIG_IGN || after == SIG_ERR) {
		fprintf(stderr, "fuzz-harness: replay did not install SIGALRM\n");
		return FUZZ_HARNESS_EXIT;
	}
	fuzz_end_budget();
	return 0;
#else
	(void)signal(SIGALRM, SIG_IGN);
	fuzz_begin_budget();
	after = signal(SIGALRM, SIG_DFL);
	if (after != SIG_IGN) {
		fprintf(stderr, "fuzz-harness: campaign installed SIGALRM\n");
		return FUZZ_HARNESS_EXIT;
	}
	fuzz_end_budget();
	return 0;
#endif
}
