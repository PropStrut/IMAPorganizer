#!/usr/bin/env python3
"""IMAPorganizer - move email from listed senders into a review folder.

A dry run only reports what would be moved. A live run moves the matching
messages (after you confirm) and logs every move.

Run order:
   1. load IMAPorganizer.ini (same folder as this script) and validate it
   2. set up logging from the [global] settings (log file + screen output)
   3. parse the command-line switches:
        --dry-run | --live            skip the dry-run prompt
        --keep AGE                    skip the keep-recent prompt
        --keep-flagged | --move-flagged   skip the flagged prompt
        --yes                         live run: skip the "Move N messages?" question
        --suggest [N]                 also list the N most common sender domains
                                      that are NOT on your sender list
   4. ask which account to use
   5. ask for that account's password (never stored, never printed, NEVER
      logged)
   6. ask how much recent mail to keep in place: now, 30d, 2w, 3m, 1y or a
      date such as 2026-08-04. Messages RECEIVED on or after that day are
      never moved; only older ones may be. (The default answer comes from
      the account's own keep_recent, else the [global] one.)
   7. ask whether flagged (starred) messages stay in place (default from
      the account's own keep_flagged, else the [global] one)
   8. ask dry-run or live
   9. log the settings for this run
  10. connect (TLS, or STARTTLS on port 143) and log in
  11. list the server's folders and check that the source and review
      folders exist. If the review folder is missing: a dry run only
      reports it; a live run asks whether to create it (default: stop).
      Folder names in the config are relative to the server's personal
      folder prefix: where personal folders live under 'INBOX.', the name
      IMAPorganizer means INBOX.IMAPorganizer.
  12. read the sender, subject, flags and received date of every message in
      the source folder (read-only: the folder is opened read-only and
      messages are NOT marked as read), and report which messages match the
      sender list and which of those would be moved or kept.
  13. dry run: stop here. Live run: show how many messages will move and ask
      for confirmation (default: no; --yes skips the question).
  14. live run, after you confirm: re-open the folder for changes, check that
      its UIDVALIDITY is unchanged, re-read the flags of the messages about
      to move (anything flagged or removed since the scan is left alone) and
      move them to the review folder in batches, logging each one. The server's
      MOVE command is used if available; otherwise COPY + delete + UID EXPUNGE
      (which needs UIDPLUS, so only the moved messages are ever expunged).
      A server with neither is refused: nothing is moved.

Log levels (most to least important):
  ERROR/WARNING  problems
  NOTICE         the essentials of a run (account, mode, counts, results)
  INFO           per-message detail (each matched sender / message)
  DEBUG          technical detail

  log_level (file):      NONE = no log file, INFO = everything except DEBUG,
                         DEBUG = everything
  screen_output:         quiet = warnings/errors only, summary = NOTICE and
                         up, verbose = INFO and up (every message)
"""

import argparse
import base64
import calendar
import configparser
import datetime
import email
import email.policy
import email.utils
import getpass
import imaplib
import logging
import os
import re
import socket
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "IMAPorganizer.ini"

# Built-in defaults for the [global] section (used if a setting is missing).
GLOBAL_DEFAULTS = {
    "source_folder": "INBOX",
    "review_folder": "IMAPorganizer",
    "dry_run_default": "yes",
    "log_file": "IMAPorganizer.log",
    "log_level": "INFO",
    "screen_output": "summary",
    "keep_recent": "now",
    "keep_flagged": "yes",
}
LOG_LEVELS = ("NONE", "INFO", "DEBUG")
SCREEN_LEVELS = ("quiet", "summary", "verbose")

ACCOUNT_REQUIRED = ("server", "senders")
ACCOUNT_OPTIONAL = (
    "port",
    "username",
    "source_folder",
    "review_folder",
    "keep_recent",
    "keep_flagged",
)
DEFAULT_PORT = 993
STARTTLS_PORT = 143


@dataclass
class GlobalSettings:
    source_folder: str
    review_folder: str
    dry_run_default: bool
    log_file: Path
    log_level: str
    screen_output: str
    keep_recent: str  # now, 30d, 2w, 3m, 1y or YYYY-MM-DD (validated)
    keep_flagged: bool


@dataclass
class Account:
    email: str
    server: str
    port: int
    security: str  # "TLS" or "STARTTLS"
    username: str
    source_folder: str
    review_folder: str
    senders: list
    keep_recent: str  # this account's default answer (own setting, else global)
    keep_flagged: bool  # likewise


def sender_entry_is_valid(entry):
    """Valid forms: someone@example.com  or  *@example.com"""
    if any(ch.isspace() for ch in entry) or entry.count("@") != 1:
        return False
    local, domain = entry.split("@")
    if not local or not domain or "*" in domain:
        return False
    if "*" in local and local != "*":
        return False
    return True


