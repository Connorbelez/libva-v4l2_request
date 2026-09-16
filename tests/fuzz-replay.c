/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Standalone seed replay for LLVMFuzzerTestOneInput. No libFuzzer required. */
#include "fuzz-common.h"

#include <dirent.h>
#include <errno.h>
#include <sys/stat.h>
#include <unistd.h>

static int harness_io(const char *why)
{
	fprintf(stderr, "fuzz-harness: %s\n", why);
	return FUZZ_HARNESS_EXIT;
}

static const char *seed_basename(const char *path)
{
	const char *base = strrchr(path, '/');

	return base ? base + 1 : path;
}

static void check_expected(const char *path)
{
	const char *base = seed_basename(path);
	const struct fuzz_oracle *o = &fuzz_oracle_last;

	if (!strcmp(base, "valid-idr")) {
		if (o->nal_unit_type != 5 && o->nal_unit_type != 19)
			fuzz_harness_abort("expected-idr-nal");
		return;
	}
	if (!strcmp(base, "pred-weight-vla")) {
		if (!(o->flags & FUZZ_ORACLE_PRED_WEIGHT) || !(o->flags & FUZZ_ORACLE_ERROR))
			fuzz_harness_abort("expected-pred-weight-guard");
		return;
	}
	if (!strcmp(base, "ue-overflow")) {
		if (!(o->flags & FUZZ_ORACLE_UE) || !(o->flags & FUZZ_ORACLE_ERROR))
			fuzz_harness_abort("expected-ue-overflow-guard");
		return;
	}
	if (!strcmp(base, "vps-as-slice") || !strcmp(base, "sps-as-slice") ||
	    !strcmp(base, "truncated-nal") || !strcmp(base, "truncated-1") ||
	    !strcmp(base, "truncated-3") || !strcmp(base, "forbidden-bit") ||
	    !strcmp(base, "bad-marker") || !strcmp(base, "zeros")) {
		if (o->flags & FUZZ_ORACLE_VALID)
			fuzz_harness_abort("expected-reject");
	}
}

static int replay_buffer(const uint8_t *data, size_t size, int twice)
{
	if (twice)
		return fuzz_replay_identity(data, size);
	return fuzz_replay_once(data, size, NULL);
}

static int replay_file(const char *path, int twice)
{
	FILE *f;
	uint8_t *buf;
	struct stat st;
	size_t want, got;
	int rc;

	if (stat(path, &st) != 0)
		return harness_io(strerror(errno));
	if (!S_ISREG(st.st_mode))
		return harness_io("not a regular file");
	if (st.st_size < 0)
		return harness_io("negative size");
	want = (unsigned long long)st.st_size > FUZZ_MAX_INPUT ?
	       FUZZ_MAX_INPUT : (size_t)st.st_size;

	f = fopen(path, "rb");
	if (!f)
		return harness_io(strerror(errno));
	buf = malloc(want + 1);
	if (!buf) {
		fclose(f);
		fuzz_harness_abort("alloc");
	}
	got = want ? fread(buf, 1, want, f) : 0;
	if (ferror(f) || got != want) {
		free(buf);
		fclose(f);
		return harness_io("read");
	}
	fclose(f);
	rc = replay_buffer(buf, want, twice);
	if (twice)
		check_expected(path);
	free(buf);
	return rc;
}

static int replay_dir(const char *path, int twice)
{
	DIR *dir;
	struct dirent *ent;
	int rc = 0, seen = 0;

	dir = opendir(path);
	if (!dir)
		return harness_io(strerror(errno));
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
			rc = FUZZ_HARNESS_EXIT;
	}
	closedir(dir);
	if (!seen)
		return harness_io("no seeds");
	return rc;
}

static int self_test(void)
{
	uint8_t empty[1];
	uint8_t *huge;
	uint8_t tiny[8] = {0, 1, 2, 3, 4, 5, 6, 7};
	char tmpl[] = "/tmp/fuzz-budget-XXXXXX";
	int fd, io_rc;
	void (*handler)(int);

	replay_buffer(empty, 0, 1);
	huge = calloc(1, FUZZ_MAX_INPUT * 2);
	if (!huge)
		fuzz_harness_abort("alloc");
	memset(huge, 0xa5, FUZZ_MAX_INPUT * 2);
	replay_buffer(huge, FUZZ_MAX_INPUT * 2, 0);
	free(huge);
	replay_buffer(tiny, sizeof(tiny), 1);

	fd = mkstemp(tmpl);
	if (fd < 0)
		fuzz_harness_abort("alloc");
	/* Logical 256 MiB sparse file: the bound must apply before allocation. */
	if (ftruncate(fd, (off_t)256 * 1024 * 1024) != 0) {
		close(fd);
		unlink(tmpl);
		fuzz_harness_abort("alloc");
	}
	close(fd);
	if (replay_file(tmpl, 0) != 0) {
		unlink(tmpl);
		fuzz_harness_abort("oversize-file");
	}
	unlink(tmpl);

	io_rc = replay_file("/no/such/fuzz-seed", 0);
	if (io_rc != FUZZ_HARNESS_EXIT)
		fuzz_harness_abort("io-missing");
	io_rc = replay_file("/dev/null", 0);
	if (io_rc != FUZZ_HARNESS_EXIT)
		fuzz_harness_abort("io-nonregular");

	fuzz_begin_budget();
	handler = signal(SIGALRM, SIG_DFL);
	if (handler == SIG_DFL || handler == SIG_IGN || handler == SIG_ERR)
		fuzz_harness_abort("replay-alarm");
	signal(SIGALRM, handler);
	fuzz_end_budget();
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
		if (stat(argv[i], &st) != 0)
			return harness_io(strerror(errno));
		if (S_ISDIR(st.st_mode))
			rc |= replay_dir(argv[i], twice);
		else if (S_ISREG(st.st_mode))
			rc |= replay_file(argv[i], twice);
		else
			return harness_io("not a regular file");
	}
	return rc;
}
