/* Only disposable Mathlib build uppers are mounted through this library.
 * OverlayFS normally synchronizes the entire upper filesystem on unmount.
 * On a busy fleet XFS that can stall even a one-byte capability probe for minutes.
 * "volatile" omits that sync; source workspaces and durable published lowers are
 * outside the writable overlay. Bubblewrap removes this preload before exec.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <unistd.h>

int mount(const char *source, const char *target, const char *type,
          unsigned long flags, const void *data) {
    int (*real_mount)(const char *, const char *, const char *, unsigned long,
                      const void *) = dlsym(RTLD_NEXT, "mount");
    if (!real_mount) { errno = ENOSYS; return -1; }
    if (!type || strcmp(type, "overlay") || !data)
        return real_mount(source, target, type, flags, data);
    /* bwrap supplies stable O_PATH descriptors. Sequential cache/agent commands
     * reuse this round's upper. It is disposable, mounts are serialized by the
     * round owner, and the next round discards it, so its volatile marker is not
     * a durability promise to preserve across those sequential mounts. */
    const char *work = strstr((const char *)data, "workdir=/proc/self/fd/");
    int workfd;
    if (!work || sscanf(work, "workdir=/proc/self/fd/%d", &workfd) != 1)
        return real_mount(source, target, type, flags, data);
    {
        char marker[128];
        snprintf(marker, sizeof(marker), "/proc/self/fd/%d/work/incompat/volatile/dirty", workfd);
        if (unlink(marker) && errno != ENOENT) return -1;
        snprintf(marker, sizeof(marker), "/proc/self/fd/%d/work/incompat/volatile", workfd);
        if (rmdir(marker) && errno != ENOENT) return -1;
    }
    char *options;
    if (asprintf(&options, "%s,volatile", (const char *)data) < 0) {
        errno = ENOMEM;
        return -1;
    }
    int result = real_mount(source, target, type, flags, options);
    int saved_errno = errno;
    free(options);
    errno = saved_errno;
    return result;
}
