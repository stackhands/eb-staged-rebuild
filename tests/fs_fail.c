/* Trace the root process and fail one filesystem call.

   Usage:
     fs_fail log ROOT -- argv...
     fs_fail ERRNO OP NTH NEEDLE -- argv...

   OP is open, write, fsync, mkdir, unlink, flock or rename.
   NTH is the 1-based call of that OP whose path contains NEEDLE.
   A write, fsync or flock is named by the path of the descriptor.
   ERRNO is injected once. The release binary is static, so this
   program has to be the parent. It does not read a tool variable.
*/
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <elf.h>
#include <fcntl.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <unistd.h>

/* Registers and syscall numbers differ per architecture. Each call
   site uses the names below. A name absent on an architecture is -1,
   which never matches a real syscall number. */
#if defined(__x86_64__)
typedef struct user_regs_struct regs_t;
#define REG_NR(r) ((long)(r)->orig_rax)
#define REG_A0(r) ((r)->rdi)
#define REG_A1(r) ((r)->rsi)
#define REG_A2(r) ((r)->rdx)
#define REG_A3(r) ((r)->r10)
#define REG_RET(r) ((r)->rax)
#elif defined(__aarch64__)
typedef struct user_regs_struct regs_t;
#define REG_NR(r) ((long)(r)->regs[8])
#define REG_A0(r) ((r)->regs[0])
#define REG_A1(r) ((r)->regs[1])
#define REG_A2(r) ((r)->regs[2])
#define REG_A3(r) ((r)->regs[3])
#define REG_RET(r) ((r)->regs[0])
#ifndef NT_ARM_SYSTEM_CALL
#define NT_ARM_SYSTEM_CALL 0x404
#endif
#else
#error "fs_fail.c supports x86_64 and aarch64 Linux"
#endif

#ifdef SYS_open
#define N_OPEN SYS_open
#else
#define N_OPEN (-1)
#endif
#ifdef SYS_mkdir
#define N_MKDIR SYS_mkdir
#else
#define N_MKDIR (-1)
#endif
#ifdef SYS_unlink
#define N_UNLINK SYS_unlink
#else
#define N_UNLINK (-1)
#endif
#ifdef SYS_rename
#define N_RENAME SYS_rename
#else
#define N_RENAME (-1)
#endif
#ifdef SYS_dup2
#define N_DUP2 SYS_dup2
#else
#define N_DUP2 (-1)
#endif
#ifdef SYS_renameat
#define N_RENAMEAT SYS_renameat
#else
#define N_RENAMEAT (-1)
#endif
#ifdef SYS_openat2
#define N_OPENAT2 SYS_openat2
#else
#define N_OPENAT2 437
#endif
#ifdef SYS_pwritev2
#define N_PWRITEV2 SYS_pwritev2
#else
#define N_PWRITEV2 (-1)
#endif

enum { OP_LOG = 0, OP_OPEN, OP_WRITE, OP_FSYNC, OP_MKDIR, OP_UNLINK, OP_FLOCK, OP_RENAME, OP_FCNTL };
enum { FDMAX = 256 };

static int fail_errno, op, nth, seen, fired, poisoned;
static const char *needle;
static const char *root;
static int fds[FDMAX];
static char paths[FDMAX][768];
static int nfds;

static int read_str(pid_t pid, unsigned long addr, char *buf, size_t n) {
    struct iovec local = {buf, n};
    struct iovec remote = {(void *)addr, n};
    memset(buf, 0, n);
    if (process_vm_readv(pid, &local, 1, &remote, 1, 0) <= 0) return -1;
    buf[n - 1] = 0;
    return 0;
}

static int fd_slot(int fd) {
    for (int i = 0; i < nfds; i++) if (fds[i] == fd) return i;
    return -1;
}

static void remember(int fd, const char *path) {
    if (fd < 0) return;
    int s = fd_slot(fd);
    if (s < 0) {
        if (nfds >= FDMAX) return;
        s = nfds++;
        fds[s] = fd;
    }
    snprintf(paths[s], sizeof paths[s], "%s", path);
}

static void forget(int fd) {
    int s = fd_slot(fd);
    if (s < 0) return;
    fds[s] = fds[nfds - 1];
    memcpy(paths[s], paths[nfds - 1], sizeof paths[s]);
    nfds--;
}

static const char *path_of(int fd) {
    int s = fd_slot(fd);
    return s < 0 ? NULL : paths[s];
}

static int wanted(const char *path) {
    if (!path || !path[0]) return 0;
    if (root && root[0] && !strstr(path, root)) return 0;
    if (op != OP_LOG && needle && needle[0] && !strstr(path, needle)) return 0;
    return 1;
}

