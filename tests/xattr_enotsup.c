/* Run a command with setxattr, lsetxattr, and fsetxattr returning ENOTSUP.
 *
 * The release binary is static, so this is a seccomp filter in front of
 * the exec, not an LD_PRELOAD and not a mount.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stddef.h>
#include <stdio.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef __NR_setxattr
#define __NR_setxattr 188
#endif
#ifndef __NR_lsetxattr
#define __NR_lsetxattr 189
#endif
#ifndef __NR_fsetxattr
#define __NR_fsetxattr 190
#endif

static int install_filter(void) {
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_setxattr, 3, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_lsetxattr, 2, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_fsetxattr, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | (ENOTSUP & SECCOMP_RET_DATA)),
    };
    struct sock_fprog prog = {
        .len = (unsigned short)(sizeof filter / sizeof filter[0]),
        .filter = filter,
    };
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        return -1;
    }
    return (int)syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, &prog);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: xattr_enotsup command [args]\n");
        return 127;
    }
    if (install_filter() != 0) {
        perror("seccomp");
        return 127;
    }
    execvp(argv[1], argv + 1);
    perror("exec");
    return 127;
}
