#define FUSE_USE_VERSION 31
#define _GNU_SOURCE

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <fuse3/fuse.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/xattr.h>
#include <unistd.h>

/* Passthrough of one directory. FAULTFS_FALLOCATE=eio makes fallocate
 * return EIO. FAULTFS_SEAL is one absolute mount path whose mode is
 * reported without a write bit and whose chmod returns EPERM.
 * FAULTFS_FSYNC_DIR is one mount path whose directory flush returns
 * EIO. FAULTFS_FSYNC_DIR_SKIP is how many of those flushes succeed
 * before the failure, so an earlier flush can stay durable. */

static char backing[PATH_MAX];
static char seal[PATH_MAX];
static int fallocate_eio;
static int fsync_dir_eio;
static char fsync_dir[PATH_MAX];
static int fsync_dir_skip;

static int join(char *out, size_t outlen, const char *path) {
	int n = snprintf(out, outlen, "%s%s", backing, path);
	if (n < 0 || (size_t)n >= outlen) return -ENAMETOOLONG;
	return 0;
}

static int sealed(const char *path) {
	return seal[0] != '\0' && strcmp(path, seal) == 0;
}

static void *fs_init(struct fuse_conn_info *conn, struct fuse_config *cfg) {
	(void)conn;
	cfg->use_ino = 1;
	cfg->entry_timeout = 0;
	cfg->attr_timeout = 0;
	cfg->negative_timeout = 0;
	cfg->nullpath_ok = 0;
	return NULL;
}

static int fs_getattr(const char *path, struct stat *st, struct fuse_file_info *fi) {
	(void)fi;
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (lstat(full, st) < 0) return -errno;
	if (sealed(path)) st->st_mode &= ~(mode_t)(S_IWUSR | S_IWGRP | S_IWOTH);
	return 0;
}

static int fs_readlink(const char *path, char *buf, size_t size) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	ssize_t n = readlink(full, buf, size - 1);
	if (n < 0) return -errno;
	buf[n] = '\0';
	return 0;
}

static int fs_mknod(const char *path, mode_t mode, dev_t dev) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (mknod(full, mode, dev) < 0) return -errno;
	return 0;
}

static int fs_mkdir(const char *path, mode_t mode) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (mkdir(full, mode) < 0) return -errno;
	return 0;
}

static int fs_unlink(const char *path) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (unlink(full) < 0) return -errno;
	return 0;
}

static int fs_rmdir(const char *path) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (rmdir(full) < 0) return -errno;
	return 0;
}

static int fs_symlink(const char *target, const char *path) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (symlink(target, full) < 0) return -errno;
	return 0;
}

static int fs_rename(const char *from, const char *to, unsigned int flags) {
	char a[PATH_MAX], b[PATH_MAX];
	int rc = join(a, sizeof a, from);
	if (rc) return rc;
	rc = join(b, sizeof b, to);
	if (rc) return rc;
	if (renameat2(AT_FDCWD, a, AT_FDCWD, b, flags) < 0) return -errno;
	return 0;
}

static int fs_link(const char *from, const char *to) {
	char a[PATH_MAX], b[PATH_MAX];
	int rc = join(a, sizeof a, from);
	if (rc) return rc;
	rc = join(b, sizeof b, to);
	if (rc) return rc;
	if (link(a, b) < 0) return -errno;
	return 0;
}

static int fs_chmod(const char *path, mode_t mode, struct fuse_file_info *fi) {
	(void)fi;
	if (sealed(path)) return -EPERM;
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (chmod(full, mode) < 0) return -errno;
	return 0;
}

static int fs_chown(const char *path, uid_t uid, gid_t gid, struct fuse_file_info *fi) {
	(void)fi;
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (lchown(full, uid, gid) < 0) return -errno;
	return 0;
}

static int fs_truncate(const char *path, off_t size, struct fuse_file_info *fi) {
	if (fi != NULL) {
		if (ftruncate((int)fi->fh, size) < 0) return -errno;
		return 0;
	}
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (truncate(full, size) < 0) return -errno;
	return 0;
}

static int fs_utimens(const char *path, const struct timespec tv[2], struct fuse_file_info *fi) {
	(void)fi;
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (utimensat(AT_FDCWD, full, tv, AT_SYMLINK_NOFOLLOW) < 0) return -errno;
	return 0;
}

static int fs_open(const char *path, struct fuse_file_info *fi) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	int fd = open(full, fi->flags);
	if (fd < 0) return -errno;
	fi->fh = (uint64_t)fd;
	return 0;
}

static int fs_create(const char *path, mode_t mode, struct fuse_file_info *fi) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	int fd = open(full, fi->flags, mode);
	if (fd < 0) return -errno;
	fi->fh = (uint64_t)fd;
	return 0;
}

static int fs_read(const char *path, char *buf, size_t size, off_t off, struct fuse_file_info *fi) {
	(void)path;
	ssize_t n = pread((int)fi->fh, buf, size, off);
	if (n < 0) return -errno;
	return (int)n;
}

static int fs_write(const char *path, const char *buf, size_t size, off_t off, struct fuse_file_info *fi) {
	(void)path;
	ssize_t n = pwrite((int)fi->fh, buf, size, off);
	if (n < 0) return -errno;
	return (int)n;
}

static int fs_flush(const char *path, struct fuse_file_info *fi) {
	(void)path;
	int fd = dup((int)fi->fh);
	if (fd < 0) return -errno;
	int rc = close(fd);
	return rc < 0 ? -errno : 0;
}

static int fs_release(const char *path, struct fuse_file_info *fi) {
	(void)path;
	if (close((int)fi->fh) < 0) return -errno;
	return 0;
}