def load_config(path):
    """Return (GlobalSettings, [Account, ...], [error strings])."""
    if not path.is_file():
        return None, [], [f"Config file not found: {path}"]

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        return None, [], [f"Could not parse {path.name}: {exc}"]

    errors = []

    # ---- [global] ----
    raw = dict(GLOBAL_DEFAULTS)
    if parser.has_section("global"):
        for key, value in parser.items("global"):
            if key not in GLOBAL_DEFAULTS:
                errors.append(f"[global] unknown setting: {key}")
            else:
                raw[key] = value.strip()

    dry_run = parser.BOOLEAN_STATES.get(raw["dry_run_default"].lower())
    if dry_run is None:
        errors.append("[global] dry_run_default must be yes or no")
        dry_run = True

    keep_flagged = parser.BOOLEAN_STATES.get(raw["keep_flagged"].lower())
    if keep_flagged is None:
        errors.append("[global] keep_flagged must be yes or no")
        keep_flagged = True

    try:
        resolve_keep_recent(raw["keep_recent"], today())
    except ValueError as exc:
        errors.append(f"[global] keep_recent: {exc}")

    log_level = raw["log_level"].upper()
    if log_level not in LOG_LEVELS:
        errors.append(f"[global] log_level must be one of: {', '.join(LOG_LEVELS)}")

    screen_output = raw["screen_output"].lower()
    if screen_output not in SCREEN_LEVELS:
        errors.append(
            f"[global] screen_output must be one of: {', '.join(SCREEN_LEVELS)}"
        )

    for key in ("source_folder", "review_folder", "log_file"):
        if not raw[key]:
            errors.append(f"[global] {key} must not be empty")

    log_file = Path(raw["log_file"]).expanduser()
    if not log_file.is_absolute():
        log_file = SCRIPT_DIR / log_file

    settings = GlobalSettings(
        source_folder=raw["source_folder"],
        review_folder=raw["review_folder"],
        dry_run_default=dry_run,
        log_file=log_file,
        log_level=log_level,
        screen_output=screen_output,
        keep_recent=raw["keep_recent"].strip(),
        keep_flagged=keep_flagged,
    )

    # ---- accounts (every section except [global]) ----
    accounts = []
    for section in parser.sections():
        if section == "global":
            continue
        label = f"[{section}]"
        items = dict(parser.items(section))

        if "@" not in section or any(ch.isspace() for ch in section):
            errors.append(f"{label} section name should be an email address")

        for key in items:
            if key not in ACCOUNT_REQUIRED + ACCOUNT_OPTIONAL:
                errors.append(f"{label} unknown setting: {key}")
        for key in ACCOUNT_REQUIRED:
            if not items.get(key, "").strip():
                errors.append(f"{label} missing required setting: {key}")

        # port
        port = DEFAULT_PORT
        port_text = items.get("port", "").strip()
        if port_text:
            if port_text.isdigit() and 1 <= int(port_text) <= 65535:
                port = int(port_text)
            else:
                errors.append(f"{label} port must be a number from 1 to 65535")

        # senders: one per line, case-insensitive
        senders = []
        for line in items.get("senders", "").splitlines():
            entry = line.strip().lower()
            if not entry:
                continue
            if sender_entry_is_valid(entry):
                senders.append(entry)
            else:
                errors.append(f"{label} invalid sender entry: {line.strip()!r}")

        # keep rules: the account's own setting, else the [global] value
        keep_recent = items.get("keep_recent", "").strip()
        if keep_recent:
            try:
                resolve_keep_recent(keep_recent, today())
            except ValueError as exc:
                errors.append(f"{label} keep_recent: {exc}")
        else:
            keep_recent = settings.keep_recent

        keep_flagged = settings.keep_flagged
        flagged_text = items.get("keep_flagged", "").strip()
        if flagged_text:
            parsed = parser.BOOLEAN_STATES.get(flagged_text.lower())
            if parsed is None:
                errors.append(f"{label} keep_flagged must be yes or no")
            else:
                keep_flagged = parsed

        accounts.append(
            Account(
                email=section,
                server=items.get("server", "").strip(),
                port=port,
                security="STARTTLS" if port == STARTTLS_PORT else "TLS",
                username=items.get("username", "").strip() or section,
                source_folder=items.get("source_folder", "").strip()
                or settings.source_folder,
                review_folder=items.get("review_folder", "").strip()
                or settings.review_folder,
                senders=senders,
                keep_recent=keep_recent,
                keep_flagged=keep_flagged,
            )
        )

    for account in accounts:
        same = account.source_folder == account.review_folder or (
            account.source_folder.upper() == "INBOX"
            and account.review_folder.upper() == "INBOX"
        )
        if same:
            errors.append(
                f"[{account.email}] source_folder and review_folder are the "
                f"same ({account.source_folder!r})"
            )

    if not accounts:
        errors.append("No accounts found in the config file")

    return settings, accounts, errors


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

NOTICE = 25  # between INFO (20) and WARNING (30)
logging.addLevelName(NOTICE, "NOTICE")
log = logging.getLogger("IMAPorganizer")

FILE_LEVELS = {"INFO": logging.INFO, "DEBUG": logging.DEBUG}
SCREEN_LEVEL_MAP = {
    "quiet": logging.WARNING,
    "summary": NOTICE,
    "verbose": logging.INFO,
}


def notice(message, *args):
    log.log(NOTICE, message, *args)


class ScreenFormatter(logging.Formatter):
    """Plain text for normal messages; 'WARNING: ...' prefix for problems."""

    def format(self, record):
        text = record.getMessage()
        if record.levelno >= logging.WARNING:
            return f"{record.levelname}: {text}"
        return text


def setup_logging(settings):
    """Configure the file and screen handlers from the [global] settings.

    Raises OSError if a log file is wanted but cannot be opened. (We refuse
    to run without the audit trail the user asked for.)
    """
    log.setLevel(logging.DEBUG)  # handlers do the filtering
    log.propagate = False
    log.handlers.clear()

    screen = logging.StreamHandler(sys.stdout)
    screen.setLevel(SCREEN_LEVEL_MAP[settings.screen_output])
    screen.setFormatter(ScreenFormatter())
    log.addHandler(screen)

    if settings.log_level != "NONE":
        # Create the file private (owner only) if it doesn't exist yet.
        fd = os.open(settings.log_file, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
        handler = logging.FileHandler(settings.log_file, encoding="utf-8")
        handler.setLevel(FILE_LEVELS[settings.log_level])
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"
            )
        )
        log.addHandler(handler)


def choose_account(accounts):
    """First user prompt: pick one account. Enter selects the first."""
    print("Accounts:")
    for number, account in enumerate(accounts, start=1):
        note = "  (default)" if number == 1 else ""
        print(f"  {number}. {account.email}{note}")
    while True:
        answer = input(f"Choose an account [1-{len(accounts)}] (Enter = 1): ").strip()
        if answer == "":
            return accounts[0]
        if answer.isdigit() and 1 <= int(answer) <= len(accounts):
            return accounts[int(answer) - 1]
        print(f"Please enter a number from 1 to {len(accounts)}.")


# ---------------------------------------------------------------------------
# "Keep recent" cutoff
# ---------------------------------------------------------------------------

RELATIVE_PATTERN = re.compile(
    r"^(\d+)\s*(d|day|days|w|week|weeks|m|month|months|y|year|years)$"
)
ISO_DATE_PATTERN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
KEEP_HELP = "now, 30d, 2w, 3m, 1y, or a date like 2026-08-04"


def today():
    """Today's date (a function so tests can fix it)."""
    return datetime.date.today()


def subtract_months(day, months):
    """Calendar-month subtraction; the day is clamped (Mar 31 - 1 month = Feb 28)."""
    total = day.year * 12 + (day.month - 1) - months
    year, month0 = divmod(total, 12)
    if year < 1:
        raise ValueError("that goes back too far")
    month = month0 + 1
    return datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def resolve_keep_recent(text, current_day):
    """Turn 'now', '3m', '2 weeks' or '2026-08-04' into a cutoff date.

    Returns None for 'now' (no age limit). Messages received ON OR AFTER the
    cutoff date are kept in place; only older messages may be moved.
    Raises ValueError with a message fit to show the user.
    """
    value = text.strip().lower()
    if not value:
        raise ValueError("no value given")
    if value == "now":
        return None

    iso = ISO_DATE_PATTERN.match(value)
    if iso:
        try:
            cutoff = datetime.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            raise ValueError(f"{text.strip()!r} is not a valid date (use YYYY-MM-DD)") from None
        if cutoff > current_day:
            raise ValueError(f"{cutoff.isoformat()} is in the future")
        return cutoff

    relative = RELATIVE_PATTERN.match(value)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2)[0]
        try:
            if unit == "d":
                return current_day - datetime.timedelta(days=amount)
            if unit == "w":
                return current_day - datetime.timedelta(weeks=amount)
            if unit == "m":
                return subtract_months(current_day, amount)
            return subtract_months(current_day, amount * 12)
        except OverflowError:
            raise ValueError("that goes back too far") from None

    raise ValueError(f"{text.strip()!r} is not understood. Use {KEEP_HELP}.")


