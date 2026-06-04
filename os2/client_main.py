import os
import sys
import time
import threading
import traceback

from common import load_config_from_env, send_line, log_event


def _cmd_and_rest(line: str) -> tuple[str, str]:
    """
    Split a command line into (command, rest_of_line).

    - Command is converted to lowercase for case-insensitive parsing.
    - The remaining part preserves spaces so book titles and user names
      may contain spaces.
    """
    cmd, _, rest = line.strip().partition(" ")
    return cmd.strip().lower(), rest.strip()


def user_thread(user_name: str, cmd_file: str):
    """
    Worker thread that simulates a single user.

    Each thread:
    - Reads commands from its assigned file
    - Sends requests to the Library server
    - Maintains its own user_id (assigned after successful registration)

    Protocol:
    - register_user           -> USER port
    - register_book/lend/return -> BOOK port
    """
    cfg = load_config_from_env()
    uid = None  # Will be assigned after successful register_user

    abs_path = os.path.abspath(cmd_file)
    log_event(
        cfg.log_path,
        "client",
        f"{user_name} thread started cmd_file='{cmd_file}' abs='{abs_path}'",
    )

    try:
        # Ensure the commands file exists
        if not os.path.exists(abs_path):
            log_event(
                cfg.log_path,
                "client",
                f"{user_name} ERROR: commands file not found: {abs_path}",
            )
            return

        # Process the commands file line by line
        with open(abs_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()

                # Ignore empty lines and comments
                if not line or line.startswith("#"):
                    continue

                cmd, rest = _cmd_and_rest(line)

                # -----------------------
                # Sleep command
                #   sleep <seconds>
                # -----------------------
                if cmd == "sleep":
                    try:
                        sec = float(rest) if rest else 0.0
                    except ValueError:
                        sec = 0.0
                    log_event(cfg.log_path, "client", f"{user_name} Sleep {sec}")
                    time.sleep(sec)
                    continue

                # -----------------------
                # Register user
                #   register | register_user [name]
                # If name is omitted, use the thread name (user1, user2, ...)
                # Sent to USER port
                # -----------------------
                if cmd in ("register", "register_user"):
                    name = rest if rest else user_name
                    resp = send_line(
                        cfg.host,
                        cfg.user_port,
                        f"register_user {name}",
                    )
                    log_event(
                        cfg.log_path,
                        "client",
                        f"{user_name} register_user {name} -> {resp}",
                    )

                    # Extract and store the user id on success
                    if resp.startswith("success"):
                        try:
                            uid = int(resp.split()[1])
                        except Exception:
                            uid = None
                    continue

                # -----------------------
                # Register book
                #   register_book <title>
                # Sent to BOOK port
                # -----------------------
                if cmd in ("registerbook", "register_book"):
                    title = rest
                    if not title:
                        log_event(
                            cfg.log_path,
                            "client",
                            f"{user_name} register_book skipped (no title)",
                        )
                        continue
                    resp = send_line(
                        cfg.host,
                        cfg.book_port,
                        f"register_book {title}",
                    )
                    log_event(
                        cfg.log_path,
                        "client",
                        f"{user_name} register_book {title} -> {resp}",
                    )
                    continue

                # -----------------------
                # Lend book
                #   lend | lend_book <title>
                # Requires successful registration (uid must be set)
                # Sent to BOOK port
                # -----------------------
                if cmd in ("lend", "lend_book"):
                    title = rest
                    if not title:
                        log_event(
                            cfg.log_path,
                            "client",
                            f"{user_name} lend skipped (no title)",
                        )
                        continue
                    if uid is None:
                        log_event(
                            cfg.log_path,
                            "client",
                            f"{user_name} lend {title} skipped (not registered)",
                        )
                        continue

                    resp = send_line(
                        cfg.host,
                        cfg.book_port,
                        f"lend_book {uid} {title}",
                    )
                    log_event(
                        cfg.log_path,
                        "client",
                        f"{user_name} lend_book {uid} {title} -> {resp}",
                    )
                    continue

                # -----------------------
                # Return book
                #   return | return_book <title>
                # Requires successful registration (uid must be set)
                # Sent to BOOK port
                # -----------------------
                if cmd in ("return", "return_book"):
                    title = rest
                    if not title:
                        log_event(
                            cfg.log_path,
                            "client",
                            f"{user_name} return skipped (no title)",
                        )
                        continue
                    if uid is None:
                        log_event(
                            cfg.log_path,
                            "client",
                            f"{user_name} return {title} skipped (not registered)",
                        )
                        continue

                    resp = send_line(
                        cfg.host,
                        cfg.book_port,
                        f"return_book {uid} {title}",
                    )
                    log_event(
                        cfg.log_path,
                        "client",
                        f"{user_name} return_book {uid} {title} -> {resp}",
                    )
                    continue

                # -----------------------
                # Unknown command
                # -----------------------
                log_event(
                    cfg.log_path,
                    "client",
                    f"{user_name} UNKNOWN command line: '{line}'",
                )

        # Completed the command file successfully
        log_event(cfg.log_path, "client", f"{user_name} thread finished normally")

    except Exception as e:
        # Log unexpected exceptions with full stack trace
        log_event(cfg.log_path, "client", f"{user_name} EXCEPTION: {e}")
        log_event(cfg.log_path, "client", traceback.format_exc())


def main() -> int:
    """
    Client main entry point.

    - Accepts N command files as arguments.
    - Spawns one thread per file (user1, user2, ...).
    - Waits for all user threads to complete.
    """
    if len(sys.argv) < 2:
        print("Usage: python3 client_main.py <commands/user1.txt> [commands/user2.txt ...]")
        return 2

    cmd_files = sys.argv[1:]
    cfg = load_config_from_env()

    log_event(cfg.log_path, "client", f"client_main started argv={cmd_files}")

    # Start one worker thread per command file
    threads = []
    for i, path in enumerate(cmd_files, start=1):
        user_name = f"user{i}"
        t = threading.Thread(target=user_thread, args=(user_name, path))
        t.start()
        threads.append(t)

    # Wait for all user threads to finish
    for t in threads:
        t.join()

    log_event(cfg.log_path, "client", "client_main finished")
    return 0


if __name__ == "__main__":
    # Exit with the return code of main()
    raise SystemExit(main())
