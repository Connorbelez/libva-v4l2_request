/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "fuzz-common.h"

struct fuzz_oracle fuzz_oracle_last;

void fuzz_oracle_reset(void)
{
	memset(&fuzz_oracle_last, 0, sizeof(fuzz_oracle_last));
}

void fuzz_oracle_mix(uint64_t v)
{
	fuzz_oracle_last.digest ^= v + 0x9e3779b97f4a7c15ULL +
		(fuzz_oracle_last.digest << 6) + (fuzz_oracle_last.digest >> 2);
}

int fuzz_replay_once(const uint8_t *data, size_t size, struct fuzz_oracle *out)
{
	fuzz_oracle_reset();
	LLVMFuzzerTestOneInput(data, size);
	if (out)
		*out = fuzz_oracle_last;
	return 0;
}

int fuzz_replay_identity(const uint8_t *data, size_t size)
{
	struct fuzz_oracle a, b;

	fuzz_replay_once(data, size, &a);
	fuzz_replay_once(data, size, &b);
	if (memcmp(&a, &b, sizeof(a)) != 0) {
		fprintf(stderr,
			"fuzz-harness: replay-mismatch digest %llu vs %llu flags %u vs %u\n",
			(unsigned long long)a.digest, (unsigned long long)b.digest,
			a.flags, b.flags);
		fuzz_harness_abort("replay-mismatch");
	}
	fuzz_oracle_last = a;
	return 0;
}