def describe_cutoff(cutoff):
    if cutoff is None:
        return "no age limit: every match is eligible to be moved"
    return (
        f"messages received on or after {cutoff.isoformat()} stay in place; "
        "only older matches may be moved"
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Move email from listed senders into a review folder."
    )
    # argparse refuses to run (and says so) if both switches are given.
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="dry-run: report what would be moved, change nothing "
        "(skips the dry-run prompt)",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="live run: really move messages (skips the dry-run prompt)",
    )

    parser.add_argument(
        "--keep",
        metavar="AGE",
        type=keep_recent_argument,
        help=f"keep recent mail in place: {KEEP_HELP} (skips the keep-recent prompt)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="live run: do not ask for confirmation before moving messages",
    )
    parser.add_argument(
        "--suggest",
        nargs="?",
        const=10,
        type=positive_int,
        metavar="N",
        help="also list the N (default 10) most common sender domains that are "
        "NOT on your sender list, as ready-to-paste *@domain entries",
    )
    flagged = parser.add_mutually_exclusive_group()
    flagged.add_argument(
        "--keep-flagged",
        action="store_true",
        help="never move flagged (starred) messages (skips the flagged prompt)",
    )
    flagged.add_argument(
        "--move-flagged",
        action="store_true",
        help="treat flagged (starred) messages like any other (skips the flagged prompt)",
    )
    return parser.parse_args()


def positive_int(text):
    """argparse type: a whole number of 1 or more."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError("must be a whole number of 1 or more")
    return value


def keep_recent_argument(text):
    """argparse type for --keep: validate now, resolve later."""
    try:
        resolve_keep_recent(text, today())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return text


def get_password(account):
    """Password source #1: prompt at run time (no echo).

    Kept as its own function so other password sources can be added later
    without touching the rest of the script.
    """
    while True:
        password = getpass.getpass(f"Password for {account.email}: ")
        if password:
            return password
        print("Password cannot be empty.")


def choose_mode(args, settings):
    """Return True for dry-run, False for live.

    A command-line switch wins and skips the prompt. Otherwise ask, with the
    default taken from dry_run_default in the config.
    """
    if args.dry_run:
        return True
    if args.live:
        return False

    default_dry = settings.dry_run_default
    hint = "[Y/n]" if default_dry else "[y/N]"
    while True:
        answer = input(f"Dry-run (report only, change nothing)? {hint}: ")
        answer = answer.strip().lower()
        if answer == "":
            return default_dry
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def choose_keep_recent(args, account):
    """Return the cutoff date (None = no age limit). --keep skips the prompt.

    The prompt's default is this account's setting (its own, else global).
    """
    if args.keep is not None:
        return resolve_keep_recent(args.keep, today())

    default = account.keep_recent
    while True:
        answer = input(f"Keep messages newer than (Enter = {default}; e.g. {KEEP_HELP}): ")
        try:
            return resolve_keep_recent(answer.strip() or default, today())
        except ValueError as exc:
            print(f"  {exc}")


def choose_keep_flagged(args, account):
    """Return True to keep flagged (starred) messages in place."""
    if args.keep_flagged:
        return True
    if args.move_flagged:
        return False
    return ask_yes_no(
        "Keep flagged (starred) messages in place?", default=account.keep_flagged
    )


# ---------------------------------------------------------------------------
# IMAP connection and folder checks
# ---------------------------------------------------------------------------

CONNECT_TIMEOUT = 30  # seconds


class ImapProblem(Exception):
    """A server/connection problem, worded for the user."""


def imap_utf7_encode(text):
    """Encode a folder name the way IMAP servers expect (modified UTF-7)."""
    out, pending = [], []

    def flush():
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            b64 = base64.b64encode(raw).decode("ascii").rstrip("=")
            out.append("&" + b64.replace("/", ",") + "-")
            pending.clear()

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(text):
    """Decode a server folder name (modified UTF-7) into normal text."""
    out, i = [], 0
    while i < len(text):
        if text[i] != "&":
            out.append(text[i])
            i += 1
            continue
        end = text.find("-", i)
        if end == -1:  # malformed: keep as is
            out.append(text[i:])
            break
        chunk = text[i + 1 : end]
        if chunk == "":
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                out.append(text[i : end + 1])
        i = end + 1
    return "".join(out)


def quote_mailbox(encoded_name):
    """Quote a (UTF-7 encoded) folder name for use in an IMAP command."""
    escaped = encoded_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass
class Folder:
    name: str  # as you would type it in the config
    raw: str  # as the server writes it (modified UTF-7)
    flags: set  # lower-case, e.g. {"\\haschildren", "\\noselect"}


LIST_PATTERN = re.compile(
    rb'\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)',
    re.DOTALL,
)


def unquote_imap_string(text):
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return re.sub(r"\\(.)", r"\1", text[1:-1])
    return text


def parse_list_response(items):
    """Turn the server's LIST reply into a list of Folder objects."""
    folders = []
    for item in items:
        if not item:
            continue
        if isinstance(item, tuple):  # name sent as a literal: (head, name)
            match = LIST_PATTERN.match(item[0])
            raw = item[1].decode("utf-8", "replace") if match else None
        else:
            match = LIST_PATTERN.match(item)
            raw = (
                unquote_imap_string(match.group("name").decode("utf-8", "replace").strip())
                if match
                else None
            )
        if not match or raw is None:
            continue  # e.g. the stray b')' that follows a literal
        flags = {f.lower() for f in match.group("flags").decode("ascii").split()}
        folders.append(Folder(name=imap_utf7_decode(raw), raw=raw, flags=flags))
    return folders


def find_folder(folders, wanted):
    for folder in folders:
        if folder.name == wanted:
            return folder
        if wanted.upper() == "INBOX" and folder.name.upper() == "INBOX":
            return folder
    return None


def is_selectable(folder):
    return not ({"\\noselect", "\\nonexistent"} & folder.flags)


