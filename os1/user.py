import os
import sys
import time
import socket

from common import SafeLogger, send_line, recv_line


def do_request(host: str, port: int, line: str) -> str:
    """
    Send exactly one request line to the Library server and read exactly one response line.

    This helper:
    - Opens a TCP connection to (host, port)
    - Sends `line` as a newline-terminated command
    - Receives one newline-terminated response
    - Closes the connection automatically via `with s:`
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    with s:
        send_line(s, line)
        return recv_line(s).strip()


def user_main(cmd_file: str, lib_host: str, lib_port: int, log_path: str, user_name: str):
    """
    User (client) process main.

    Responsibilities:
    - Read a pre-written command script file (one command per line).
    - Execute commands in order, possibly sleeping between actions.
    - For each library-related action, connect to the Library server and send a request.
    - Log all actions and responses to the shared log file.
    """
    logger = SafeLogger(log_path)
    logger.write(f"user '{user_name}' started with cmd_file={cmd_file}")

    # Will be set after successful "Register"
    user_id = None

    # Read the user's command script file line-by-line
    with open(cmd_file, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()

            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue

            parts = line.split()
            op = parts[0].lower()

            # --------------------
            # Sleep <seconds>
            # --------------------
            if op == "sleep" and len(parts) == 2:
                sec = float(parts[1])
                logger.write(f"user '{user_name}': Sleep {sec}")
                time.sleep(sec)

            # --------------------
            # Register
            # (maps to library command: register_user <user_name>)
            # --------------------
            elif op == "register":
                resp = do_request(lib_host, lib_port, f"register_user {user_name}")
                logger.write(f"user '{user_name}': register -> {resp}")

                # On success, store the returned user_id for later lend/return
                if resp.startswith("success"):
                    user_id = resp.split()[1]

            # --------------------
            # Register_book <book_title...>
            # (maps to: register_book <book_title>)
            # --------------------
            elif op == "register_book" and len(parts) >= 2:
                title = " ".join(parts[1:])
                resp = do_request(lib_host, lib_port, f"register_book {title}")
                logger.write(f"user '{user_name}': register_book '{title}' -> {resp}")

            # --------------------
            # Lend <book_title...>
            # (maps to: lend_book <user_id> <book_title>)
            # --------------------
            elif op == "lend" and len(parts) >= 2:
                title = " ".join(parts[1:])

                # Must register first to obtain a user_id
                if not user_id:
                    logger.write(f"user '{user_name}': Lend '{title}' skipped (not registered)")
                    continue

                resp = do_request(lib_host, lib_port, f"lend_book {user_id} {title}")
                logger.write(f"user '{user_name}': lend '{title}' -> {resp}")

            # --------------------
            # Return <book_title...>
            # (maps to: return_book <user_id> <book_title>)
            # --------------------
            elif op == "return" and len(parts) >= 2:
                title = " ".join(parts[1:])

                # Must register first to obtain a user_id
                if not user_id:
                    logger.write(f"user '{user_name}': Return '{title}' skipped (not registered)")
                    continue

                resp = do_request(lib_host, lib_port, f"return_book {user_id} {title}")
                logger.write(f"user '{user_name}': return '{title}' -> {resp}")

            # --------------------
            # Unknown command
            # --------------------
            else:
                logger.write(f"user '{user_name}': unknown command line: {line}")

    logger.write(f"user '{user_name}' finished.")


if __name__ == "__main__":
    # Expected argv: user.py <commands_file>
    if len(sys.argv) != 2:
        print("Usage: python3 src/user.py <commands_file>", file=sys.stderr)
        sys.exit(1)

    # Command script file path
    cmd_file = sys.argv[1]

    # Library server connection settings (provided by builder.py via environment variables)
    lib_host = os.environ.get("LIB_LISTEN_HOST", "127.0.0.1")
    lib_port = int(os.environ.get("LIB_LISTEN_PORT", "5001"))

    # Shared log path (also provided by builder.py)
    log_path = os.environ.get("LIB_LOG_PATH", "./logs/library.log")

    # Logical user name (provided by builder.py), otherwise fallback to PID-based name
    user_name = os.environ.get("USER_NAME", f"user{os.getpid()}")

    user_main(cmd_file, lib_host, lib_port, log_path, user_name)
