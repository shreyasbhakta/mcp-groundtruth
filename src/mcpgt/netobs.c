/*
 * mcpgt network observer: a library preloaded into the server under test
 * (DYLD_INSERT_LIBRARIES on macOS, LD_PRELOAD on Linux) that records network
 * operations as JSON lines in the file named by $MCPGT_NETOBS_LOG.
 *
 * It observes; it does not enforce. The sandbox is the boundary: it blocks
 * the network whatever this library sees. Recorded, per call:
 *
 *   hello    the library loaded into a process (proves observation is active)
 *   resolve  getaddrinfo(): the host name asked for
 *   connect  connect(): destination address, socket type, result and errno
 *   send     sendto()/sendmsg() with an explicit destination (UDP): address,
 *            length, and whether the bytes contain $MCPGT_CANARY
 *   exec     execve() and posix_spawn() (on Linux also execv, execvp, execvpe
 *            and execl*): the program, and the pid that will run it, so a
 *            child that never says hello is a coverage gap
 *
 * No payload bytes are recorded, only lengths and the canary flag.
 *
 * Not seen: raw system calls that bypass libc, statically linked programs,
 * children started with this library removed from their environment (they
 * appear as exec events without a matching hello), and on macOS any program
 * the system refuses to inject into (system binaries such as /bin/sh).
 */
/* No _GNU_SOURCE: with it, glibc declares connect() and friends with a
 * transparent-union address type that the definitions below would not match. */
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define LINE 1024

static long long now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static void emit(const char *line) {
    const char *path = getenv("MCPGT_NETOBS_LOG");
    if (path == NULL || *path == '\0') return;
    int saved = errno;
    int fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0600);
    if (fd >= 0) {
        ssize_t ignored = write(fd, line, strlen(line));  /* one write: O_APPEND keeps lines whole */
        (void)ignored;
        close(fd);
    }
    errno = saved;
}

/* A JSON string body: quotes, backslashes and control bytes escaped, truncated. */
static void json_str(char *out, size_t n, const char *s) {
    size_t j = 0;
    for (size_t i = 0; s != NULL && s[i] != '\0' && j + 7 < n && i < 300; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"' || c == '\\') {
            out[j++] = '\\';
            out[j++] = (char)c;
        } else if (c < 0x20 || c == 0x7f) {
            j += (size_t)snprintf(out + j, n - j, "\\u%04x", c);
        } else {
            out[j++] = (char)c;
        }
    }
    out[j] = '\0';
}

static void addr_fields(const struct sockaddr *sa, socklen_t len, char *out, size_t n) {
    char ip[INET6_ADDRSTRLEN] = "";
    if (sa == NULL) {
        snprintf(out, n, "\"family\":\"none\"");
    } else if (sa->sa_family == AF_INET && len >= (socklen_t)sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *s = (const struct sockaddr_in *)(const void *)sa;
        inet_ntop(AF_INET, &s->sin_addr, ip, sizeof ip);
        snprintf(out, n, "\"family\":\"inet\",\"ip\":\"%s\",\"port\":%d", ip, ntohs(s->sin_port));
    } else if (sa->sa_family == AF_INET6 && len >= (socklen_t)sizeof(struct sockaddr_in6)) {
        const struct sockaddr_in6 *s = (const struct sockaddr_in6 *)(const void *)sa;
        inet_ntop(AF_INET6, &s->sin6_addr, ip, sizeof ip);
        snprintf(out, n, "\"family\":\"inet6\",\"ip\":\"%s\",\"port\":%d", ip, ntohs(s->sin6_port));
    } else if (sa->sa_family == AF_UNIX) {
        char path[400];
        const struct sockaddr_un *s = (const struct sockaddr_un *)(const void *)sa;
        json_str(path, sizeof path, s->sun_path);
        snprintf(out, n, "\"family\":\"unix\",\"path\":\"%s\"", path);
    } else {
        snprintf(out, n, "\"family\":\"other-%d\"", (int)sa->sa_family);
    }
}

static const char *sock_kind(int fd) {
    int type = 0;
    socklen_t len = sizeof type;
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &len) != 0) return "unknown";
    return type == SOCK_STREAM ? "tcp" : type == SOCK_DGRAM ? "udp" : "other";
}