def error_text(exc):
    parts = [
        a.decode("utf-8", "replace") if isinstance(a, bytes) else str(a)
        for a in exc.args
    ]
    return " ".join(parts).strip() or exc.__class__.__name__


def close_quietly(conn):
    try:
        conn.logout()
    except Exception:  # noqa: BLE001 - we are shutting down anyway
        pass


def connect_and_login(account, password):
    """Open a TLS (or STARTTLS) connection and log in. Returns the connection."""
    where = f"{account.server}:{account.port}"
    context = ssl.create_default_context()
    log.info("Connecting to %s (%s)", where, account.security)
    try:
        if account.security == "TLS":
            conn = imaplib.IMAP4_SSL(
                account.server,
                account.port,
                ssl_context=context,
                timeout=CONNECT_TIMEOUT,
            )
        else:
            conn = imaplib.IMAP4(account.server, account.port, timeout=CONNECT_TIMEOUT)
            conn.starttls(ssl_context=context)
    except ssl.SSLCertVerificationError as exc:
        reason = getattr(exc, "verify_message", None) or str(exc)
        raise ImapProblem(
            f"The TLS certificate from {where} could not be verified ({reason}). "
            "The server name in the config must match the name on the "
            "server's certificate, and the certificate must come from a "
            "trusted authority."
        ) from exc
    except socket.gaierror as exc:
        raise ImapProblem(
            f"Could not find a server named {account.server!r} (DNS lookup failed)."
        ) from exc
    except ConnectionRefusedError as exc:
        raise ImapProblem(
            f"Connection refused by {where}. Check the server name and port."
        ) from exc
    except TimeoutError as exc:
        raise ImapProblem(
            f"Timed out after {CONNECT_TIMEOUT} seconds connecting to {where}."
        ) from exc
    except imaplib.IMAP4.error as exc:
        raise ImapProblem(f"Server problem while connecting: {error_text(exc)}") from exc
    except ssl.SSLError as exc:
        raise ImapProblem(
            f"TLS problem connecting to {where}: {exc}. (Port 993 expects TLS "
            "from the start; port 143 expects STARTTLS.)"
        ) from exc
    except OSError as exc:
        raise ImapProblem(f"Could not connect to {where}: {exc}") from exc

    conn.debug = 0  # imaplib's own debug output can print the password
    try:
        conn.login(account.username, password)
    except imaplib.IMAP4.error as exc:
        close_quietly(conn)
        raise ImapProblem(
            f"Login failed for {account.username}: {error_text(exc)}"
        ) from exc
    except UnicodeEncodeError as exc:
        close_quietly(conn)
        raise ImapProblem(
            "The password contains characters (non-ASCII) that this version "
            "cannot send to the server."
        ) from exc
    except OSError as exc:
        raise ImapProblem(f"Connection lost during login: {exc}") from exc
    return conn


def list_folders(conn):
    try:
        status, data = conn.list()
    except (imaplib.IMAP4.error, OSError) as exc:
        raise ImapProblem(f"Could not list the server's folders: {exc}") from exc
    if status != "OK":
        raise ImapProblem(f"Could not list the server's folders: {reply_text(data)}")
    folders = parse_list_response(data)
    if not folders:
        raise ImapProblem("The server returned no folders.")
    return folders


def message_count(conn, folder):
    """Number of messages in a folder (read-only STATUS), or None."""
    try:
        status, data = conn.status(quote_mailbox(folder.raw), "(MESSAGES)")
    except (imaplib.IMAP4.error, OSError):
        return None
    if status != "OK" or not data:
        return None
    text = b" ".join(
        x if isinstance(x, bytes) else b" ".join(y for y in x if isinstance(y, bytes))
        for x in data
        if x
    )
    match = re.search(rb"MESSAGES\s+(\d+)", text)
    return int(match.group(1)) if match else None


def describe_count(count):
    if count is None:
        return "message count unavailable"
    return f"{count} message{'' if count == 1 else 's'}"


def ask_yes_no(prompt, default):
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{prompt} {hint}: ").strip().lower()
        if answer == "":
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def reply_text(data):
    """Readable text from an imaplib reply (a list of bytes/strings)."""
    return " ".join(
        x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)
        for x in (data or [])
        if x
    ).strip()


NAMESPACE_PATTERN = re.compile(rb'\s*\(\(\s*"((?:[^"\\]|\\.)*)"')


def get_personal_prefix(conn, folders):
    """The prefix personal folders live under on this server.

    Some servers (e.g. Dovecot/cPanel) keep every personal folder under
    'INBOX.' (so a folder called Receipts is really 'INBOX.Receipts');
    others use no prefix. Asks the server (NAMESPACE) and falls back to
    inspecting the folder list.
    """
    try:
        status, data = conn.namespace()
        if status == "OK" and data and isinstance(data[0], bytes):
            match = NAMESPACE_PATTERN.match(data[0])
            if match:
                return imap_utf7_decode(match.group(1).decode("utf-8", "replace"))
    except (imaplib.IMAP4.error, OSError):
        pass  # server doesn't support NAMESPACE: infer it below

    others = [f.name for f in folders if f.name.upper() != "INBOX"]
    for candidate in ("INBOX.", "INBOX/"):
        if others and all(name.startswith(candidate) for name in others):
            return candidate
    return ""


def full_folder_name(wanted, prefix):
    """A config folder name as the server knows it (adds the prefix if needed)."""
    if wanted.upper() == "INBOX" or not prefix or wanted.startswith(prefix):
        return wanted
    return prefix + wanted


def resolve_folder(folders, wanted, prefix):
    """Find a configured folder on the server, with or without the prefix."""
    return find_folder(folders, wanted) or find_folder(
        folders, full_folder_name(wanted, prefix)
    )


def offer_to_create_review_folder(conn, full_name, prefix):
    """Live runs only: the review folder is missing. Stop, or create it."""
    log.warning("Review folder %r does not exist on the server.", full_name)
    create = ask_yes_no(
        f"Create the folder {full_name!r} now? (No = stop here)",
        default=False,
    )
    if not create:
        log.warning("Stopped: the review folder does not exist and was not created.")
        return False

    quoted = quote_mailbox(imap_utf7_encode(full_name))
    try:
        status, data = conn.create(quoted)
    except imaplib.IMAP4.error as exc:
        status, data = "NO", [error_text(exc)]
    if status != "OK":
        log.error("The server would not create %r: %s", full_name, reply_text(data))
        if not prefix:
            log.error(
                "Some servers keep personal folders under a prefix such as "
                "'INBOX.'. If yours does, set review_folder = INBOX.%s in the "
                "config.",
                full_name,
            )
        return False
    notice("Created review folder %s", full_name)

    # Subscribe so the folder shows up in email clients that hide
    # unsubscribed folders. Failure here is not serious.
    try:
        status, data = conn.subscribe(quoted)
        if status != "OK":
            log.warning("Folder created but could not be subscribed: %s", reply_text(data))
    except (imaplib.IMAP4.error, OSError) as exc:
        log.warning("Folder created but could not be subscribed: %s", exc)
    return True