static int is_open(long nr) {
    return nr == N_OPEN || nr == SYS_openat || nr == N_OPENAT2;
}

static int class_of(long nr, unsigned long arg) {
    if (is_open(nr)) return OP_OPEN;
    if (nr == SYS_write || nr == SYS_pwrite64 || nr == SYS_writev || nr == SYS_pwritev ||
        nr == N_PWRITEV2) return OP_WRITE;
    if (nr == SYS_fsync || nr == SYS_fdatasync) return OP_FSYNC;
    if (nr == N_MKDIR || nr == SYS_mkdirat) return OP_MKDIR;
    if (nr == N_UNLINK || nr == SYS_unlinkat) return OP_UNLINK;
    if (nr == SYS_flock) return OP_FLOCK;
    if (nr == N_RENAME || nr == N_RENAMEAT || nr == SYS_renameat2) return OP_RENAME;
    /* F_SETLK and F_SETLKW. The record lock is the other half of the gate. */
    if (nr == SYS_fcntl && (arg == F_SETLK || arg == F_SETLKW)) return OP_FCNTL;
    return 0;
}

static void join_path(char *dst, int dirfd, const char *rel) {
    if (!rel[0] || rel[0] == '/') {
        snprintf(dst, 768, "%s", rel);
        return;
    }
    const char *base = dirfd == -100 ? NULL : path_of(dirfd);
    if (base && base[0]) snprintf(dst, 768, "%s/%s", base, rel);
    else if (root && root[0]) snprintf(dst, 768, "%s/%s", root, rel);
    else snprintf(dst, 768, "%s", rel);
}

static void path_arg(pid_t pid, long nr, const regs_t *regs, char *a, char *b) {
    a[0] = b[0] = 0;
    char raw1[768], raw2[768];
    raw1[0] = raw2[0] = 0;
    unsigned long p1 = 0, p2 = 0;
    int d1 = -100, d2 = -100;
    if (nr == N_OPEN || nr == N_MKDIR || nr == N_UNLINK) p1 = REG_A0(regs);
    else if (nr == SYS_openat || nr == N_OPENAT2 || nr == SYS_mkdirat || nr == SYS_unlinkat) {
        d1 = (int)REG_A0(regs);
        p1 = REG_A1(regs);
    } else if (nr == N_RENAME) {
        p1 = REG_A0(regs);
        p2 = REG_A1(regs);
    } else if (nr == N_RENAMEAT || nr == SYS_renameat2) {
        d1 = (int)REG_A0(regs);
        p1 = REG_A1(regs);
        d2 = (int)REG_A2(regs);
        p2 = REG_A3(regs);
    }
    if (p1) read_str(pid, p1, raw1, sizeof raw1);
    if (p2) read_str(pid, p2, raw2, sizeof raw2);
    if (raw1[0]) join_path(a, d1, raw1);
    if (raw2[0]) join_path(b, d2, raw2);
}

static const char *name_of(int c) {
    switch (c) {
    case OP_OPEN: return "open";
    case OP_WRITE: return "write";
    case OP_FSYNC: return "fsync";
    case OP_MKDIR: return "mkdir";
    case OP_UNLINK: return "unlink";
    case OP_FLOCK: return "flock";
    case OP_FCNTL: return "fcntl";
    case OP_RENAME: return "rename";
    default: return "?";
    }
}

static FILE *logf;

static int get_regs(pid_t pid, regs_t *regs) {
    struct iovec iov = {regs, sizeof *regs};
    return ptrace(PTRACE_GETREGSET, pid, (void *)NT_PRSTATUS, &iov);
}

static int set_regs(pid_t pid, regs_t *regs) {
    struct iovec iov = {regs, sizeof *regs};
    return ptrace(PTRACE_SETREGSET, pid, (void *)NT_PRSTATUS, &iov);
}

/* Replace the syscall at entry with getpid. */
static void poison(pid_t pid, regs_t *regs) {
#if defined(__x86_64__)
    regs->orig_rax = SYS_getpid;
    if (set_regs(pid, regs) < 0) _exit(1);
#else
    (void)regs;
    int nr = SYS_getpid;
    struct iovec iov = {&nr, sizeof nr};
    if (ptrace(PTRACE_SETREGSET, pid, (void *)NT_ARM_SYSTEM_CALL, &iov) < 0) _exit(1);
#endif
    poisoned = 1;
    fired = 1;
}

