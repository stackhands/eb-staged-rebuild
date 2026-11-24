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
#include <sys/ptrace.h>
#include <sys/uio.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <unistd.h>

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

static int class_of(long nr, unsigned long arg) {
    if (nr == 2 || nr == 257 || nr == 437) return OP_OPEN;
    if (nr == 1 || nr == 18 || nr == 20 || nr == 296 || nr == 328) return OP_WRITE;
    if (nr == 74 || nr == 75) return OP_FSYNC;
    if (nr == 83 || nr == 258) return OP_MKDIR;
    if (nr == 87 || nr == 263) return OP_UNLINK;
    if (nr == 73) return OP_FLOCK;
    if (nr == 82 || nr == 264 || nr == 316) return OP_RENAME;
    /* F_SETLK and F_SETLKW. The record lock is the other half of the gate. */
    if (nr == 72 && (arg == 6 || arg == 7)) return OP_FCNTL;
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

static void path_arg(pid_t pid, long nr, struct user_regs_struct *regs, char *a, char *b) {
    a[0] = b[0] = 0;
    char raw1[768], raw2[768];
    raw1[0] = raw2[0] = 0;
    unsigned long p1 = 0, p2 = 0;
    int d1 = -100, d2 = -100;
    if (nr == 2 || nr == 83 || nr == 87) p1 = regs->rdi;
    else if (nr == 257 || nr == 437 || nr == 258 || nr == 263) {
        d1 = (int)regs->rdi;
        p1 = regs->rsi;
    } else if (nr == 82) {
        p1 = regs->rdi;
        p2 = regs->rsi;
    } else if (nr == 264 || nr == 316) {
        d1 = (int)regs->rdi;
        p1 = regs->rsi;
        d2 = (int)regs->rdx;
        p2 = regs->r10;
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

static void poison(pid_t pid, struct user_regs_struct *regs) {
    regs->orig_rax = 39;
    if (ptrace(PTRACE_SETREGS, pid, 0, regs) < 0) _exit(1);
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
        struct user_regs_struct regs;
        if (ptrace(PTRACE_GETREGS, pid, 0, &regs) < 0) return 1;
        if (entering) {
            poisoned = 0;
            long nr = (long)regs.orig_rax;
            int cls = class_of(nr, regs.rsi);
            char a[768], b[768];
            path_arg(pid, nr, &regs, a, b);
            const char *via = NULL;
            if (cls == OP_WRITE || cls == OP_FSYNC || cls == OP_FLOCK || cls == OP_FCNTL)
                via = path_of((int)regs.rdi);
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
            if (nr == 3) forget((int)regs.rdi);
            entering = 0;
        } else {
            long nr = (long)regs.orig_rax;
            if (poisoned) regs.rax = (unsigned long)(-fail_errno);
            if ((nr == 2 || nr == 257 || nr == 437) && (long)regs.rax >= 0) {
                char a[768], b[768];
                path_arg(pid, nr, &regs, a, b);
                if (a[0]) remember((int)regs.rax, a);
            }
            if (nr == 32 && (long)regs.rax >= 0) {
                const char *p = path_of((int)regs.rdi);
                if (p) remember((int)regs.rax, p);
            }
            if ((nr == 33 || nr == 292) && (long)regs.rax >= 0) {
                const char *p = path_of((int)regs.rdi);
                if (p) remember((int)regs.rsi, p);
            }
            if (poisoned || nr == 2 || nr == 257 || nr == 437 || nr == 32 || nr == 33 || nr == 292)
                if (ptrace(PTRACE_SETREGS, pid, 0, &regs) < 0) return 1;
            entering = 1;
        }
        if (ptrace(PTRACE_SYSCALL, pid, 0, 0) < 0) return 1;
    }
}