static int has_canary(const void *buf, size_t len) {
    const char *canary = getenv("MCPGT_CANARY");
    if (canary == NULL || *canary == '\0' || buf == NULL) return 0;
    size_t clen = strlen(canary);
    const char *p = buf;
    for (size_t i = 0; i + clen <= len; i++)
        if (memcmp(p + i, canary, clen) == 0) return 1;
    return 0;
}

static void log_connect(int fd, const struct sockaddr *sa, socklen_t len, int rc, int err) {
    char addr[512], line[LINE];
    addr_fields(sa, len, addr, sizeof addr);
    snprintf(line, sizeof line,
             "{\"op\":\"connect\",\"pid\":%d,\"ts\":%lld,\"socket\":\"%s\",%s,\"rc\":%d,\"errno\":%d}\n",
             (int)getpid(), now_ns(), sock_kind(fd), addr, rc, rc == 0 ? 0 : err);
    emit(line);
}

static void log_send(int fd, const struct sockaddr *sa, socklen_t len, size_t bytes, int canary, ssize_t rc,
                     int err) {
    char addr[512], line[LINE];
    addr_fields(sa, len, addr, sizeof addr);
    snprintf(line, sizeof line,
             "{\"op\":\"send\",\"pid\":%d,\"ts\":%lld,\"socket\":\"%s\",%s,\"bytes\":%zu,\"canary\":%s,"
             "\"rc\":%zd,\"errno\":%d}\n",
             (int)getpid(), now_ns(), sock_kind(fd), addr, bytes, canary ? "true" : "false", rc,
             rc < 0 ? err : 0);
    emit(line);
}

static void log_resolve(const char *node, int rc) {
    char host[700], line[LINE];
    json_str(host, sizeof host, node);
    snprintf(line, sizeof line,
             "{\"op\":\"resolve\",\"pid\":%d,\"ts\":%lld,\"host\":\"%s\",\"canary\":%s,\"rc\":%d}\n",
             (int)getpid(), now_ns(), host, node != NULL && has_canary(node, strlen(node)) ? "true" : "false",
             rc);
    emit(line);
}

static void log_exec(const char *how, const char *path, int child, int rc) {
    char prog[700], line[LINE];
    json_str(prog, sizeof prog, path);
    snprintf(line, sizeof line,
             "{\"op\":\"exec\",\"via\":\"%s\",\"pid\":%d,\"ts\":%lld,\"path\":\"%s\",\"child\":%d,\"rc\":%d}\n", how,
             (int)getpid(), now_ns(), prog, child, rc);
    emit(line);
}

static size_t msg_len(const struct msghdr *msg) {
    size_t total = 0;
    for (size_t i = 0; msg != NULL && i < (size_t)msg->msg_iovlen; i++) total += msg->msg_iov[i].iov_len;
    return total;
}

static int msg_canary(const struct msghdr *msg) {
    for (size_t i = 0; msg != NULL && i < (size_t)msg->msg_iovlen; i++)
        if (has_canary(msg->msg_iov[i].iov_base, msg->msg_iov[i].iov_len)) return 1;
    return 0;
}

__attribute__((constructor)) static void mcpgt_hello(void) {
    char line[LINE];
    snprintf(line, sizeof line, "{\"op\":\"hello\",\"pid\":%d,\"ppid\":%d,\"ts\":%lld}\n", (int)getpid(),
             (int)getppid(), now_ns());
    emit(line);
}

#if defined(__APPLE__) && !defined(MCPGT_PRELOAD_STYLE)
/* dyld interposing: calls from every other image are redirected here, and a
 * call to the original name inside this file reaches the real function. */

static int mcpgt_connect(int fd, const struct sockaddr *sa, socklen_t len) {
    int rc = connect(fd, sa, len), err = errno;
    log_connect(fd, sa, len, rc, err);
    errno = err;
    return rc;
}

