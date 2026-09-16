/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Standalone seed replay for LLVMFuzzerTestOneInput. No libFuzzer required. */
#include "fuzz-common.h"

#include <dirent.h>
#include <errno.h>
#include <sys/stat.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static int replay_buffer(const uint8_t *data, size_t size, int twice)
{
	LLVMFuzzerTestOneInput(data, size);
	if (twice)
		LLVMFuzzerTestOneInput(data, size);
	return 0;
}

static int replay_file(const char *path, int twice)
{
	FILE *f;
	uint8_t *buf;
	long n;
	int rc;

	f = fopen(path, "rb");
	if (!f) {
		fprintf(stderr, "fuzz-harness: open %s: %s\n", path, strerror(errno));
		return 1;
	}
	if (fseek(f, 0, SEEK_END) != 0) {
		fclose(f);
		return 1;
	}
	n = ftell(f);
	if (n < 0) {
		fclose(f);
		return 1;
	}
	rewind(f);
	buf = malloc((size_t)n + 1);
	if (!buf) {
		fclose(f);
		fuzz_harness_abort("alloc");
	}
	if (n && fread(buf, 1, (size_t)n, f) != (size_t)n) {
		free(buf);
		fclose(f);
		fprintf(stderr, "fuzz-harness: read %s\n", path);
		return 1;
	}
	fclose(f);
	rc = replay_buffer(buf, (size_t)n, twice);
	free(buf);
	return rc;
}

static int replay_dir(const char *path, int twice)
{
	DIR *dir;
	struct dirent *ent;
	int rc = 0, seen = 0;

	dir = opendir(path);
	if (!dir) {
		fprintf(stderr, "fuzz-harness: opendir %s: %s\n", path, strerror(errno));
		return 1;
	}
	while ((ent = readdir(dir))) {
		char child[1024];
		struct stat st;

		if (ent->d_name[0] == '.')
			continue;
		if (snprintf(child, sizeof(child), "%s/%s", path, ent->d_name) >= (int)sizeof(child))
			continue;
		if (stat(child, &st) != 0 || !S_ISREG(st.st_mode))
			continue;
		seen++;
		if (replay_file(child, twice))
			rc = 1;
	}
	closedir(dir);
	if (!seen) {
		fprintf(stderr, "fuzz-harness: no seeds in %s\n", path);
		return 1;
	}
	return rc;
}

static int self_test(void)
{
	uint8_t empty[1];
	uint8_t *huge;
	uint8_t tiny[8] = {0, 1, 2, 3, 4, 5, 6, 7};

	replay_buffer(empty, 0, 1);
	huge = calloc(1, FUZZ_MAX_INPUT * 2);
	if (!huge)
		fuzz_harness_abort("alloc");
	memset(huge, 0xa5, FUZZ_MAX_INPUT * 2);
	replay_buffer(huge, FUZZ_MAX_INPUT * 2, 0);
	free(huge);
	replay_buffer(tiny, sizeof(tiny), 1);
	return 0;
}

int main(int argc, char **argv)
{
	struct rlimit core = {0, 0};
	int twice = 1;
	int i, rc = 0;
	struct stat st;

	(void)setrlimit(RLIMIT_CORE, &core);
	if (argc < 2) {
		fprintf(stderr, "usage: %s [--once] <seed-file-or-dir>...\n"
				"       %s --self-test\n", argv[0], argv[0]);
		return 2;
	}
	if (argc == 2 && !strcmp(argv[1], "--self-test"))
		return self_test();
	i = 1;
	if (!strcmp(argv[1], "--once")) {
		twice = 0;
		i = 2;
	}
	if (i >= argc)
		return 2;
	for (; i < argc; i++) {
		if (stat(argv[i], &st) != 0) {
			fprintf(stderr, "fuzz-harness: stat %s: %s\n", argv[i], strerror(errno));
			return 1;
		}
		if (S_ISDIR(st.st_mode))
			rc |= replay_dir(argv[i], twice);
		else
			rc |= replay_file(argv[i], twice);
	}
	return rc;
}
