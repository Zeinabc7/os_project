import os
import socket
import signal
import time  # <-- ADDED (for demo)
from multiprocessing import Semaphore
from typing import Dict, Optional

from common import SafeLogger, send_line, recv_line

# ---- Internal manager protocols ----
# users_manager:
#   register_user <name>  -> success <id> | failure
#   check_user <id>       -> success | failure
#
# books_manager:
#   register_book <title>               -> success | failure
#   lend_book <user_id> <title>         -> success | failure
#   return_book <user_id> <title>       -> success | failure
#   status_book <title>                 -> missing | available | lent <user_id>
#
# NOTE:
# - status_book is INTERNAL ONLY (library <-> books_manager), used to explain failures more clearly.


def users_manager_main(host: str, port: int, log_path: str):
    """
    Internal USERS manager process.

    Responsibilities:
    - Maintain an in-memory users table: user_id -> user_name
    - Serve internal socket requests from the Library process:
        * register_user <name>  -> success <id>
        * check_user <id>       -> success | failure
    """
    logger = SafeLogger(log_path)
    logger.write(f"users_manager started on {host}:{port}")

    users: Dict[str, str] = {}  # user_id -> name
    next_id = 1

    # Create internal TCP server for the users manager
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(50)

    while True:
        conn, addr = srv.accept()
        with conn:
            # Read exactly one command line
            cmd = recv_line(conn).strip()
            if not cmd:
                continue

            parts = cmd.split()
            op = parts[0]

            if op == "register_user" and len(parts) >= 2:
                # Allocate a new user_id and store the user's name
                user_id = str(next_id)
                next_id += 1
                name = " ".join(parts[1:])
                users[user_id] = name

                logger.write(f"users_manager: registered user '{name}' as id={user_id}")
                send_line(conn, f"success {user_id}")

            elif op == "check_user" and len(parts) == 2:
                # Check if the given user_id exists
                user_id = parts[1]
                if user_id in users:
                    send_line(conn, "success")
                else:
                    send_line(conn, "failure")

            else:
                # Unknown/invalid command
                send_line(conn, "failure")


def books_manager_main(host: str, port: int, log_path: str):
    """
    Internal BOOKS manager process.

    Responsibilities:
    - Maintain an in-memory books table: title -> holder_user_id
        * holder_user_id is None if the book is available
    - Serve internal socket requests from the Library process:
        * register_book <title>       -> success | failure
        * lend_book <user_id> <title> -> success | failure
        * return_book <user_id> <title> -> success | failure
        * status_book <title>         -> missing | available | lent <user_id>
    """
    logger = SafeLogger(log_path)
    logger.write(f"books_manager started on {host}:{port}")

    # title -> holder_user_id (None if available)
    books: Dict[str, Optional[str]] = {}

    # Create internal TCP server for the books manager
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(50)

    while True:
        conn, addr = srv.accept()
        with conn:
            # Read exactly one command line
            cmd = recv_line(conn).strip()
            if not cmd:
                continue

            parts = cmd.split()
            op = parts[0]

            if op == "register_book" and len(parts) >= 2:
                # Add a new book (available by default)
                title = " ".join(parts[1:])
                if title in books:
                    send_line(conn, "failure")
                else:
                    books[title] = None
                    logger.write(f"books_manager: registered book '{title}'")
                    send_line(conn, "success")

            elif op == "lend_book" and len(parts) >= 3:
                # Lend a book to a user if it exists and is currently available
                user_id = parts[1]
                title = " ".join(parts[2:])

                if title not in books:
                    send_line(conn, "failure")
                elif books[title] is not None:
                    send_line(conn, "failure")
                else:
                    books[title] = user_id
                    logger.write(f"books_manager: lent '{title}' to user_id={user_id}")
                    send_line(conn, "success")

            elif op == "return_book" and len(parts) >= 3:
                # Return a book only if it exists and is currently held by this user_id
                user_id = parts[1]
                title = " ".join(parts[2:])

                if title not in books:
                    send_line(conn, "failure")
                elif books[title] != user_id:
                    send_line(conn, "failure")
                else:
                    books[title] = None
                    logger.write(f"books_manager: returned '{title}' from user_id={user_id}")
                    send_line(conn, "success")

            elif op == "status_book" and len(parts) >= 2:
                # INTERNAL ONLY: report current status of a book
                title = " ".join(parts[1:])
                if title not in books:
                    send_line(conn, "missing")
                else:
                    holder = books[title]
                    if holder is None:
                        send_line(conn, "available")
                    else:
                        send_line(conn, f"lent {holder}")

            else:
                # Unknown/invalid command
                send_line(conn, "failure")