static ssize_t mcpgt_sendto(int fd, const void *buf, size_t n, int flags, const struct sockaddr *sa, socklen_t len) {
    ssize_t rc = sendto(fd, buf, n, flags, sa, len);
    int err = errno;
    if (sa != NULL) log_send(fd, sa, len, n, has_canary(buf, n), rc, err);
    errno = err;
    return rc;
}

static ssize_t mcpgt_sendmsg(int fd, const struct msghdr *msg, int flags) {
    ssize_t rc = sendmsg(fd, msg, flags);
    int err = errno;
    if (msg != NULL && msg->msg_name != NULL)
        log_send(fd, msg->msg_name, msg->msg_namelen, msg_len(msg), msg_canary(msg), rc, err);
    errno = err;
    return rc;
}

static int mcpgt_getaddrinfo(const char *node, const char *service, const struct addrinfo *hints,
                             struct addrinfo **res) {
    int rc = getaddrinfo(node, service, hints, res), err = errno;
    log_resolve(node, rc);
    errno = err;
    return rc;
}

static int mcpgt_execve(const char *path, char *const argv[], char *const envp[]) {
    log_exec("execve", path, (int)getpid(), 0);
    return execve(path, argv, envp);
}

static int mcpgt_posix_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *fa,
                             const posix_spawnattr_t *attr, char *const argv[], char *const envp[]) {
    pid_t child = 0;
    int rc = posix_spawn(&child, path, fa, attr, argv, envp);
    if (pid != NULL) *pid = child;
    log_exec("posix_spawn", path, rc == 0 ? (int)child : 0, rc);
    return rc;
}

static int mcpgt_posix_spawnp(pid_t *pid, const char *file, const posix_spawn_file_actions_t *fa,
                              const posix_spawnattr_t *attr, char *const argv[], char *const envp[]) {
    pid_t child = 0;
    int rc = posix_spawnp(&child, file, fa, attr, argv, envp);
    if (pid != NULL) *pid = child;
    log_exec("posix_spawnp", file, rc == 0 ? (int)child : 0, rc);
    return rc;
}

__attribute__((used)) static const struct {
    const void *replacement;
    const void *replacee;
} mcpgt_interposers[] __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)mcpgt_connect, (const void *)connect},
    {(const void *)mcpgt_sendto, (const void *)sendto},
    {(const void *)mcpgt_sendmsg, (const void *)sendmsg},
    {(const void *)mcpgt_getaddrinfo, (const void *)getaddrinfo},
    {(const void *)mcpgt_execve, (const void *)execve},
    {(const void *)mcpgt_posix_spawn, (const void *)posix_spawn},
    {(const void *)mcpgt_posix_spawnp, (const void *)posix_spawnp},
};

#else
/* LD_PRELOAD: these definitions shadow libc's; the real ones come from RTLD_NEXT.
 * (MCPGT_PRELOAD_STYLE compiles this branch on macOS too, to check its syntax.) */

#ifndef RTLD_NEXT
#define RTLD_NEXT ((void *)-1L) /* glibc and musl; dlfcn.h only defines it under _GNU_SOURCE */
#endif

