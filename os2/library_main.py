import signal
import threading
from typing import Dict, Optional, Set, Tuple

from common import (
    load_config_from_env,
    make_server_socket,
    recv_one_line,
    safe_close,
    log_event,
    RWLock,
)

# --------------------------------------------
# Phase 2: ONE Library process with shared tables
# --------------------------------------------
# users_by_name: maps user_name -> user_id
# user_borrows:  maps user_id -> set of borrowed book titles
# books:         maps book_title -> user_id (if lent) or None (if available)


class LibraryState:
    """
    Holds all in-memory shared state for the library server.

    This object is shared by all handler threads and therefore
    must be protected using locks.
    """

    def __init__(self, log_path: str):
        self.log_path = log_path

        # -----------------
        # In-memory tables
        # -----------------

        # Map user name to numeric user id
        self.users_by_name: Dict[str, int] = {}

        # Map user id to the set of books currently borrowed
        self.user_borrows: Dict[int, Set[str]] = {}

        # Map book title to the user id it is lent to (None if available)
        self.books: Dict[str, Optional[int]] = {}

        # -----------------
        # User-id allocator
        # -----------------

        # Protects allocation of new user ids
        self._id_lock = threading.Lock()
        self._next_user_id = 1000

        # -----------------
        # Per-table Readers/Writers locks
        # -----------------

        # users_lock protects:
        #   - users_by_name
        #   - user_borrows
        self.users_lock = RWLock()

        # books_lock protects:
        #   - books
        self.books_lock = RWLock()

    def _alloc_user_id(self) -> int:
        """
        Allocate a unique user id in a thread-safe manner.
        """
        with self._id_lock:
            uid = self._next_user_id
            self._next_user_id += 1
            return uid


def parse_request(line: str) -> Tuple[str, str]:
    """
    Parse a raw request line into (command, rest).

    Uses partition(" ") so arguments may contain spaces.
    Example:
        "register_book War and Peace"
        -> ("register_book", "War and Peace")
    """
    cmd, _, rest = line.strip().partition(" ")
    return cmd.strip(), rest.strip()


def handle_register_user(st: LibraryState, rest: str) -> str:
    """
    Handle command:
        register_user <name>

    Behavior:
    - If the user already exists -> failure
    - Otherwise:
        * allocate a new user id
        * initialize an empty borrow set
        * return success <user_id>
    """
    if not rest:
        return "failure"

    name = rest

    st.users_lock.acquire_write()
    try:
        if name in st.users_by_name:
            log_event(st.log_path, "library", f"register_user {name} -> failure (exists)")
            return "failure"

        uid = st._alloc_user_id()
        st.users_by_name[name] = uid
        st.user_borrows[uid] = set()

        log_event(st.log_path, "library", f"register_user {name} -> success {uid}")
        return f"success {uid}"
    finally:
        st.users_lock.release_write()


def handle_register_book(st: LibraryState, rest: str) -> str:
    """
    Handle command:
        register_book <title>

    Behavior:
    - If the book already exists -> failure
    - Otherwise:
        * add the book as available (lent_to = None)
        * return success
    """
    if not rest:
        return "failure"

    title = rest

    st.books_lock.acquire_write()
    try:
        if title in st.books:
            log_event(st.log_path, "library", f"register_book {title} -> failure (exists)")
            return "failure"

        st.books[title] = None
        log_event(st.log_path, "library", f"register_book {title} -> success")
        return "success"
    finally:
        st.books_lock.release_write()


def handle_lend_book(st: LibraryState, rest: str) -> str:
    """
    Handle command:
        lend_book <user_id> <book_title>

    Validation:
    - user exists
    - book exists
    - book is not already lent

    On success:
    - books[title] = user_id
    - user_borrows[user_id].add(title)
    """
    uid_s, _, title = rest.partition(" ")
    uid_s = uid_s.strip()
    title = title.strip()

    if not uid_s or not title:
        return "failure"

    try:
        uid = int(uid_s)
    except ValueError:
        return "failure"

    # Deadlock prevention:
    # ALWAYS acquire locks in the same order: users_lock -> books_lock
    st.users_lock.acquire_write()
    st.books_lock.acquire_write()
    try:
        if uid not in st.user_borrows:
            log_event(st.log_path, "library", f"lend_book {uid} {title} -> failure (no user)")
            return "failure"

        if title not in st.books:
            log_event(st.log_path, "library", f"lend_book {uid} {title} -> failure (no book)")
            return "failure"

        if st.books[title] is not None:
            log_event(st.log_path, "library", f"lend_book {uid} {title} -> failure (already lent)")
            return "failure"

        st.books[title] = uid
        st.user_borrows[uid].add(title)

        log_event(st.log_path, "library", f"lend_book {uid} {title} -> success")
        return "success"
    finally:
        st.books_lock.release_write()
        st.users_lock.release_write()


