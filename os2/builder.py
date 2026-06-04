import os
import sys
import time
import signal
import socket

from common import (
    DEFAULT_HOST,
    DEFAULT_USER_PORT,
    DEFAULT_BOOK_PORT,
    DEFAULT_LOG_PATH,
    log_event,
)


def _env_int(name: str, default: int) -> int:
    """
    Read an environment variable and parse it as an integer.

    - If the variable is missing or empty -> return `default`
    - If the value is not a valid integer -> return `default`

    This prevents the program from crashing due to a bad environment value.
    """
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def spawn_exec(python_exe: str, script: str, args: list, env: dict, log_path: str, who: str) -> int:
    """
    Spawn a new process using fork + execve.

    Behavior:
    - Parent process:
        * returns child's PID immediately
    - Child process:
        * replaces itself with: `python_exe script args...`
        * uses the provided `env` dictionary as its environment
        * if exec fails, tries to log the error, then exits with code 1

    Why fork+exec?
    - It creates fully separate processes (like running two terminal commands),
      which is useful for a "builder" that orchestrates server/client.
    """
    pid = os.fork()
    if pid == 0:
        # Child process path
        try:
            # Replace the current child process image with a new Python process.
            # NOTE: execve never returns if successful.
            os.execve(python_exe, [python_exe, script] + args, env)
        except Exception as e:
            # If exec fails (e.g., file not found, permission issue),
            # try to log it (best effort), then exit.
            try:
                log_event(log_path, who, f"exec failed: {e}")
            except Exception:
                # If logging also fails, ignore to avoid crashing further
                pass
        os._exit(1)  # Ensure the child exits (do NOT continue running builder code)
    return pid  # Parent receives child's PID


def wait_for_port(host: str, port: int, timeout_sec: float = 10.0) -> None:
    """
    Wait until a TCP server is listening on (host, port).

    We repeatedly attempt to create a TCP connection until:
    - success -> the port is open and accepting connections -> return
    - timeout expires -> raise RuntimeError

    Purpose:
    - Guarantees the library server is ready before starting the client.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            # If this succeeds, a server is listening and accepting connections.
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            # Connection failed: server not ready yet (or temporary network issue)
            time.sleep(0.05)

    # If we reach here, the server did not open the port in time.
    raise RuntimeError(f"Library did not open port {host}:{port} within {timeout_sec}s")


def main() -> int:
    """
    Builder entry point.

    Usage:
      python3 builder.py <commands/user1.txt> [commands/user2.txt ...]

    Responsibilities (high-level):
    1) Read host/ports/log path from environment variables (or use defaults).
    2) Start the Library process (server).
    3) Wait until the Library opens both ports (user port + book port).
    4) Start the Client process, passing command files (one file => one user thread).
    5) Wait for the Client to finish.
    6) Terminate the Library process gracefully (SIGTERM).
    7) Log important events to a shared log file.
    """
    # Require at least one command file argument
    if len(sys.argv) < 2:
        print("Usage: python3 builder.py <commands/user1.txt> [commands/user2.txt ...]")
        return 2

    # Command files passed to the client (each becomes a user-thread in client_main.py)
    cmd_files = sys.argv[1:]

    # Absolute path to project directory (where this builder.py lives)
    base_dir = os.path.abspath(os.path.dirname(__file__))

    # Read host/ports/log_path from environment, fallback to defaults if missing
    host = os.environ.get("LIB_HOST", DEFAULT_HOST)
    user_port = _env_int("LIB_USER_PORT", DEFAULT_USER_PORT)
    book_port = _env_int("LIB_BOOK_PORT", DEFAULT_BOOK_PORT)
    log_path = os.environ.get("LIB_LOG_PATH", DEFAULT_LOG_PATH)

    # Prepare child environment:
    # - Start from the current environment (do NOT clear it)
    # - Force LIB_* values so both library and client use the same exact config
    env = dict(os.environ)
    env.update(
        {
            "LIB_HOST": host,
            "LIB_USER_PORT": str(user_port),
            "LIB_BOOK_PORT": str(book_port),
            "LIB_LOG_PATH": log_path,
        }
    )

    # Ensure the log directory exists so writing to log_path won't fail
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    # Log builder startup and the chosen port configuration
    log_event(log_path, "builder", "starting builder (phase2)")
    log_event(log_path, "builder", f"ports user={user_port} book={book_port}")

    # Determine which Python interpreter to use (same one running builder.py)
    python_exe = sys.executable

    # Resolve paths for the library server and client scripts
    library_script = os.path.join(base_dir, "library_main.py")
    client_script = os.path.join(base_dir, "client_main.py")

    # Start the library server as a separate process
    lib_pid = spawn_exec(python_exe, library_script, [], env, log_path, "builder")
    log_event(log_path, "builder", f"spawned library pid={lib_pid}")

    # Wait for the library server to open both ports (so client won't race/fail)
    wait_for_port(host, user_port, timeout_sec=10.0)
    wait_for_port(host, book_port, timeout_sec=10.0)
    log_event(log_path, "builder", "library ports are ready")

    # Start the client process, passing all command files as arguments
    client_pid = spawn_exec(python_exe, client_script, cmd_files, env, log_path, "builder")
    log_event(log_path, "builder", f"spawned client pid={client_pid}")

    # Wait until the client finishes its work
    os.waitpid(client_pid, 0)
    log_event(log_path, "builder", "client finished; terminating library")

    # Ask the library to shutdown gracefully (SIGTERM)
    try:
        os.kill(lib_pid, signal.SIGTERM)
    except Exception as e:
        # If SIGTERM fails (e.g., already exited), record it
        log_event(log_path, "builder", f"failed to SIGTERM library: {e}")

    # Reap the library process so it doesn't become a zombie
    try:
        os.waitpid(lib_pid, 0)
    except Exception:
        # Ignore: library may already be reaped or not exist
        pass

    # Final log and exit successfully
    log_event(log_path, "builder", "builder finished")
    return 0


if __name__ == "__main__":
    # Convert main() return code into actual process exit code
    raise SystemExit(main())