#define REAL(name)                                                                  \
    static __typeof__(name) *real_##name = NULL;                                    \
    if (real_##name == NULL) *(void **)(&real_##name) = dlsym(RTLD_NEXT, #name)

int connect(int fd, const struct sockaddr *sa, socklen_t len) {
    REAL(connect);
    int rc = real_connect(fd, sa, len), err = errno;
    log_connect(fd, sa, len, rc, err);
    errno = err;
    return rc;
}

ssize_t sendto(int fd, const void *buf, size_t n, int flags, const struct sockaddr *sa, socklen_t len) {
    REAL(sendto);
    ssize_t rc = real_sendto(fd, buf, n, flags, sa, len);
    int err = errno;
    if (sa != NULL) log_send(fd, sa, len, n, has_canary(buf, n), rc, err);
    errno = err;
    return rc;
}

ssize_t sendmsg(int fd, const struct msghdr *msg, int flags) {
    REAL(sendmsg);
    ssize_t rc = real_sendmsg(fd, msg, flags);
    int err = errno;
    if (msg != NULL && msg->msg_name != NULL)
        log_send(fd, msg->msg_name, msg->msg_namelen, msg_len(msg), msg_canary(msg), rc, err);
    errno = err;
    return rc;
}

int getaddrinfo(const char *node, const char *service, const struct addrinfo *hints, struct addrinfo **res) {
    REAL(getaddrinfo);
    int rc = real_getaddrinfo(node, service, hints, res), err = errno;
    log_resolve(node, rc);
    errno = err;
    return rc;
}

int execve(const char *path, char *const argv[], char *const envp[]) {
    REAL(execve);
    log_exec("execve", path, (int)getpid(), 0);
    return real_execve(path, argv, envp);
}

/* glibc's execv, execvp, execvpe and execl* call the real execve internally,
 * past this library, so each is wrapped too (CPython uses execv for a child
 * that inherits its environment). The execl* forms collect their variadic
 * arguments and go through the execv* wrappers. */

typedef int (*execv_fn)(const char *, char *const[]);
typedef int (*execvpe_fn)(const char *, char *const[], char *const[]);

int execv(const char *path, char *const argv[]) {
    static execv_fn real = NULL;
    if (real == NULL) *(void **)(&real) = dlsym(RTLD_NEXT, "execv");
    log_exec("execv", path, (int)getpid(), 0);
    return real(path, argv);
}

int execvp(const char *file, char *const argv[]) {
    static execv_fn real = NULL;
    if (real == NULL) *(void **)(&real) = dlsym(RTLD_NEXT, "execvp");
    log_exec("execvp", file, (int)getpid(), 0);
    return real(file, argv);
}

int execvpe(const char *file, char *const argv[], char *const envp[]) {
    static execvpe_fn real = NULL;
    if (real == NULL) *(void **)(&real) = dlsym(RTLD_NEXT, "execvpe");
    log_exec("execvpe", file, (int)getpid(), 0);
    return real(file, argv, envp);
}

#define MAX_EXECL_ARGS 256

static int collect(const char *first, va_list ap, char **argv, char ***envp) {
    int n = 0;
    argv[n++] = (char *)first;
    while (n < MAX_EXECL_ARGS && argv[n - 1] != NULL) argv[n++] = va_arg(ap, char *);
    if (argv[n - 1] != NULL) {
        errno = E2BIG;
        return -1;
    }
    if (envp != NULL) *envp = va_arg(ap, char **);
    return 0;
}

int execl(const char *path, const char *arg, ...) {
    char *argv[MAX_EXECL_ARGS];
    va_list ap;
    va_start(ap, arg);
    int rc = collect(arg, ap, argv, NULL);
    va_end(ap);
    return rc < 0 ? -1 : execv(path, argv);
}

int execlp(const char *file, const char *arg, ...) {
    char *argv[MAX_EXECL_ARGS];
    va_list ap;
    va_start(ap, arg);
    int rc = collect(arg, ap, argv, NULL);
    va_end(ap);
    return rc < 0 ? -1 : execvp(file, argv);
}

int execle(const char *path, const char *arg, ...) {
    char *argv[MAX_EXECL_ARGS];
    char **envp = NULL;
    va_list ap;
    va_start(ap, arg);
    int rc = collect(arg, ap, argv, &envp);
    va_end(ap);
    return rc < 0 ? -1 : execve(path, argv, envp);
}

int posix_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *fa, const posix_spawnattr_t *attr,
                char *const argv[], char *const envp[]) {
    REAL(posix_spawn);
    pid_t child = 0;
    int rc = real_posix_spawn(&child, path, fa, attr, argv, envp);
    if (pid != NULL) *pid = child;
    log_exec("posix_spawn", path, rc == 0 ? (int)child : 0, rc);
    return rc;
}

int posix_spawnp(pid_t *pid, const char *file, const posix_spawn_file_actions_t *fa, const posix_spawnattr_t *attr,
                 char *const argv[], char *const envp[]) {
    REAL(posix_spawnp);
    pid_t child = 0;
    int rc = real_posix_spawnp(&child, file, fa, attr, argv, envp);
    if (pid != NULL) *pid = child;
    log_exec("posix_spawnp", file, rc == 0 ? (int)child : 0, rc);
    return rc;
}
#endif