static int fs_fsync(const char *path, int datasync, struct fuse_file_info *fi) {
	(void)path;
	int rc = datasync ? fdatasync((int)fi->fh) : fsync((int)fi->fh);
	if (rc < 0) return -errno;
	return 0;
}

static int fs_fsyncdir(const char *path, int datasync, struct fuse_file_info *fi) {
	(void)datasync;
	(void)fi;
	if (fsync_dir_eio && strcmp(path, fsync_dir) == 0) {
		if (fsync_dir_skip > 0) {
			fsync_dir_skip -= 1;
		} else {
			return -EIO;
		}
	}
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	int fd = open(full, O_RDONLY | O_DIRECTORY);
	if (fd < 0) return -errno;
	rc = fsync(fd);
	int err = errno;
	close(fd);
	return rc < 0 ? -err : 0;
}

static int fs_fallocate(const char *path, int mode, off_t offset, off_t length, struct fuse_file_info *fi) {
	(void)path;
	if (fallocate_eio) return -EIO;
	if (fallocate((int)fi->fh, mode, offset, length) < 0) return -errno;
	return 0;
}

static int fs_opendir(const char *path, struct fuse_file_info *fi) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	DIR *dir = opendir(full);
	if (dir == NULL) return -errno;
	fi->fh = (uint64_t)(uintptr_t)dir;
	return 0;
}

static int fs_readdir(const char *path, void *buf, fuse_fill_dir_t filler, off_t off, struct fuse_file_info *fi, enum fuse_readdir_flags flags) {
	(void)path;
	(void)off;
	(void)flags;
	DIR *dir = (DIR *)(uintptr_t)fi->fh;
	rewinddir(dir);
	struct dirent *ent;
	while ((ent = readdir(dir)) != NULL) {
		if (filler(buf, ent->d_name, NULL, 0, 0) != 0) break;
	}
	return 0;
}

static int fs_releasedir(const char *path, struct fuse_file_info *fi) {
	(void)path;
	DIR *dir = (DIR *)(uintptr_t)fi->fh;
	if (closedir(dir) < 0) return -errno;
	return 0;
}

static int fs_access(const char *path, int mask) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (sealed(path) && (mask & W_OK)) return -EACCES;
	if (access(full, mask) < 0) return -errno;
	return 0;
}

static int fs_setxattr(const char *path, const char *name, const char *value, size_t size, int flags) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (lsetxattr(full, name, value, size, flags) < 0) return -errno;
	return 0;
}

static int fs_getxattr(const char *path, const char *name, char *value, size_t size) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	ssize_t n = lgetxattr(full, name, value, size);
	if (n < 0) return -errno;
	return (int)n;
}

static int fs_listxattr(const char *path, char *list, size_t size) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	ssize_t n = llistxattr(full, list, size);
	if (n < 0) return -errno;
	return (int)n;
}

static int fs_removexattr(const char *path, const char *name) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (lremovexattr(full, name) < 0) return -errno;
	return 0;
}

static int fs_statfs(const char *path, struct statvfs *st) {
	char full[PATH_MAX];
	int rc = join(full, sizeof full, path);
	if (rc) return rc;
	if (statvfs(full, st) < 0) return -errno;
	return 0;
}

static const struct fuse_operations ops = {
	.init = fs_init,
	.getattr = fs_getattr,
	.readlink = fs_readlink,
	.mknod = fs_mknod,
	.mkdir = fs_mkdir,
	.unlink = fs_unlink,
	.rmdir = fs_rmdir,
	.symlink = fs_symlink,
	.rename = fs_rename,
	.link = fs_link,
	.chmod = fs_chmod,
	.chown = fs_chown,
	.truncate = fs_truncate,
	.utimens = fs_utimens,
	.open = fs_open,
	.create = fs_create,
	.read = fs_read,
	.write = fs_write,
	.flush = fs_flush,
	.release = fs_release,
	.fsync = fs_fsync,
	.fsyncdir = fs_fsyncdir,
	.fallocate = fs_fallocate,
	.opendir = fs_opendir,
	.readdir = fs_readdir,
	.releasedir = fs_releasedir,
	.access = fs_access,
	.setxattr = fs_setxattr,
	.getxattr = fs_getxattr,
	.listxattr = fs_listxattr,
	.removexattr = fs_removexattr,
	.statfs = fs_statfs,
};

int main(int argc, char **argv) {
	if (argc != 3) {
		fprintf(stderr, "usage: faultfs <backing> <mount>\n");
		return 2;
	}
	if (realpath(argv[1], backing) == NULL) {
		perror("backing");
		return 1;
	}
	const char *want = getenv("FAULTFS_FALLOCATE");
	fallocate_eio = want != NULL && strcmp(want, "eio") == 0;
	const char *dir_sync = getenv("FAULTFS_FSYNC_DIR");
	fsync_dir[0] = '\0';
	fsync_dir_eio = 0;
	fsync_dir_skip = 0;
	if (dir_sync != NULL && dir_sync[0] != '\0') {
		if (strlen(dir_sync) >= sizeof fsync_dir) return 1;
		memcpy(fsync_dir, dir_sync, strlen(dir_sync) + 1);
		fsync_dir_eio = 1;
		const char *skip = getenv("FAULTFS_FSYNC_DIR_SKIP");
		if (skip != NULL && skip[0] != '\0') fsync_dir_skip = atoi(skip);
	}
	const char *sealed_path = getenv("FAULTFS_SEAL");
	seal[0] = '\0';
	if (sealed_path != NULL && sealed_path[0] != '\0') {
		if (strlen(sealed_path) >= sizeof seal) return 1;
		memcpy(seal, sealed_path, strlen(sealed_path) + 1);
	}
	char *fuse_argv[] = {"faultfs", "-f", "-s", argv[2], NULL};
	return fuse_main(4, fuse_argv, &ops, NULL);
}
