import os
import time
import socket
import fcntl
import threading
from dataclasses import dataclass
from typing import Optional

# ---------------------------
# Fixed / predetermined defaults
# ---------------------------

# Default host address for the library server
DEFAULT_HOST = "127.0.0.1"

# Default ports for user-related and book-related requests
DEFAULT_USER_PORT = 5000
DEFAULT_BOOK_PORT = 5001

# Shared log file path (used by all processes and threads)
DEFAULT_LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "library.log")


@dataclass(frozen=True)
class Config:
    """
    Holds runtime configuration for the system.

    This object is immutable (frozen=True) so configuration
    values cannot be modified accidentally at runtime.
    """
    host: str
    user_port: int
    book_port: int
    log_path: str


def load_config_from_env() -> Config:
    """
    Load configuration values from environment variables.

    If an environment variable is missing, the default value
    is used instead.
    """
    host = os.environ.get("LIB_HOST", DEFAULT_HOST)
    user_port = int(os.environ.get("LIB_USER_PORT", str(DEFAULT_USER_PORT)))
    book_port = int(os.environ.get("LIB_BOOK_PORT", str(DEFAULT_BOOK_PORT)))
    log_path = os.environ.get("LIB_LOG_PATH", DEFAULT_LOG_PATH)
    return Config(host=host, user_port=user_port, book_port=book_port, log_path=log_path)


def now_ts() -> str:
    """
    Return the current local time as a human-readable timestamp.
    Format: YYYY-MM-DD HH:MM:SS
    """
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def log_event(log_path: str, who: str, msg: str) -> None:
    """
    Append a single log entry to the shared log file.

    Properties:
    - Safe across multiple *processes* and *threads*
    - Uses OS-level file locking (fcntl.flock)
    - Each log line contains:
        timestamp | pid | thread id | component | message
    """
    # Ensure the log directory exists
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    # Construct the log line
    line = (
        f"[{now_ts()}] "
        f"pid={os.getpid()} "
        f"tid={threading.get_ident()} "
        f"{who}: {msg}\n"
    )

    # Open file in append mode and acquire exclusive lock
    with open(log_path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()              # Flush Python-level buffer
            os.fsync(f.fileno())   # Flush OS-level buffer (durability)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def make_server_socket(host: str, port: int, backlog: int = 128) -> socket.socket:
    """
    Create, bind, and listen on a TCP server socket.

    - SO_REUSEADDR allows quick restarts.
    - SO_REUSEPORT is attempted (if supported) to avoid bind conflicts.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except Exception:
        # Some systems do not support SO_REUSEPORT
        pass

    s.bind((host, port))
    s.listen(backlog)
    return s


def safe_close(obj) -> None:
    """
    Safely close an object (e.g., socket or file).

    Any exception during close is ignored.
    """
    try:
        if obj is not None:
            obj.close()
    except Exception:
        pass


def recv_one_line(conn: socket.socket, max_bytes: int = 4096) -> str:
    """
    Read a single newline-terminated line from a socket.

    Stops reading when:
    - a newline is encountered
    - max_bytes is reached
    - the peer closes the connection
    """
    buf = bytearray()
    while len(buf) < max_bytes:
        b = conn.recv(1)
        if not b:
            # Peer closed connection
            break
        if b == b"\n":
            # End of line
            break
        buf += b

    return buf.decode("utf-8", errors="replace").strip()


def send_line(
    host: str,
    port: int,
    line: str,
    timeout: float = 5.0,
    retries: int = 40,
    retry_delay: float = 0.05,
) -> str:
    """
    Client helper function.

    - Connects to (host, port)
    - Sends exactly one line (adds '\\n' if missing)
    - Reads the full response until the server closes the connection
    - Retries on connection errors (useful during server startup)

    Returns:
        The server response as a UTF-8 string (stripped).
    """
    data = (line.rstrip("\n") + "\n").encode("utf-8")
    last_err: Optional[Exception] = None

    for _ in range(retries):
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                sock.sendall(data)
                sock.shutdown(socket.SHUT_WR)  # Signal end of request

                # Read response until server closes the connection
                chunks = []
                while True:
                    part = sock.recv(4096)
                    if not part:
                        break
                    chunks.append(part)

            return b"".join(chunks).decode("utf-8", errors="replace").strip()

        except (ConnectionRefusedError, OSError) as e:
            # Server may not be ready yet
            last_err = e
            time.sleep(retry_delay)

    # All retries failed
    raise last_err if last_err is not None else RuntimeError("send_line failed unexpectedly")


# ---------------------------
# Readers / Writers Lock
# ---------------------------
class RWLock:
    """
    A classic Readers–Writers lock implementation.

    Properties:
    - Multiple readers can hold the lock concurrently.
    - Writers are exclusive (no readers or writers allowed).
    - Writers are given priority to reduce writer starvation.
    """

    def __init__(self) -> None:
        self._mtx = threading.Lock()
        self._cv = threading.Condition(self._mtx)

        self._readers = 0            # Number of active readers
        self._writer = False         # Whether a writer holds the lock
        self._writers_waiting = 0    # Number of waiting writers (for priority)

    def acquire_read(self) -> None:
        """
        Acquire the lock in read mode.

        A reader must wait if:
        - a writer is currently active, OR
        - there are writers waiting (writer priority).
        """
        with self._cv:
            while self._writer or self._writers_waiting > 0:
                self._cv.wait()
            self._readers += 1

    def release_read(self) -> None:
        """
        Release the read lock.

        If this was the last reader, wake up waiting threads.
        """
        with self._cv:
            self._readers -= 1
            if self._readers == 0:
                self._cv.notify_all()

    def acquire_write(self) -> None:
        """
        Acquire the lock in write mode.

        A writer waits until:
        - no other writer is active
        - no readers are active
        """
        with self._cv:
            self._writers_waiting += 1
            try:
                while self._writer or self._readers > 0:
                    self._cv.wait()
                self._writer = True
            finally:
                # Ensure waiting writers counter is decremented
                self._writers_waiting -= 1

    def release_write(self) -> None:
        """
        Release the write lock and wake all waiting readers and writers.
        """
        with self._cv:
            self._writer = False
            self._cv.notify_all()
