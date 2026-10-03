// Minimal PID 1 for Afterglow under iSH.
//
// iSH cannot run systemd (no cgroups, no real mounts, no netlink), so the
// Afterglow rootfs boots straight into a login shell. This tiny init only
// has to do two things: reap adopted zombie children, and stay alive so the
// emulator keeps running until the host app kills it.
//
// Build: zig cc -target x86-linux-musl -mcpu=i486 -static -Os -s -o init init.c
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

static void reap(int sig) {
    (void) sig;
    while (waitpid(-1, NULL, WNOHANG) > 0);
}

int main(void) {
    struct sigaction sa = {0};
    sa.sa_handler = reap;
    sa.sa_flags = SA_RESTART | SA_NOCLDSTOP;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGCHLD, &sa, NULL);
    for (;;)
        pause();
}