int main(int argc, char **argv) {
    int argi;
    if (argc < 4) return 2;
    if (!strcmp(argv[1], "log")) {
        op = OP_LOG;
        root = argv[2];
        if (strcmp(argv[3], "--") != 0) return 2;
        argi = 4;
    } else {
        fail_errno = atoi(argv[1]);
        op = !strcmp(argv[2], "open") ? OP_OPEN :
             !strcmp(argv[2], "write") ? OP_WRITE :
             !strcmp(argv[2], "fsync") ? OP_FSYNC :
             !strcmp(argv[2], "mkdir") ? OP_MKDIR :
             !strcmp(argv[2], "unlink") ? OP_UNLINK :
             !strcmp(argv[2], "flock") ? OP_FLOCK :
             !strcmp(argv[2], "fcntl") ? OP_FCNTL :
             !strcmp(argv[2], "rename") ? OP_RENAME : 0;
        nth = atoi(argv[3]);
        needle = argv[4];
        if (!op || nth < 1 || argc < 7 || strcmp(argv[5], "--") != 0) return 2;
        argi = 6;
    }

    const char *log_path = getenv("FS_SWEEP_LOG");
    pid_t pid = fork();
    if (pid < 0) return 1;
    if (pid == 0) {
        if (ptrace(PTRACE_TRACEME, 0, 0, 0) < 0) _exit(126);
        raise(SIGSTOP);
        execvp(argv[argi], argv + argi);
        _exit(127);
    }
    if (log_path && log_path[0]) {
        logf = fopen(log_path, "w");
        if (!logf) return 1;
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) return 1;
    if (ptrace(PTRACE_SETOPTIONS, pid, 0, PTRACE_O_TRACESYSGOOD | PTRACE_O_EXITKILL) < 0) return 1;
    if (ptrace(PTRACE_SYSCALL, pid, 0, 0) < 0) return 1;
    int entering = 1;
    regs_t entry;

    for (;;) {
        if (waitpid(pid, &status, 0) < 0) return errno == ECHILD ? 0 : 1;
        if (WIFEXITED(status)) return WEXITSTATUS(status);
        if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
        if (!WIFSTOPPED(status)) continue;
        int sig = WSTOPSIG(status);
        if (sig != (SIGTRAP | 0x80)) {
            if (ptrace(PTRACE_SYSCALL, pid, 0, sig == SIGTRAP ? 0 : sig) < 0) return 1;
            continue;
        }
        regs_t regs;
        if (get_regs(pid, &regs) < 0) return 1;
        if (entering) {
            poisoned = 0;
            entry = regs;
            long nr = REG_NR(&regs);
            int cls = class_of(nr, REG_A1(&regs));
            char a[768], b[768];
            path_arg(pid, nr, &regs, a, b);
            const char *via = NULL;
            if (cls == OP_WRITE || cls == OP_FSYNC || cls == OP_FLOCK || cls == OP_FCNTL)
                via = path_of((int)REG_A0(&regs));
            if (op == OP_LOG && cls) {
                const char *show = via ? via : a;
                if (show && show[0] && (!root || !root[0] || strstr(show, root) || (b[0] && strstr(b, root)))) {
                    FILE *out = logf ? logf : stderr;
                    if (cls == OP_RENAME) fprintf(out, "rename %s -> %s\n", a, b);
                    else fprintf(out, "%s %s\n", name_of(cls), show);
                    fflush(out);
                }
            } else if (!fired && cls == op) {
                int hit = 0;
                if (cls == OP_RENAME) hit = wanted(a) || wanted(b);
                else if (via) hit = wanted(via);
                else hit = wanted(a);
                if (hit && ++seen == nth) poison(pid, &regs);
            }
            if (nr == SYS_close) forget((int)REG_A0(&regs));
            entering = 0;
        } else {
            /* The return value replaces argument 0 on some architectures,
               so arguments come from the entry snapshot. */
            long nr = poisoned ? -1 : REG_NR(&entry);
            if (poisoned) REG_RET(&regs) = (unsigned long)(-fail_errno);
            if (is_open(nr) && (long)REG_RET(&regs) >= 0) {
                char a[768], b[768];
                path_arg(pid, nr, &entry, a, b);
                if (a[0]) remember((int)REG_RET(&regs), a);
            }
            if (nr == SYS_dup && (long)REG_RET(&regs) >= 0) {
                const char *p = path_of((int)REG_A0(&entry));
                if (p) remember((int)REG_RET(&regs), p);
            }
            if ((nr == N_DUP2 || nr == SYS_dup3) && (long)REG_RET(&regs) >= 0) {
                const char *p = path_of((int)REG_A0(&entry));
                if (p) remember((int)REG_A1(&entry), p);
            }
            if (poisoned || is_open(nr) || nr == SYS_dup || nr == N_DUP2 || nr == SYS_dup3)
                if (set_regs(pid, &regs) < 0) return 1;
            entering = 1;
        }
        if (ptrace(PTRACE_SYSCALL, pid, 0, 0) < 0) return 1;
    }
}
