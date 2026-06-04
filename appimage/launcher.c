#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>

int main(int argc, char *argv[]) {
    const char *appdir = getenv("APPDIR");
    if (!appdir) {
        fprintf(stderr, "[FluxCast] APPDIR not set\n");
        return 1;
    }

    char setup_script[4096];
    snprintf(setup_script, sizeof(setup_script),
             "%s/usr/bin/fluxcast-setup.sh", appdir);

    if (access(setup_script, F_OK) == 0) {
        pid_t pid = fork();
        if (pid == 0) {
            char *setup_argv[] = {"/bin/bash", setup_script, NULL};
            execv("/bin/bash", setup_argv);
            _exit(1);
        } else if (pid > 0) {
            int status;
            waitpid(pid, &status, 0);
        }
    }

    char python3[4096];
    snprintf(python3, sizeof(python3), "%s/usr/bin/python3", appdir);

    char mainpy[4096];
    snprintf(mainpy, sizeof(mainpy),
             "%s/usr/src/fluxcast/src/main.py", appdir);

    char **new_argv = (char **)malloc((argc + 2) * sizeof(char *));
    if (!new_argv) return 1;
    new_argv[0] = python3;
    new_argv[1] = mainpy;
    for (int i = 1; i < argc; i++) new_argv[i + 1] = argv[i];
    new_argv[argc + 1] = NULL;

    execv(python3, new_argv);
    perror("[FluxCast] Failed to launch Python");
    return 1;
}
