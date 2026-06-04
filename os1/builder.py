import sys
import os
import signal
import time

from common import SafeLogger

# ********************************************
def builder_main(command_files):
    """
    Builder (parent) process entry point.

    Responsibilities (per the project PDF):
    - Create the shared log file.
    - Provide the initial communication "bridges" (e.g., ports/host) to all processes.
    - Spawn the Library process.
    - Spawn one User process per command file.
    - Wait for users to finish, then terminate the library (for testing/cleanup).
    """

    # Network configuration:
    # - Users connect to the Library on (listen_host, listen_port)
    # - Library internally talks to two managers (users/books) on internal ports
    listen_host = "127.0.0.1"
    listen_port = 5001

    internal_host = "127.0.0.1"
    users_port = 6001
    books_port = 6002

    # Shared log path used by all processes
    log_path = "./logs/library.log"

# ********************************************
    # Initialize shared logger (safe across processes)
    logger = SafeLogger(log_path)
    logger.write("builder: starting builder (phase1)")
# ********************************************
    # Reap child processes automatically (avoid zombies).
    # Note: with SIGCHLD=SIG_IGN, waitpid() may raise ChildProcessError; we handle it later.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
# ********************************************
    # -------------------------
    # Spawn the Library process
    # -------------------------
    lib_pid = os.fork()
    if lib_pid == 0:
        # Child: configure the library via environment variables
        os.environ["LIB_LISTEN_HOST"] = listen_host
        os.environ["LIB_LISTEN_PORT"] = str(listen_port)

        os.environ["LIB_INTERNAL_HOST"] = internal_host
        os.environ["LIB_USERS_PORT"] = str(users_port)
        os.environ["LIB_BOOKS_PORT"] = str(books_port)

        os.environ["LIB_LOG_PATH"] = log_path

        # Replace this process image with library.py (no return on success)
        os.execvp(sys.executable, [sys.executable, "library.py"])
# ********************************************
    # Parent: log library spawn info
    logger.write(f"builder: spawned library pid={lib_pid}")
    logger.write(
        f"builder: ports user={listen_port} internal_users={users_port} internal_books={books_port}"
    )
# ********************************************
    # Small delay so the library has time to bind/listen before users connect
    time.sleep(0.3)
# ********************************************
    # -----------------------
    # Spawn User processes
    # -----------------------
    user_pids = []
    for i, cmd in enumerate(command_files, start=1):
        pid = os.fork()
        if pid == 0:
            # Child: configure user process (where to connect + logging + username)
            os.environ["LIB_LISTEN_HOST"] = listen_host
            os.environ["LIB_LISTEN_PORT"] = str(listen_port)
            os.environ["LIB_LOG_PATH"] = log_path
            os.environ["USER_NAME"] = f"user{i}"

            # Execute user.py with the command file path as argument
            os.execvp(sys.executable, [sys.executable, "user.py", cmd])

        # Parent: track and log user spawn
        user_pids.append(pid)
        logger.write(f"builder: spawned user{i} pid={pid} cmd_file={cmd}")
# ********************************************
    # ---------------------------------------
    # Wait until all user processes finish
    # ---------------------------------------
    for pid in user_pids:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            # If SIGCHLD is ignored, the OS may auto-reap children and waitpid can fail.
            pass

    logger.write("builder: all users finished. terminating library...")
# ********************************************
    # -----------------------
    # Terminate the Library
    # -----------------------
    # Not strictly required by the spec, but helpful for clean test shutdown.
    try:
        os.kill(lib_pid, signal.SIGTERM)
    except ProcessLookupError:
        # Library already exited
        pass

    logger.write("builder: done.")

# ********************************************
if __name__ == "__main__":
    # Command line usage:
    # python3 builder.py <cmd1> <cmd2> ...
    if len(sys.argv) < 2:
        print("Usage: python3 src/builder.py <cmd1> <cmd2> ...", file=sys.stderr)
        sys.exit(1)

    builder_main(sys.argv[1:])