def check_folders(conn, account, folders, dry_run):
    """Check the source and review folders.

    Returns (source_folder, review_folder_name) if OK to continue, else None.
    """
    log.info("The server has %d folder%s.", len(folders), "" if len(folders) == 1 else "s")
    for folder in folders:
        log.debug("  folder: %s", folder.name)

    prefix = get_personal_prefix(conn, folders)
    log.info("Personal folder prefix on this server: %r", prefix)

    source = resolve_folder(folders, account.source_folder, prefix)
    if source is None or not is_selectable(source):
        names = sorted(f.name for f in folders if is_selectable(f))
        log.error("Source folder %r was not found on the server.", account.source_folder)
        log.error("Folders on the server: %s", ", ".join(names))
        return None
    notice(
        "Source folder %s: %s", source.name, describe_count(message_count(conn, source))
    )

    review = resolve_folder(folders, account.review_folder, prefix)
    if review is not None:
        if not is_selectable(review):
            log.error(
                "%r exists on the server but cannot hold messages.", review.name
            )
            return None
        notice(
            "Review folder %s: exists (%s)",
            review.name,
            describe_count(message_count(conn, review)),
        )
        return source, review.name

    full_name = full_folder_name(account.review_folder, prefix)
    if dry_run:
        notice(
            "Review folder %s does not exist yet. (A live run would offer to "
            "create it; a dry run changes nothing.)",
            full_name,
        )
        return source, full_name
    if offer_to_create_review_folder(conn, full_name, prefix):
        return source, full_name
    return None


# ---------------------------------------------------------------------------
# Reading messages, matching senders, applying the keep rules (read-only)
# ---------------------------------------------------------------------------

FETCH_BATCH = 250  # messages per FETCH request
UID_PATTERN = re.compile(rb"UID\s+(\d+)")
FLAGS_PATTERN = re.compile(rb"FLAGS\s+\(([^)]*)\)")
SIZE_PATTERN = re.compile(rb"RFC822\.SIZE\s+(\d+)")


@dataclass
class Message:
    uid: int
    senders: list  # lower-case addresses from the From header
    display: str  # the From header as readable text
    subject: str
    date: str  # the Date header (display only; NOT used for the age rule)
    received: object = None  # date the server received it (local), or None
    flagged: bool = False
    seen: bool = False  # has the Seen flag, i.e. it has been read
    size: object = None  # size in bytes (RFC822.SIZE), or None if unknown
    matched_by: str = ""  # the sender-list entry that matched, if any
    keep_reason: str = ""  # "", "flagged", "newer" or "date unknown"

    @property
    def will_move(self):
        """True for a sender match that no keep rule protects."""
        return bool(self.matched_by) and not self.keep_reason


