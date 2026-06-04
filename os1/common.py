import os
import time
import fcntl

# time
def now_ts() -> str:
    """Return current local timestamp as 'YYYY-MM-DD HH:MM:SS'."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
# ******************
class SafeLogger:
    """
    A shared logger used by all processes, protected by an OS-level file lock.

    Why this exists:
    - Multiple processes write to the same log file concurrently.
    - We use `fcntl.flock(..., LOCK_EX)` to prevent interleaved / corrupted log lines.
    """
    # create log folder and save it
    def __init__(self, log_path: str):
        self.log_path = log_path
        # Ensure the parent directory exists
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        # Ensure the log file exists
        open(self.log_path, "a", encoding="utf-8").close()

    def write(self, msg: str):
        """
        Append one log line with a timestamp and the current process PID.
        The write is protected by an exclusive file lock.
        """
        line = f"[{now_ts()}] pid={os.getpid()} {msg}\n"
        with open(self.log_path, "a", encoding="utf-8") as f:
            # Exclusive lock so only one process writes at a time
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
                # Force the OS to flush buffers to disk (more durable logs)
                os.fsync(f.fileno())
            finally:
                # Always release the lock
                fcntl.flock(f, fcntl.LOCK_UN)

def send_line(sock, text: str):
    """
    Send a single line over a socket (UTF-8), ensuring it ends with '\\n'.
    """
    data = (text.rstrip("\n") + "\n").encode("utf-8")
    sock.sendall(data)

def recv_line(sock) -> str:
    """
    Receive exactly one line from a socket until '\\n' (newline) is reached.
    If the peer closes the connection, returns whatever has been read so far.
    """
    buf = bytearray()
    while True:
        ch = sock.recv(1)
        if not ch:
            break
        buf += ch
        if ch == b"\n":
            break
    return buf.decode("utf-8", errors="replace").rstrip("\n")