def _ask_internal(host: str, port: int, line: str) -> str:
    """
    Helper used by the Library/handlers to talk to internal manager processes.
    It opens a TCP connection, sends one command line, reads one response line, then closes.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    with s:
        send_line(s, line)
        return recv_line(s).strip()


def handle_client(
    conn_fd: int,
    client_addr: str,
    log_path: str,
    sem: Semaphore,
    internal_host: str,
    users_port: int,
    books_port: int,
):
    """
    Per-client request handler (runs in a forked child process).

    Project requirements:
    - Each client request must be served by a NEW process.
    - The library must handle up to 3 concurrent requests (enforced by `sem`).

    Notes:
    - This handler reads ONE request line, processes it, replies, then exits.
    - `sem.release()` is guaranteed via `finally`.
    """
    logger = SafeLogger(log_path)
    try:
        # Rebuild a socket object from the inherited file descriptor
        conn = socket.socket(fileno=conn_fd)
        with conn:
            req = recv_line(conn).strip()
            logger.write(f"handler: received from {client_addr}: {req}")

            # ---- ADDED (demo only): keep handler busy so semaphore waiting becomes visible ----
            time.sleep(1.5)
            # --------------------------------------------------------------------------------

            parts = req.split()
            if not parts:
                send_line(conn, "failure")
                logger.write(f"handler: replied to {client_addr}: failure (empty request)")
                return

            cmd = parts[0]

            # --------------------
            # register_user
            # --------------------
            if cmd == "register_user" and len(parts) >= 2:
                name = " ".join(parts[1:])
                resp = _ask_internal(internal_host, users_port, f"register_user {name}")
                send_line(conn, resp)
                logger.write(f"handler: replied to {client_addr}: {resp}")

            # --------------------
            # register_book
            # --------------------
            elif cmd == "register_book" and len(parts) >= 2:
                title = " ".join(parts[1:])
                resp = _ask_internal(internal_host, books_port, f"register_book {title}")
                send_line(conn, resp)
                logger.write(f"handler: replied to {client_addr}: {resp}")

            # --------------------
            # lend_book
            # --------------------
            elif cmd == "lend_book" and len(parts) >= 3:
                user_id = parts[1]
                title = " ".join(parts[2:])

                # Step 1: validate user_id (authorization)
                auth = _ask_internal(internal_host, users_port, f"check_user {user_id}")
                if auth != "success":
                    send_line(conn, "failure")
                    logger.write(
                        f"handler: lend failure (reason=no such user) user_id={user_id} title='{title}'"
                    )
                    return

                # Step 2: check book status for better error logging
                status = _ask_internal(internal_host, books_port, f"status_book {title}")
                if status == "missing":
                    send_line(conn, "failure")
                    logger.write(
                        f"handler: lend failure (reason=book not found) user_id={user_id} title='{title}'"
                    )
                    return

                if status.startswith("lent "):
                    holder = status.split(" ", 1)[1]
                    send_line(conn, "failure")
                    logger.write(
                        f"handler: lend failure (reason=book already lent) user_id={user_id} title='{title}' holder_user_id={holder}"
                    )
                    return

                # Step 3: book is available -> attempt to lend
                resp = _ask_internal(internal_host, books_port, f"lend_book {user_id} {title}")
                send_line(conn, resp)

                # If something unexpected happened, capture the final status for debugging
                if resp != "success":
                    status2 = _ask_internal(internal_host, books_port, f"status_book {title}")
                    logger.write(
                        f"handler: lend failure (reason=unexpected) user_id={user_id} title='{title}' status_after='{status2}'"
                    )

                logger.write(f"handler: replied to {client_addr}: {resp}")

            # --------------------
            # return_book
            # --------------------
            elif cmd == "return_book" and len(parts) >= 3:
                user_id = parts[1]
                title = " ".join(parts[2:])

                # Step 1: validate user_id (authorization)
                auth = _ask_internal(internal_host, users_port, f"check_user {user_id}")
                if auth != "success":
                    send_line(conn, "failure")
                    logger.write(
                        f"handler: return failure (reason=no such user) user_id={user_id} title='{title}'"
                    )
                    return

                # Step 2: verify the book exists and is currently lent to THIS user
                status = _ask_internal(internal_host, books_port, f"status_book {title}")
                if status == "missing":
                    send_line(conn, "failure")
                    logger.write(
                        f"handler: return failure (reason=book not found) user_id={user_id} title='{title}'"
                    )
                    return

                if status == "available":
                    send_line(conn, "failure")
                    logger.write(
                        f"handler: return failure (reason=book is not lent to anyone) user_id={user_id} title='{title}'"
                    )
                    return

                if status.startswith("lent "):
                    holder = status.split(" ", 1)[1]
                    if holder != user_id:
                        send_line(conn, "failure")
                        logger.write(
                            f"handler: return failure (reason=not holder) user_id={user_id} title='{title}' holder_user_id={holder}"
                        )
                        return

                # Step 3: perform return
                resp = _ask_internal(internal_host, books_port, f"return_book {user_id} {title}")
                send_line(conn, resp)

                # If something unexpected happened, capture final status for debugging
                if resp != "success":
                    status2 = _ask_internal(internal_host, books_port, f"status_book {title}")
                    logger.write(
                        f"handler: return failure (reason=unexpected) user_id={user_id} title='{title}' status_after='{status2}'"
                    )

                logger.write(f"handler: replied to {client_addr}: {resp}")

            # --------------------
            # unknown command
            # --------------------
            else:
                send_line(conn, "failure")
                logger.write(
                    f"handler: replied to {client_addr}: failure (unknown command) req='{req}'"
                )

    finally:
        logger.write(f"handler: done for {client_addr}, releasing semaphore")
        sem.release()


def library_main(
    listen_host: str,
    listen_port: int,
    internal_host: str,
    users_port: int,
    books_port: int,
    log_path: str,
):
    """
    Main Library process.

    High-level structure:
    - Spawn two internal manager processes:
        * users_manager_main
        * books_manager_main
    - Start the external server for user clients.
    - For each incoming connection:
        * Acquire semaphore (limit concurrency to 3 handlers).
        * Fork a handler process to serve that request.
    """
    logger = SafeLogger(log_path)
    logger.write(
        f"library started. listen={listen_host}:{listen_port} internal={internal_host} users_port={users_port} books_port={books_port}"
    )
# *****************************
    # Avoid zombies from forked handler processes
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    # -------------------------------
    # Spawn internal manager processes
    # -------------------------------
    pid_users = os.fork()
    if pid_users == 0:
        users_manager_main(internal_host, users_port, log_path)
        os._exit(0)

    pid_books = os.fork()
    if pid_books == 0:
        books_manager_main(internal_host, books_port, log_path)
        os._exit(0)

    # -------------------------------
    # External server for user clients
    # -------------------------------
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen_host, listen_port))
    srv.listen(50)
# *******************************
    # Concurrency limit: at most 3 handler processes at the same time
    sem = Semaphore(3)

    logger.write("library: accepting clients...")
    while True:
        conn, addr = srv.accept()
        client_addr = f"{addr[0]}:{addr[1]}"

        # ---- Added logs to make semaphore behavior visible in the log ----
        logger.write(f"library: connection from {client_addr} -> waiting for semaphore")
        sem.acquire()
        logger.write(f"library: semaphore acquired for {client_addr} -> forking handler")
        # -----------------------------------------------------------------

        pid = os.fork()
        if pid == 0:
            try:
                srv.close()
            except Exception:
                pass

            handle_client(
                conn.fileno(),
                client_addr,
                log_path,
                sem,
                internal_host,
                users_port,
                books_port,
            )
            os._exit(0)
        else:
            conn.close()


if __name__ == "__main__":
    listen_host = os.environ.get("LIB_LISTEN_HOST", "127.0.0.1")
    listen_port = int(os.environ.get("LIB_LISTEN_PORT", "5001"))

    internal_host = os.environ.get("LIB_INTERNAL_HOST", "127.0.0.1")
    users_port = int(os.environ.get("LIB_USERS_PORT", "6001"))
    books_port = int(os.environ.get("LIB_BOOKS_PORT", "6002"))

    log_path = os.environ.get("LIB_LOG_PATH", "./logs/library.log")

    library_main(listen_host, listen_port, internal_host, users_port, books_port, log_path)