def clean_text(value, limit=80):
    """One tidy printable line (no control characters, bounded length)."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def match_sender(addresses, patterns):
    """Return the sender-list entry that matches one of the addresses, or "".

    Case-insensitive. 'name@example.com' must match exactly;
    '*@example.com' matches any address at exactly example.com
    (not subdomains such as mail.example.com).
    """
    for address in addresses:
        address = address.lower()
        if "@" not in address:
            continue
        domain = address.rsplit("@", 1)[1]
        for pattern in patterns:
            if pattern.startswith("*@"):
                if domain == pattern[2:]:
                    return pattern
            elif address == pattern:
                return pattern
    return ""


def parse_message_headers(uid, raw, meta=b""):
    """Build a Message from raw header bytes plus the FETCH metadata text.

    Never raises on odd headers. 'meta' carries FLAGS and INTERNALDATE.
    """
    senders, display, subject, date = [], "", "", "?"
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        from_header = msg["From"]
        if from_header is not None:
            display = clean_text(from_header)
            senders = [
                a.addr_spec.lower()
                for a in getattr(from_header, "addresses", ())
                if a.addr_spec
            ]
        if msg["Subject"] is not None:
            subject = clean_text(msg["Subject"])
        if msg["Date"] is not None:
            try:
                date = email.utils.parsedate_to_datetime(str(msg["Date"])).strftime(
                    "%Y-%m-%d"
                )
            except (TypeError, ValueError, IndexError):
                date = clean_text(msg["Date"], 25) or "?"
    except Exception as exc:  # noqa: BLE001 - a bad header must not stop the run
        log.debug("Could not fully read the headers of uid %s: %s", uid, exc)

    flagged = seen = False
    found = FLAGS_PATTERN.search(meta)
    if found:
        flag_set = {f.lower() for f in found.group(1).split()}
        flagged = b"\\flagged" in flag_set
        seen = b"\\seen" in flag_set

    received = None
    try:
        parts = imaplib.Internaldate2tuple(meta)
        if parts:
            received = datetime.date(parts[0], parts[1], parts[2])
    except (ValueError, OverflowError):
        received = None

    size = None
    found_size = SIZE_PATTERN.search(meta)
    if found_size:
        size = int(found_size.group(1))

    return Message(
        uid=uid,
        senders=senders,
        display=display,
        subject=subject,
        date=date,
        received=received,
        flagged=flagged,
        seen=seen,
        size=size,
    )


def parse_fetch_response(data):
    """Pull (uid, header_bytes, meta_bytes) out of an imaplib UID FETCH reply.

    meta_bytes is the FETCH text carrying UID, FLAGS and INTERNALDATE.
    Servers differ on whether that text comes before or after the header
    text, so both are handled.
    """
    results, pending = [], None  # pending = [raw_header, meta]
    for item in data:
        if isinstance(item, tuple):
            if pending is not None:
                results.append(pending)
            pending = [item[1], item[0]]
        elif isinstance(item, bytes):
            if pending is not None:
                pending[1] = pending[1] + b" " + item
                results.append(pending)
                pending = None
            elif UID_PATTERN.search(item):  # no header text came with it
                results.append([b"", item])
    if pending is not None:
        results.append(pending)

    out = []
    for raw, meta in results:
        found = UID_PATTERN.search(meta)
        if found:
            out.append((int(found.group(1)), raw, meta))
    return out


def get_uidvalidity(conn):
    """The open folder's UIDVALIDITY (from the last SELECT/EXAMINE), or None.

    Message UIDs are only meaningful while this number stays the same.
    """
    try:
        _, data = conn.response("UIDVALIDITY")
    except (imaplib.IMAP4.error, OSError):
        return None
    for item in reversed(data or []):
        if item:
            try:
                return int(item)
            except (TypeError, ValueError):
                return None
    return None


def fetch_headers(conn, uids, patterns):
    """Read FLAGS, received date and headers for the given UIDs (read-only)."""
    messages = []
    for start in range(0, len(uids), FETCH_BATCH):
        batch = ",".join(str(u) for u in uids[start : start + FETCH_BATCH])
        status, data = conn.uid(
            "FETCH",
            batch,
            "(FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
        )
        if status != "OK":
            raise ImapProblem(f"Could not read messages: {reply_text(data)}")
        for uid, raw, meta in parse_fetch_response(data):
            message = parse_message_headers(uid, raw, meta)
            message.matched_by = match_sender(message.senders, patterns)
            messages.append(message)
        log.info("Read %d of %d messages", len(messages), len(uids))
    return messages


def fetch_messages(conn, folder, patterns):
    """Read every message in a folder and match the senders.

    Strictly read-only: the folder is opened with EXAMINE and everything is
    fetched with BODY.PEEK (FLAGS and INTERNALDATE are read-only items), so
    nothing is changed and nothing is marked read.
    Returns (messages, uidvalidity).
    """
    try:
        status, data = conn.select(quote_mailbox(folder.raw), readonly=True)
        if status != "OK":
            raise ImapProblem(f"Could not open {folder.name}: {reply_text(data)}")
        uidvalidity = get_uidvalidity(conn)
        status, data = conn.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise ImapProblem(f"Could not list messages in {folder.name}: {reply_text(data)}")
        uids = [int(x) for x in (data[0] or b"").split()]
        log.info("Reading %d messages in %s", len(uids), folder.name)
        messages = fetch_headers(conn, uids, patterns)
    except (imaplib.IMAP4.error, OSError) as exc:
        raise ImapProblem(
            f"Problem reading messages from {folder.name}: "
            f"{error_text(exc) if isinstance(exc, imaplib.IMAP4.error) else exc}"
        ) from exc

    if len(messages) != len(uids):
        log.warning(
            "Expected %d messages but the server returned %d.", len(uids), len(messages)
        )
    return messages, uidvalidity


def apply_keep_rules(messages, cutoff, keep_flagged):
    """Decide, for each sender match, whether it stays in place.

    A matching message is kept if it is flagged (when keep_flagged is on),
    or was received on or after the cutoff date. Flagged takes precedence so
    each kept message is counted once. If the received date cannot be read
    and an age limit is set, the message is kept (the safe choice).
    """
    for m in messages:
        m.keep_reason = ""
        if not m.matched_by:
            continue
        if keep_flagged and m.flagged:
            m.keep_reason = "flagged"
        elif cutoff is not None:
            if m.received is None:
                m.keep_reason = "date unknown"
            elif m.received >= cutoff:
                m.keep_reason = "newer"


def keep_reason_text(reason, cutoff):
    if reason == "flagged":
        return "flagged"
    if reason == "newer":
        return f"received on or after {cutoff.isoformat()}"
    return "received date unknown"


def format_size(num_bytes):
    """Human-readable size (1 KB = 1024 bytes)."""
    size = float(num_bytes)
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"


def describe_total_size(messages):
    """Total size of the given messages, noting any whose size is unknown."""
    if not messages:
        return "0 B"
    sizes = [m.size for m in messages if m.size is not None]
    if not sizes:
        return "unknown"
    text = format_size(sum(sizes))
    unknown = len(messages) - len(sizes)
    if unknown:
        text += f" (size unknown for {unknown} message{'' if unknown == 1 else 's'})"
    return text


def describe_message(m):
    return (
        f"[uid {m.uid}] {m.date} | {m.display or '(no From)'} | {m.subject}"
        f"  (matched {m.matched_by})"
    )


def report_matches(messages, source, review_name, patterns, dry_run, cutoff, keep_flagged):
    matched = [m for m in messages if m.matched_by]
    to_move = [m for m in matched if not m.keep_reason]
    verb = "Would move" if dry_run else "Matched"

    for m in matched:
        what = describe_message(m)
        if m.keep_reason:
            log.info("Keeping (%s): %s", keep_reason_text(m.keep_reason, cutoff), what)
        else:
            log.info("%s: %s", verb, what)
    for m in messages:
        if not m.matched_by:
            log.debug("No match: [uid %d] %s | %s", m.uid, m.date, m.display or "(no From)")

    notice(
        "%d of %d messages in %s match the sender list.",
        len(matched), len(messages), source.name,
    )
    for pattern in patterns:
        hits = [m for m in matched if m.matched_by == pattern]
        notice(
            "  %-32s %3d matched, %3d to move",
            pattern, len(hits), sum(1 for m in hits if not m.keep_reason),
        )

    parts = [f"{len(to_move)} to move"]
    if keep_flagged:
        parts.append(f"{sum(1 for m in matched if m.keep_reason == 'flagged')} kept (flagged)")
    if cutoff is not None:
        parts.append(
            f"{sum(1 for m in matched if m.keep_reason == 'newer')} kept "
            f"(received on or after {cutoff.isoformat()})"
        )
    unknown = sum(1 for m in matched if m.keep_reason == "date unknown")
    if unknown:
        parts.append(f"{unknown} kept (received date unknown)")
    notice("Of the %d matches: %s.", len(matched), ", ".join(parts))

    unread = [m for m in to_move if not m.seen]
    if unread:
        notice("Unread messages to move:")
        for pattern in patterns:
            count = sum(1 for m in unread if m.matched_by == pattern)
            if count:
                notice("  %-32s %3d unread", pattern, count)
        notice("Total unread to move: %d of %d.", len(unread), len(to_move))
    else:
        notice("Unread messages to move: none (0 of %d).", len(to_move))

    notice("Total size to move: %s", describe_total_size(to_move))

    plural = "" if len(to_move) == 1 else "s"
    if dry_run:
        notice(
            "Dry run: %d message%s would be moved from %s to %s. Nothing was changed.",
            len(to_move), plural, source.name, review_name,
        )
    elif to_move:
        notice(
            "Live run: %d message%s to move from %s to %s.",
            len(to_move), plural, source.name, review_name,
        )


def sender_domain(message):
    """Domain of the message's first usable From address, or "" if none."""
    for address in message.senders:
        if "@" in address:
            return address.rsplit("@", 1)[1]
    return ""