def handle_return_book(st: LibraryState, rest: str) -> str:
    """
    Handle command:
        return_book <user_id> <book_title>

    Validation:
    - user exists
    - book exists
    - book is currently lent
    - book is lent to this user

    On success:
    - books[title] = None
    - user_borrows[user_id].remove(title)
    """
    uid_s, _, title = rest.partition(" ")
    uid_s = uid_s.strip()
    title = title.strip()

    if not uid_s or not title:
        return "failure"

    try:
        uid = int(uid_s)
    except ValueError:
        return "failure"

    # Deadlock prevention:
    # ALWAYS acquire locks in the same order: users_lock -> books_lock
    st.users_lock.acquire_write()
    st.books_lock.acquire_write()
    try:
        if uid not in st.user_borrows:
            log_event(st.log_path, "library", f"return_book {uid} {title} -> failure (no user)")
            return "failure"

        if title not in st.books:
            log_event(st.log_path, "library", f"return_book {uid} {title} -> failure (no book)")
            return "failure"

        if st.books[title] is None:
            log_event(st.log_path, "library", f"return_book {uid} {title} -> failure (not lent)")
            return "failure"

        if st.books[title] != uid:
            log_event(st.log_path, "library", f"return_book {uid} {title} -> failure (wrong user)")
            return "failure"

        st.books[title] = None
        st.user_borrows[uid].discard(title)

        log_event(st.log_path, "library", f"return_book {uid} {title} -> success")
        return "success"
    finally:
        st.books_lock.release_write()
        st.users_lock.release_write()


def dispatch(st: LibraryState, line: str) -> str:
    """
    Dispatch a single request line to the appropriate handler.
    """
    cmd, rest = parse_request(line)

    if cmd == "register_user":
        return handle_register_user(st, rest)
    if cmd == "register_book":
        return handle_register_book(st, rest)
    if cmd == "lend_book":
        return handle_lend_book(st, rest)
    if cmd == "return_book":
        return handle_return_book(st, rest)

    # Unknown command
    return "failure"


def accept_loop(st: LibraryState, server_sock, who: str, stop_event: threading.Event):
    """
    Accept-loop for one listening socket (USER_PORT or BOOK_PORT).

    For each accepted connection:
    - spawn a daemon worker thread
    - read exactly one request
    - dispatch it
    - send back the response
    - log request and response
    """
    log_event(st.log_path, "library", f"{who} accept loop started")

    while not stop_event.is_set():
        try:
            conn, addr = server_sock.accept()
        except OSError:
            # Socket was closed during shutdown
            break

        def worker(c=conn, a=addr):
            try:
                line = recv_one_line(c)
                if not line:
                    return

                resp = dispatch(st, line)
                c.sendall((resp + "\n").encode("utf-8"))

                log_event(st.log_path, "handler", f"{who} from={a} req='{line}' resp='{resp}'")
            except Exception as e:
                log_event(st.log_path, "handler", f"{who} error: {e}")
            finally:
                safe_close(c)
# ********************************
        # One thread per connection (simple concurrency model)
        t = threading.Thread(target=worker, daemon=True)
        t.start()

    log_event(st.log_path, "library", f"{who} accept loop stopped")


def main():
    """
    Library server main entry point.

    Steps:
    1) Load configuration (host, ports, log path)
    2) Create shared library state
    3) Create two server sockets (user port and book port)
    4) Start accept-loops (one per port)
    5) Install SIGTERM / SIGINT handlers for graceful shutdown
    6) Block main thread until termination signal arrives
    """
    cfg = load_config_from_env()
    st = LibraryState(cfg.log_path)

    stop_event = threading.Event()

    # Create listening sockets
    user_sock = make_server_socket(cfg.host, cfg.user_port)
    book_sock = make_server_socket(cfg.host, cfg.book_port)

    log_event(
        cfg.log_path,
        "library",
        f"started. host={cfg.host} user_port={cfg.user_port} book_port={cfg.book_port}",
    )

    def on_term(signum, frame):
        """
        Signal handler:
        - mark stop_event
        - close sockets to unblock accept()
        """
        log_event(cfg.log_path, "library", f"signal {signum} received -> shutting down")
        stop_event.set()
        safe_close(user_sock)
        safe_close(book_sock)

    # Graceful shutdown on SIGTERM (builder) or SIGINT (Ctrl+C)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    # Start accept loops (daemon threads)
    t_user = threading.Thread(
        target=accept_loop,
        args=(st, user_sock, "USER_PORT", stop_event),
        daemon=True,
    )
    t_book = threading.Thread(
        target=accept_loop,
        args=(st, book_sock, "BOOK_PORT", stop_event),
        daemon=True,
    )

    t_user.start()
    t_book.start()

    # Main thread waits until a termination signal is received
    while not stop_event.is_set():
        signal.pause()


if __name__ == "__main__":
    main()