def report_suggestions(messages, limit, folder_name):
    """--suggest: the most common sender domains that are NOT on the sender list.

    The entries are written as *@domain so they can be pasted straight into
    the senders list. Read-only: it only looks at what was already fetched.
    """
    unmatched = [m for m in messages if not m.matched_by]
    counts, no_from = {}, 0
    for m in unmatched:
        domain = sender_domain(m)
        if domain:
            counts[domain] = counts.get(domain, 0) + 1
        else:
            no_from += 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    notice(
        "Senders not on your list (%d of %d messages in %s), most common domains first:",
        len(unmatched), len(messages), folder_name,
    )
    if not ranked:
        notice("  (none)")
    for domain, count in ranked[:limit]:
        notice("  %-32s %5d", f"*@{domain}", count)
    if len(ranked) > limit:
        more = len(ranked) - limit
        notice("  ... and %d more domain%s", more, "" if more == 1 else "s")
    if no_from:
        notice(
            "  (%d message%s had no readable From address)",
            no_from, "" if no_from == 1 else "s",
        )


def run_summary_line(account, dry_run, messages, outcome):
    """One searchable line summarising the run, for the log file.

    'unread' and 'size' describe the messages that were to be moved;
    'kept' is the number of matching messages left in place.
    """
    matched = [m for m in messages if m.matched_by]
    to_move = [m for m in matched if not m.keep_reason]
    return (
        f"Run summary: {account.email} | {'DRY-RUN' if dry_run else 'LIVE'} | "
        f"scanned {len(messages)} | matched {len(matched)} | {outcome} | "
        f"unread {sum(1 for m in to_move if not m.seen)} | "
        f"kept {len(matched) - len(to_move)} | size {describe_total_size(to_move)}"
    )


# ---------------------------------------------------------------------------
# Moving messages (live runs only)
# ---------------------------------------------------------------------------

MOVE_BATCH = 100  # messages per MOVE / COPY request

# imaplib does not know the MOVE command (RFC 6851); registering it lets
# conn.uid("MOVE", ...) through. It is only valid in the selected state.
imaplib.Commands.setdefault("MOVE", ("SELECTED",))


def get_capabilities(conn):
    """The server's capability words, upper-cased (empty set if unknown)."""
    try:
        status, data = conn.capability()
    except (imaplib.IMAP4.error, OSError):
        return set()
    if status != "OK" or not data or not data[0]:
        return set()
    first = data[0]
    text = first.decode("ascii", "replace") if isinstance(first, bytes) else str(first)
    return set(text.upper().split())


def get_move_method(conn):
    """How this server can move messages safely: "MOVE", "COPY", or None.

    "COPY" means COPY + mark deleted + UID EXPUNGE, which needs UIDPLUS so that
    ONLY the moved messages are expunged (a plain EXPUNGE would also remove
    any other message already marked deleted in the folder).
    """
    caps = get_capabilities(conn)
    log.debug("Server capabilities: %s", " ".join(sorted(caps)) or "(unknown)")
    if "MOVE" in caps:
        return "MOVE"
    if "UIDPLUS" in caps:
        return "COPY"
    return None


def uid_command(conn, *args):
    """conn.uid(...) with protocol errors (BAD) turned into a status."""
    try:
        return conn.uid(*args)
    except imaplib.IMAP4.error as exc:
        return "BAD", [error_text(exc)]


def count_in(conn, folder_name):
    folder = Folder(name=folder_name, raw=imap_utf7_encode(folder_name), flags=set())
    return message_count(conn, folder)


def move_batch(conn, batch, quoted_review, method):
    """Move one batch of messages. Raises ImapProblem if it did not work."""
    uidset = ",".join(str(m.uid) for m in batch)

    if method == "MOVE":
        status, data = uid_command(conn, "MOVE", uidset, quoted_review)
        if status != "OK":
            raise ImapProblem(f"The server refused the move: {reply_text(data)}")
        return

    # COPY, then mark the originals deleted, then expunge exactly those UIDs.
    status, data = uid_command(conn, "COPY", uidset, quoted_review)
    if status != "OK":
        raise ImapProblem(
            "The server refused to copy the messages, so nothing was deleted: "
            f"{reply_text(data)}"
        )
    status, data = uid_command(conn, "STORE", uidset, "+FLAGS.SILENT", "(\\Deleted)")
    if status != "OK":
        raise ImapProblem(
            "The messages were copied but could not be marked for deletion, so "
            f"they now exist in both folders: {reply_text(data)}"
        )
    status, data = uid_command(conn, "EXPUNGE", uidset)
    if status != "OK":
        uid_command(conn, "STORE", uidset, "-FLAGS.SILENT", "(\\Deleted)")  # best-effort undo
        raise ImapProblem(
            "The messages were copied but could not be removed from the source "
            f"folder, so they now exist in both folders: {reply_text(data)}"
        )


def confirm_move(args, count, source_name, review_name):
    """Ask before a live move. --yes skips the question."""
    if args.yes:
        return True
    return ask_yes_no(
        f"Move {count} message{'' if count == 1 else 's'} from {source_name} "
        f"to {review_name}?",
        default=False,
    )


def move_messages(
    conn, source, review_name, candidates, uidvalidity, patterns, cutoff, keep_flagged, method
):
    """Move the confirmed messages to the review folder. Returns how many moved.

    Before anything is moved the folder is re-opened for changes, its
    UIDVALIDITY is compared with the scan's, and the flags of the candidates
    are read again: a message that was flagged, moved or deleted since the
    scan is left alone.
    """
    quoted_review = quote_mailbox(imap_utf7_encode(review_name))
    moved, eligible = 0, []
    try:
        status, data = conn.select(quote_mailbox(source.raw))  # read-write
        if status != "OK":
            raise ImapProblem(f"Could not open {source.name} for changes: {reply_text(data)}")
        current = get_uidvalidity(conn)
        if current is None or current != uidvalidity:
            raise ImapProblem(
                f"{source.name} changed on the server since it was scanned (its "
                "UIDVALIDITY is different or missing), so message numbers can no "
                "longer be trusted. Nothing was moved. Run the script again."
            )

        fresh = fetch_headers(conn, [m.uid for m in candidates], patterns)
        apply_keep_rules(fresh, cutoff, keep_flagged)
        eligible = [m for m in fresh if m.will_move]
        still_eligible = {m.uid for m in eligible}
        skipped = [m for m in candidates if m.uid not in still_eligible]
        if skipped:
            log.warning(
                "%d message%s changed on the server since the scan (flagged, "
                "moved or deleted) and will be left alone.",
                len(skipped), "" if len(skipped) == 1 else "s",
            )
            for m in skipped:
                log.info("Skipped (changed since the scan): %s", describe_message(m))
        if not eligible:
            notice("Nothing left to move.")
            return 0

        before = count_in(conn, review_name)
        log.info("Moving %d message%s using %s", len(eligible),
                 "" if len(eligible) == 1 else "s", method)
        for start in range(0, len(eligible), MOVE_BATCH):
            batch = eligible[start : start + MOVE_BATCH]
            try:
                move_batch(conn, batch, quoted_review, method)
            except ImapProblem as exc:
                raise ImapProblem(
                    f"Stopped after moving {moved} of {len(eligible)} messages. {exc}"
                ) from exc
            moved += len(batch)
            for m in batch:
                log.info("Moved: %s", describe_message(m))
    except KeyboardInterrupt:
        log.warning(
            "Interrupted: %d of %d messages had been moved (a batch that was in "
            "progress may be partly done).", moved, len(eligible),
        )
        raise
    except (imaplib.IMAP4.error, OSError) as exc:
        raise ImapProblem(
            f"Problem while moving messages ({moved} of {len(eligible)} moved so "
            f"far): {error_text(exc) if isinstance(exc, imaplib.IMAP4.error) else exc}. "
            "Check the source and review folders before running again."
        ) from exc

    notice(
        "Moved %d message%s from %s to %s.",
        moved, "" if moved == 1 else "s", source.name, review_name,
    )
    after = count_in(conn, review_name)
    if before is not None and after is not None:
        log.info("Review folder %s now has %d messages (was %d).", review_name, after, before)
        if after - before != moved:
            log.warning(
                "Expected the review folder to grow by %d but it grew by %d. "
                "Check the folder in your email client.", moved, after - before,
            )
    return moved


def report_move_plan(method):
    """Dry runs: say whether a live run could move messages on this server."""
    if method == "MOVE":
        log.info("A live run would move messages with the server's MOVE command.")
    elif method == "COPY":
        log.info("A live run would move messages with COPY + UID EXPUNGE (no MOVE support).")
    else:
        log.warning(
            "A live run could NOT move messages on this server: it supports "
            "neither MOVE nor UIDPLUS."
        )


def log_run_settings(settings, account, dry_run, cutoff, keep_flagged):
    """Log the settings this run would use. (No password, ever.)"""
    notice("=== IMAPorganizer run started ===")
    notice("Account: %s", account.email)
    notice(
        "Mode: %s",
        "DRY-RUN (nothing will be changed)" if dry_run else "LIVE",
    )
    notice(
        "Source folder: %s  ->  review folder: %s",
        account.source_folder,
        account.review_folder,
    )
    notice("Age rule: %s", describe_cutoff(cutoff))
    notice(
        "Flagged (starred) messages: %s",
        "kept in place" if keep_flagged else "may be moved",
    )
    log.info("Server: %s:%d (%s)", account.server, account.port, account.security)
    log.info("Username: %s", account.username)
    log.info("Senders to match (%d):", len(account.senders))
    for entry in account.senders:
        log.info("  %s", entry)
    log.debug("Log file: %s (level %s)", settings.log_file, settings.log_level)
    log.debug("Screen output: %s", settings.screen_output)
    log.debug("Dry-run prompt default: %s", "yes" if settings.dry_run_default else "no")
    log.debug("Keep-recent default: %s (global: %s)", account.keep_recent, settings.keep_recent)
    log.debug(
        "Keep-flagged default: %s (global: %s)",
        "yes" if account.keep_flagged else "no",
        "yes" if settings.keep_flagged else "no",
    )
    log.debug("Password: entered (value is never logged)")


def main():
    args = parse_args()

    settings, accounts, errors = load_config(CONFIG_PATH)
    if errors:
        print(f"Problems found in {CONFIG_PATH.name}:", file=sys.stderr)
        for message in errors:
            print(f"  - {message}", file=sys.stderr)
        return 1

    try:
        setup_logging(settings)
    except OSError as exc:
        print(f"Cannot open log file {settings.log_file}: {exc}", file=sys.stderr)
        print(
            "Fix the path, or set log_level = NONE in the config to run "
            "without a log file.",
            file=sys.stderr,
        )
        return 1

    try:
        account = choose_account(accounts)
        password = get_password(account)  # held in memory only; NEVER log it
        cutoff = choose_keep_recent(args, account)
        keep_flagged = choose_keep_flagged(args, account)
        dry_run = choose_mode(args, settings)
    except (EOFError, KeyboardInterrupt):
        print()
        log.warning("Cancelled by user before the run started")
        return 130

    log_run_settings(settings, account, dry_run, cutoff, keep_flagged)

    conn = None
    exit_code = 0
    messages, outcome = None, None  # set once the scan has finished
    try:
        conn = connect_and_login(account, password)
        notice("Logged in to %s as %s", account.server, account.username)
        folders = list_folders(conn)
        checked = check_folders(conn, account, folders, dry_run)
        if checked is None:
            exit_code = 1
        else:
            source, review_name = checked
            method = get_move_method(conn)
            messages, uidvalidity = fetch_messages(conn, source, account.senders)
            apply_keep_rules(messages, cutoff, keep_flagged)
            report_matches(
                messages, source, review_name, account.senders, dry_run, cutoff, keep_flagged
            )
            if args.suggest:
                report_suggestions(messages, args.suggest, source.name)
            to_move = [m for m in messages if m.will_move]
            if dry_run:
                report_move_plan(method)
                outcome = f"would move {len(to_move)}"
            elif not to_move:
                notice("Nothing to move.")
                outcome = "moved 0"
            else:
                if method is None:
                    raise ImapProblem(
                        "This server supports neither MOVE nor UIDPLUS, so messages "
                        "cannot be moved safely. Nothing was moved."
                    )
                if confirm_move(args, len(to_move), source.name, review_name):
                    moved = move_messages(
                        conn, source, review_name, to_move, uidvalidity,
                        account.senders, cutoff, keep_flagged, method,
                    )
                    outcome = f"moved {moved}"
                else:
                    notice("Cancelled: nothing was moved.")
                    outcome = "moved 0 (declined)"
    except ImapProblem as exc:
        log.error("%s", exc)
        exit_code = 1
        outcome = "FAILED"
    except (EOFError, KeyboardInterrupt):
        print()
        log.warning("Cancelled by user")
        exit_code = 130
        outcome = "CANCELLED"
    finally:
        if conn is not None:
            close_quietly(conn)

    if messages is not None:  # only runs that got as far as reading the folder
        log.info("%s", run_summary_line(account, dry_run, messages, outcome or "no result"))
    notice(
        "=== IMAPorganizer run finished%s ===",
        "" if exit_code == 0 else " WITH PROBLEMS",
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
