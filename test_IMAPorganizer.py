"""Automated tests for IMAPorganizer.py, using a simulated IMAP server.

Run from anywhere:

    python3 test_IMAPorganizer.py        # failures and a summary
    python3 test_IMAPorganizer.py -v     # also list every passing check

No network, account or extra packages are needed. Everything runs in a
temporary folder with its own built-in config and log, so your real
IMAPorganizer.ini and IMAPorganizer.log are never read or changed.
Exit code: 0 if every check passed, 1 otherwise.

The simulated server has no STORE/COPY/MOVE/EXPUNGE methods of its own beyond
what the script is allowed to use, so a script bug that touched messages in a
way it should not would show up as a failure here.
"""
import atexit, contextlib, datetime, imaplib, io, os, re, shutil, socket, ssl, sys, tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))   # find IMAPorganizer.py next to this file
import IMAPorganizer as M

# ---- isolation: a private working folder; the script's config/log paths point into it
WORK = Path(tempfile.mkdtemp(prefix="imaporganizer-tests-"))
atexit.register(shutil.rmtree, WORK, ignore_errors=True)
os.chdir(WORK)
M.SCRIPT_DIR = WORK
M.CONFIG_PATH = WORK / "IMAPorganizer.ini"

VERBOSE = "-v" in sys.argv
TODAY = datetime.date(2026, 11, 4)

TEST_INI = """[global]
source_folder = INBOX
review_folder = IMAPorganizer
dry_run_default = yes
log_file = IMAPorganizer.log
log_level = INFO
screen_output = verbose
keep_recent = now
keep_flagged = yes

[you@example.com]
server = imap.example.com
senders =
    newsletter@somestore.com
    *@marketing.example.org
    *@bigretailer.com

[other.person@example.net]
server = mail.example.net
port = 143
username = otherperson
source_folder = INBOX
review_folder = ToReview
keep_recent = 3m
keep_flagged = no
senders =
    *@spamdomain.com
    alerts@oldservice.net
"""
INI = M.CONFIG_PATH
ORIGINAL_INI = TEST_INI
INI.write_text(ORIGINAL_INI)

def ini_replace(old, new):
    assert old in ORIGINAL_INI, old
    INI.write_text(ORIGINAL_INI.replace(old, new))
def restore_ini():
    INI.write_text(ORIGINAL_INI)

failures = 0
passed = 0
def check(name, cond, detail=""):
    global failures, passed
    if cond:
        passed += 1
        if VERBOSE: print("PASS  " + name)
    else:
        failures += 1
        print(f"FAIL  {name}   <-- {detail}")

FOLDERS = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasNoChildren) "/" "IMAPorganizer"',
    b'(\\HasNoChildren) "/" "Sent Items"',
    b'(\\HasNoChildren) "/" "Caf&AOk-"',
    b'(\\Noselect \\HasChildren) "/" "Archive"',
    b'(\\HasNoChildren) "/" Drafts',
]
FOLDERS_NO_REVIEW = [f for f in FOLDERS if b"IMAPorganizer" not in f]
PREFIXED = [
    b'(\\HasChildren) "." "INBOX"',
    b'(\\HasNoChildren) "." "INBOX.Sent"',
    b'(\\HasNoChildren) "." "INBOX.Receipts"',
    b'(\\HasNoChildren) "." "INBOX.IMAPorganizer"',
]
PREFIXED_NO_REVIEW = [f for f in PREFIXED if b"IMAPorganizer" not in f]
PFX = dict(namespace_reply=b'(("INBOX." ".")) NIL NIL', require_prefix="INBOX.")
DEFAULT_META = ("", "01-Nov-2026 12:00:00 +0000")

class FakeIMAP:
    """Simulated server. It has NO store/copy/move/expunge methods, so any
    attempt by the script to change messages would blow up the test."""
    error = imaplib.IMAP4.error
    instances = []
    capabilities = b"IMAP4rev1 UIDPLUS MOVE"
    def __init__(self, host, port, **kw):
        if FakeIMAP.connect_error: raise FakeIMAP.connect_error
        self.host, self.port, self.kw = host, port, kw
        self.calls, self.starttls_called, self.debug = [], False, 0
        FakeIMAP.instances.append(self)
    def starttls(self, ssl_context=None): self.starttls_called = True
    def login(self, user, pw):
        self.calls.append(("login", user))
        if FakeIMAP.login_error: raise self.error(FakeIMAP.login_error)
        return "OK", [b"ok"]
    def list(self): return "OK", FakeIMAP.folders
    def namespace(self):
        if not FakeIMAP.namespace_supported: raise self.error("NAMESPACE not supported")
        return "OK", [FakeIMAP.namespace_reply]
    def status(self, mbox, what):
        self.calls.append(("status", mbox))
        n = 42 if FakeIMAP.status_frozen else 42 + len(FakeIMAP.review)
        return "OK", [f"{mbox} (MESSAGES {n})".encode()]
    def capability(self):
        self.calls.append(("capability",)); return "OK", [FakeIMAP.capabilities]
    def response(self, code):
        v = FakeIMAP.current_validity
        return code, [None if v is None else str(v).encode()]
    def create(self, mbox):
        self.calls.append(("create", mbox))
        rp = FakeIMAP.require_prefix
        if rp and not mbox.strip('"').startswith(rp):
            return "NO", [b"Client tried to access nonexistent namespace. (Mailbox name should probably be prefixed with: INBOX.) (0.001 + 0.000 secs)."]
        return FakeIMAP.create_status, [b"x"]
    def subscribe(self, mbox):
        self.calls.append(("subscribe", mbox)); return "OK", [b"x"]
    def select(self, mbox, readonly=False):
        self.calls.append(("select", mbox, readonly))
        vals = FakeIMAP.uidvalidity_values
        FakeIMAP.current_validity = vals[min(FakeIMAP.select_count, len(vals) - 1)]
        FakeIMAP.select_count += 1
        if not readonly and FakeIMAP.hook_rw_select: FakeIMAP.hook_rw_select()
        return "OK", [str(len(FakeIMAP.messages)).encode()]
    def _flags(self, u): return FakeIMAP.msg_meta.get(u, DEFAULT_META)[0]
    def _setflags(self, u, flags):
        FakeIMAP.msg_meta[u] = (flags.strip(), FakeIMAP.msg_meta.get(u, DEFAULT_META)[1])
    def uid(self, command, *args):
        self.calls.append(("uid", command, args))
        uids = lambda s: [int(x) for x in s.split(",")]
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(FakeIMAP.messages)).encode()]
        if command == "FETCH":
            if FakeIMAP.fetch_raises: raise FakeIMAP.fetch_raises
            if FakeIMAP.fetch_status != "OK": return FakeIMAP.fetch_status, [b"nope"]
            out = []
            for u in uids(args[0]):
                if u not in FakeIMAP.messages: continue          # vanished messages are simply not returned
                raw = FakeIMAP.messages[u]
                flags, idate = FakeIMAP.msg_meta.get(u, DEFAULT_META)
                meta = f"FLAGS ({flags})" + (f' INTERNALDATE "{idate}"' if idate else "")
                if u in FakeIMAP.msg_sizes: meta += f" RFC822.SIZE {FakeIMAP.msg_sizes[u]}"
                if FakeIMAP.uid_after_literal:
                    out.append((f"{u} (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {{{len(raw)}}}".encode(), raw))
                    out.append(f" UID {u} {meta})".encode())
                else:
                    out.append((f"{u} (UID {u} {meta} BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {{{len(raw)}}}".encode(), raw))
                    out.append(b")")
            return "OK", out
        if command == "MOVE":
            FakeIMAP.move_calls += 1
            if FakeIMAP.interrupt_on_move: raise KeyboardInterrupt()
            if FakeIMAP.fail_move_on == FakeIMAP.move_calls or FakeIMAP.move_status != "OK":
                return "NO", [b"[CANNOT] Move failed"]
            for u in uids(args[0]):
                if u in FakeIMAP.messages: del FakeIMAP.messages[u]; FakeIMAP.review.append(u)
            return "OK", [None]
        if command == "COPY":
            if FakeIMAP.copy_status != "OK": return FakeIMAP.copy_status, [b"[TRYCREATE] no such folder"]
            FakeIMAP.review.extend(u for u in uids(args[0]) if u in FakeIMAP.messages)
            return "OK", [None]
        if command == "STORE":
            if FakeIMAP.store_status != "OK": return FakeIMAP.store_status, [b"store refused"]
            for u in uids(args[0]):
                fl = [f for f in self._flags(u).split() if f != "\\Deleted"]
                if args[1].startswith("+"): fl.append("\\Deleted")
                self._setflags(u, " ".join(fl))
            return "OK", [None]
        if command == "EXPUNGE":
            if FakeIMAP.expunge_status != "OK": return FakeIMAP.expunge_status, [b"expunge refused"]
            for u in uids(args[0]):
                if "\\Deleted" in self._flags(u).split() and u in FakeIMAP.messages: del FakeIMAP.messages[u]
            return "OK", [None]
        raise AssertionError(f"unexpected UID command {command}")
    def logout(self):
        self.calls.append(("logout",)); return "BYE", [b"x"]

def reset_fake(folders=FOLDERS, **attrs):
    FakeIMAP.instances.clear()
    FakeIMAP.folders = folders
    FakeIMAP.login_error = FakeIMAP.connect_error = None
    FakeIMAP.create_status = "OK"
    FakeIMAP.namespace_reply = b'(("" "/")) NIL NIL'
    FakeIMAP.namespace_supported = True
    FakeIMAP.require_prefix = None
    FakeIMAP.messages = {}
    FakeIMAP.msg_meta = {}
    FakeIMAP.uid_after_literal = False
    FakeIMAP.fetch_status = "OK"
    FakeIMAP.fetch_raises = None
    FakeIMAP.capabilities = b"IMAP4rev1 UIDPLUS MOVE"
    FakeIMAP.uidvalidity_values = [1001]; FakeIMAP.current_validity = None; FakeIMAP.select_count = 0
    FakeIMAP.review = []; FakeIMAP.hook_rw_select = None
    FakeIMAP.move_calls = 0; FakeIMAP.fail_move_on = None; FakeIMAP.interrupt_on_move = False
    FakeIMAP.move_status = FakeIMAP.copy_status = FakeIMAP.store_status = FakeIMAP.expunge_status = "OK"
    FakeIMAP.status_frozen = False
    FakeIMAP.msg_sizes = {}
    for k, v in attrs.items(): setattr(FakeIMAP, k, v)

def run(argv, inputs, password="TOPSECRET123", folders=FOLDERS, auto=True, **fake_attrs):
    """Run main() against the fake server -> (exit_code, screen, log_text, conn).
    With auto=True, blank answers are inserted for the keep-recent and
    keep-flagged prompts unless the matching command-line switch is given."""
    if os.path.exists("IMAPorganizer.log"): os.remove("IMAPorganizer.log")
    reset_fake(folders, **fake_attrs)
    if auto and inputs:
        extra = []
        if "--keep" not in argv: extra.append("")
        if "--keep-flagged" not in argv and "--move-flagged" not in argv: extra.append("")
        inputs = [inputs[0]] + extra + list(inputs[1:])
    it = iter(inputs)
    def fake_input(prompt=""):
        print(prompt, end="")
        try: return next(it)
        except StopIteration: raise EOFError
    buf = io.StringIO()
    with mock.patch.object(M.imaplib, "IMAP4_SSL", FakeIMAP), \
         mock.patch.object(M.imaplib, "IMAP4", FakeIMAP), \
         mock.patch.object(M, "today", lambda: TODAY), \
         mock.patch("builtins.input", fake_input), \
         mock.patch("getpass.getpass", lambda prompt="": password), \
         mock.patch.object(sys, "argv", ["IMAPorganizer.py"] + argv), \
         contextlib.redirect_stdout(buf), \
         contextlib.redirect_stderr(io.StringIO()):
        code = M.main()
    log = open("IMAPorganizer.log").read() if os.path.exists("IMAPorganizer.log") else ""
    return code, buf.getvalue(), log, (FakeIMAP.instances[-1] if FakeIMAP.instances else None)

def hdr(frm=None, subject=None, date=None):
    lines = []
    if frm is not None: lines.append("From: " + frm)
    if subject is not None: lines.append("Subject: " + subject)
    if date is not None: lines.append("Date: " + date)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

inp = ["1", ""]   # account 1, accept default mode (dry-run); keep prompts auto-filled

# =============================== helper-level tests ===============================
for s in ["INBOX", "Café", "Révue & co", "日本語", "a&b", "Sent Items", "😀 fun"]:
    check(f"utf7 round trip {s!r}", M.imap_utf7_decode(M.imap_utf7_encode(s)) == s)
check("utf7 encode é / &", M.imap_utf7_encode("Café") == "Caf&AOk-" and M.imap_utf7_encode("a&b") == "a&-b")
check("quote_mailbox", M.quote_mailbox('a "b" \\c') == '"a \\"b\\" \\\\c"')
fl = M.parse_list_response(FOLDERS + [(b'(\\HasNoChildren) "/" {7}', b'My Mail'), b')', b'(\\Noselect) NIL "Odd"'])
check("parse LIST: quoted, atom, literal, NIL delimiter, utf7",
      [f.name for f in fl] == ["INBOX","IMAPorganizer","Sent Items","Café","Archive","Drafts","My Mail","Odd"], [f.name for f in fl])
check("Noselect flag respected", not M.is_selectable(M.find_folder(fl, "Archive")))
check("INBOX case-insensitive, others case-sensitive", M.find_folder(fl, "inbox").name == "INBOX" and M.find_folder(fl, "imaporganizer") is None)
check("NAMESPACE parse", M.NAMESPACE_PATTERN.match(b'(("INBOX." ".")) NIL NIL').group(1) == b"INBOX."
      and M.NAMESPACE_PATTERN.match(b'(("" "/")) NIL NIL').group(1) == b""
      and M.NAMESPACE_PATTERN.match(b'(("INBOX." "." "X-FOO" ("a" "b"))) NIL NIL').group(1) == b"INBOX.")
check("full_folder_name", M.full_folder_name("IMAPorganizer", "INBOX.") == "INBOX.IMAPorganizer"
      and M.full_folder_name("INBOX.X", "INBOX.") == "INBOX.X" and M.full_folder_name("inbox", "INBOX.") == "inbox" and M.full_folder_name("X", "") == "X")

PAT = ["newsletter@somestore.com", "*@marketing.example.org", "*@bigretailer.com"]
check("match: exact, case-insensitive", M.match_sender(["Newsletter@SomeStore.COM"], PAT) == "newsletter@somestore.com")
check("match: same domain, other address is NOT an exact match", M.match_sender(["other@somestore.com"], PAT) == "")
check("match: *@domain (mixed case)", M.match_sender(["Deals@BigRetailer.COM"], PAT) == "*@bigretailer.com")
check("match: subdomain / lookalikes do NOT match", M.match_sender(["x@mail.bigretailer.com"], PAT) == ""
      and M.match_sender(["x@notbigretailer.com"], PAT) == "" and M.match_sender(["x@bigretailer.com.evil.net"], PAT) == "")
check("match: none / no @ / any-of-several", M.match_sender([], PAT) == "" and M.match_sender(["nodomain"], PAT) == ""
      and M.match_sender(["a@gmail.com", "b@bigretailer.com"], PAT) == "*@bigretailer.com")

def pf(data): return [(u, r) for u, r, _ in M.parse_fetch_response(data)]
check("fetch parse: metadata before header text", pf([(b"1 (UID 11 FLAGS () {5}", b"hdr1\n"), b")", (b"2 (UID 12 FLAGS () {5}", b"hdr2\n"), b")"]) == [(11, b"hdr1\n"), (12, b"hdr2\n")])
check("fetch parse: metadata after header text", pf([(b"1 (BODY[] {5}", b"hdr1\n"), b" UID 11 FLAGS ())", (b"2 (BODY[] {5}", b"hdr2\n"), b" UID 12 FLAGS ())"]) == [(11, b"hdr1\n"), (12, b"hdr2\n")])
check("fetch parse: reply without header text", pf([b"3 (UID 13 FLAGS (\\Seen))"]) == [(13, b"")])
check("fetch parse: junk with no UID dropped", pf([(b"1 (BODY[] {5}", b"hdr1\n"), b")"]) == [])

# ---- flags / received date parsing
def pm(meta): return M.parse_message_headers(1, hdr("a@b.com", "s", "Mon, 26 Oct 2026 10:00:00 -0400"), meta)
check("flags: \\Flagged detected (case-insensitive, among others)", pm(b'UID 1 FLAGS (\\Seen \\FLAGGED $Forwarded)').flagged)
check("flags: \\Seen only / empty / $Flagged keyword are NOT flagged", not pm(b'UID 1 FLAGS (\\Seen)').flagged and not pm(b'UID 1 FLAGS ()').flagged and not pm(b'UID 1 FLAGS ($Flagged)').flagged)
check("received date parsed from INTERNALDATE", pm(b'UID 1 INTERNALDATE "05-Oct-2026 12:00:00 +0000"').received == datetime.date(2026, 10, 5))
check("received date: space-padded day", pm(b'UID 1 INTERNALDATE " 5-Oct-2026 12:00:00 +0000"').received == datetime.date(2026, 10, 5))
check("received date missing -> None", pm(b'UID 1 FLAGS ()').received is None)

# ---- cutoff parsing (today = 2026-11-04)
D = datetime.date
R = lambda s, d=TODAY: M.resolve_keep_recent(s, d)
check("keep: now -> None", R("now") is None and R(" NOW ") is None)
check("keep: 30d / 2w / 3m / 1y", R("30d") == D(2026,10,5) and R("2w") == D(2026,10,21) and R("3m") == D(2026,8,4) and R("1y") == D(2025,11,4))
check("keep: spelled out / spaces / case", R("2 weeks") == D(2026,10,21) and R("3 months") == D(2026,8,4) and R("1 year") == D(2025,11,4) and R("30 days") == D(2026,10,5) and R("3M") == D(2026,8,4))
check("keep: 0d = today (today's mail stays)", R("0d") == TODAY)
check("keep: month-end clamping", R("1m", D(2026,3,31)) == D(2026,2,28) and R("1y", D(2028,2,29)) == D(2027,2,28) and R("12m", D(2028,2,29)) == D(2027,2,28))
check("keep: across year boundary", R("2m", D(2026,1,15)) == D(2025,11,15))
check("keep: ISO date", R("2026-08-04") == D(2026,8,4) and R("2026-11-04") == TODAY)
def bad(s, expect):
    try: R(s); return False
    except ValueError as e: return expect in str(e)
check("keep: invalid ISO date rejected", bad("2026-02-30", "not a valid date"))
check("keep: future date rejected", bad("2026-11-05", "in the future"))
check("keep: other date formats rejected (ISO only)", bad("11/4/2026", "not understood") and bad("4-11-2026", "not understood") and bad("20261104", "not understood"))
check("keep: garbage / empty / negative rejected", bad("soon", "not understood") and bad("3x", "not understood") and bad("-2w", "not understood") and bad("", "no value"))
check("keep: absurdly large rejected (no crash)", bad("99999999y", "too far") and bad("999999999999d", "too far"))
check("describe_cutoff", "no age limit" in M.describe_cutoff(None) and "on or after 2026-08-04" in M.describe_cutoff(D(2026,8,4)))

# ---- config validation
def load_ini(text):
    Path("tmp.ini").write_text(text)
    with mock.patch.object(M, "today", lambda: TODAY):
        r = M.load_config(Path("tmp.ini"))
    Path("tmp.ini").unlink()
    return r
acct = "[a@example.com]\nserver = x\nsenders =\n    z@example.com\n"
s, _, e = load_ini("[global]\nkeep_recent = 3m\nkeep_flagged = no\n" + acct)
check("config: keep_recent / keep_flagged read", not e and s.keep_recent == "3m" and s.keep_flagged is False, e)
s, _, e = load_ini(acct)
check("config: built-in defaults are 'now' and keep flagged = yes", not e and s.keep_recent == "now" and s.keep_flagged is True, e)
s, _, e = load_ini("[global]\nkeep_recent = soon\nkeep_flagged = maybe\n" + acct)
check("config: bad values reported", any("keep_recent: 'soon' is not understood" in x for x in e) and any("keep_flagged must be yes or no" in x for x in e), e)
s, _, e = load_ini("[global]\nkeep_recent = 2027-01-01\n" + acct)
check("config: future date rejected in the config too", any("in the future" in x for x in e), e)

# ---- command-line switches
def parse(argv):
    err = io.StringIO()
    with mock.patch.object(sys, "argv", ["x"] + argv), mock.patch.object(M, "today", lambda: TODAY), contextlib.redirect_stderr(err):
        try: return M.parse_args(), 0, err.getvalue()
        except SystemExit as e: return None, e.code, err.getvalue()
a, c, _ = parse(["--keep", "3m", "--move-flagged", "--live"])
check("cli: --keep 3m --move-flagged --live accepted", c == 0 and a.keep == "3m" and a.move_flagged and a.live and not a.keep_flagged)
a, c, _ = parse([])
check("cli: no switches -> nothing preset", a.keep is None and not a.keep_flagged and not a.move_flagged and not a.live and not a.dry_run)
a, c, err = parse(["--keep", "3x"])
check("cli: bad --keep refused with a clear message", c == 2 and "--keep" in err and "not understood" in err, err)
a, c, err = parse(["--keep-flagged", "--move-flagged"])
check("cli: --keep-flagged with --move-flagged refused", c == 2 and "not allowed with" in err, err)
a, c, err = parse(["--live", "--dry-run"])
check("cli: --live with --dry-run refused", c == 2 and "not allowed with" in err, err)

# ====================== folder / connection scenarios (flat server) ======================
code, screen, log, conn = run([], inp)
check("A. all folders exist, dry-run: exit 0", code == 0, code)
check("A. counts shown", "Source folder INBOX: 42 messages" in screen and "Review folder IMAPorganizer: exists (42 messages)" in screen, screen)
check("A. TLS account: IMAP4_SSL, no starttls; logged out; nothing created",
      conn.port == 993 and not conn.starttls_called and ("logout",) in conn.calls and not any(c[0] == "create" for c in conn.calls))
check("A. password not in screen/log", "TOPSECRET123" not in screen + log)

code, screen, log, conn = run([], inp, folders=FOLDERS_NO_REVIEW)
check("B. review missing + dry-run: exit 0, says so, no create", code == 0 and "does not exist yet" in screen and not any(c[0] == "create" for c in conn.calls), screen)
code, screen, log, conn = run(["--live"], ["1", "n"], folders=FOLDERS_NO_REVIEW)
check("C. review missing + live + 'n': stops, exit 1, no create, logged out",
      code == 1 and "Stopped" in screen and not any(c[0] == "create" for c in conn.calls) and ("logout",) in conn.calls, (code, screen))
code, screen, log, conn = run(["--live"], ["1", ""], folders=FOLDERS_NO_REVIEW)
check("C2. live + Enter at create prompt = stop (default)", code == 1 and not any(c[0] == "create" for c in conn.calls))
code, screen, log, conn = run(["--live"], ["1", "y"], folders=FOLDERS_NO_REVIEW)
check("D. live + 'y': creates and subscribes, exit 0",
      code == 0 and ("create", '"IMAPorganizer"') in conn.calls and ("subscribe", '"IMAPorganizer"') in conn.calls, (code, conn.calls))
code, screen, log, conn = run(["--live"], ["1", "y"], folders=FOLDERS_NO_REVIEW, create_status="NO")
check("D2. server refuses create: error + prefix hint, exit 1", code == 1 and "INBOX.IMAPorganizer" in screen, screen)
code, screen, log, conn = run(["--live"], ["1"], folders=FOLDERS_NO_REVIEW)
check("K. Ctrl-D at create prompt: exit 130, logged out", code == 130 and ("logout",) in conn.calls, (code, conn.calls))

ini_replace("[you@example.com]\nserver = imap.example.com", "[you@example.com]\nserver = imap.example.com\nsource_folder = NoSuchFolder")
code, screen, log, conn = run([], inp)
check("E. source missing: error lists available (selectable) folders, exit 1",
      code == 1 and "'NoSuchFolder' was not found" in screen and "Sent Items" in screen and "Archive" not in screen.split("Folders on the server:")[1], screen)
restore_ini()

code, screen, log, conn = run([], inp, login_error=b"[AUTHENTICATIONFAILED] Authentication failed.")
check("F. login failure: friendly, exit 1, password hidden",
      code == 1 and "Login failed for you@example.com: [AUTHENTICATIONFAILED]" in screen and "TOPSECRET123" not in screen + log, screen)
for label, exc, expect in [
    ("G1 DNS", socket.gaierror(-2, "Name or service not known"), "DNS lookup failed"),
    ("G2 refused", ConnectionRefusedError(), "Connection refused"),
    ("G3 timeout", TimeoutError(), "Timed out"),
    ("G4 cert", ssl.SSLCertVerificationError(1, "certificate verify failed: Hostname mismatch"), "must match the name on the server's certificate"),
    ("G5 TLS", ssl.SSLError(1, "[SSL] wrong version number"), "Port 993 expects TLS"),
    ("G6 other", OSError("Network is unreachable"), "Network is unreachable"),
]:
    code, screen, log, conn = run([], inp, connect_error=exc)
    check(f"{label}: friendly message, exit 1", code == 1 and expect in screen and "Traceback" not in screen, screen)

code, screen, log, conn = run([], ["2", ""])
check("H. port-143 account uses STARTTLS, its own username and folders",
      code == 0 and conn.starttls_called and conn.port == 143 and ("login", "otherperson") in conn.calls and "Review folder ToReview" in screen, (code, screen))
ini_replace("review_folder = ToReview", "review_folder = Révue & co")
code, screen, log, conn = run(["--live"], ["2", "y"], folders=FOLDERS_NO_REVIEW)
check("J. non-ASCII/space folder name is UTF-7 encoded and quoted", ("create", '"R&AOk-vue &- co"') in conn.calls, conn.calls)
ini_replace("review_folder = ToReview", "review_folder = INBOX")
code, screen, log, conn = run([], ["1", ""])
check("L. source == review rejected before any prompt/connection", code == 1 and conn is None)
restore_ini()

# ============================ prefixed-namespace server ============================
code, screen, log, conn = run([], inp, folders=PREFIXED, **PFX)
check("P1. existing INBOX.IMAPorganizer FOUND from plain config name",
      code == 0 and "Review folder INBOX.IMAPorganizer: exists (42 messages)" in screen and "Source folder INBOX: 42 messages" in screen, screen)
code, screen, log, conn = run(["--live"], ["1", "y"], folders=PREFIXED_NO_REVIEW, **PFX)
check("P2. live + yes: creates INBOX.IMAPorganizer and subscribes, no errors",
      code == 0 and ("create", '"INBOX.IMAPorganizer"') in conn.calls and ("subscribe", '"INBOX.IMAPorganizer"') in conn.calls and "ERROR" not in screen, (code, screen))
code, screen, log, conn = run([], inp, folders=PREFIXED_NO_REVIEW, **PFX)
check("P3. dry-run missing: reports full name, creates nothing",
      code == 0 and "Review folder INBOX.IMAPorganizer does not exist yet" in screen and not any(c[0] == "create" for c in conn.calls), screen)
code, screen, log, conn = run(["--live"], ["1", "y"], folders=PREFIXED_NO_REVIEW, namespace_supported=False, require_prefix="INBOX.")
check("P4. no NAMESPACE support: prefix inferred from folder list", code == 0 and ("create", '"INBOX.IMAPorganizer"') in conn.calls, (code, screen))
ini_replace("[you@example.com]\nserver = imap.example.com", "[you@example.com]\nserver = imap.example.com\nreview_folder = INBOX.IMAPorganizer")
code, screen, log, conn = run([], inp, folders=PREFIXED, **PFX)
check("P5. explicit INBOX.IMAPorganizer in config also works", code == 0 and "Review folder INBOX.IMAPorganizer: exists" in screen, screen)
ini_replace("[you@example.com]\nserver = imap.example.com", "[you@example.com]\nserver = imap.example.com\nsource_folder = Receipts")
code, screen, log, conn = run([], inp, folders=PREFIXED, **PFX)
check("P6. source_folder = Receipts resolves to INBOX.Receipts", code == 0 and "Source folder INBOX.Receipts: 42 messages" in screen, screen)
restore_ini()
code, screen, log, conn = run(["--live"], ["1", "y"], folders=FOLDERS_NO_REVIEW, require_prefix="INBOX.")
check("P7. unexplained namespace refusal: readable error + hint", code == 1 and "nonexistent namespace" in screen and "[b'" not in screen and "INBOX.IMAPorganizer" in screen, screen)

# ======================= reading messages (no keep rules active) =======================
MSGS = {
    1: hdr("Store News <Newsletter@SomeStore.com>", "Weekly deals", "Mon, 26 Oct 2026 10:00:00 -0400"),
    2: hdr('"Big, Retailer" <deals@BigRetailer.com>', "Sale!", "Tue, 27 Oct 2026 08:00:00 +0000"),
    3: hdr("x@mail.bigretailer.com", "subdomain, should NOT match", "Wed, 28 Oct 2026 08:00:00 +0000"),
    4: hdr("Friend <friend@gmail.com>", "Lunch?", "Thu, 29 Oct 2026 08:00:00 +0000"),
    5: hdr("=?utf-8?B?UHJvbW8gVGVhbQ==?= <promo@marketing.example.org>", "=?utf-8?q?Caf=C3=A9_deals?=", "Fri, 30 Oct 2026 08:00:00 +0000"),
    6: hdr(None, "No From header at all", "Sat, 31 Oct 2026 08:00:00 +0000"),
    7: hdr("this is not an address", "Garbage From", "not a date"),
    8: hdr("other@somestore.com", "Same domain, different address", "Sun, 01 Nov 2026 08:00:00 +0000"),
    9: hdr("a@gmail.com, b@bigretailer.com", "Two From addresses", "Mon, 02 Nov 2026 08:00:00 +0000"),
    10: hdr("Evil <evil@gmail.com>", "Line one\r\n\tfolded\x07 with bell", "Tue, 03 Nov 2026 08:00:00 +0000"),
}
code, screen, log, conn = run([], inp, messages=dict(MSGS))
check("M1. dry-run: exit 0, '4 of 10' match", code == 0 and "4 of 10 messages in INBOX match the sender list." in screen, screen)
check("M1. per-entry counts (matched / to move)",
      all(re.search(rf"^  {re.escape(p)}\s+{m} matched,\s+{t} to move$", screen, re.M) for p, m, t in
          [("newsletter@somestore.com", 1, 1), ("*@marketing.example.org", 1, 1), ("*@bigretailer.com", 2, 2)]), screen)
check("M1. breakdown + dry-run summary",
      "Of the 4 matches: 4 to move" in screen and "Dry run: 4 messages would be moved from INBOX to IMAPorganizer. Nothing was changed." in screen, screen)
check("M1. exactly uids 1,2,5,9 'Would move'", all(f"Would move: [uid {u}]" in screen for u in (1,2,5,9)) and not any(f"Would move: [uid {u}]" in screen for u in (3,4,6,7,8,10)))
check("M1. RFC2047 subject decoded; display name with comma intact; control chars cleaned",
      "Café deals" in screen and "Big, Retailer" in screen and "\x07" not in screen + log and "Traceback" not in screen)
selects = [c for c in conn.calls if c[0] == "select"]
fetches = [c for c in conn.calls if c[0] == "uid" and c[1] == "FETCH"]
check("M2. folder opened READ-ONLY", selects and all(c[2] is True for c in selects), selects)
check("M2. fetched with BODY.PEEK (never marks messages read)", fetches and all("BODY.PEEK" in c[2][1] and "FLAGS INTERNALDATE" in c[2][1] for c in fetches), fetches)
check("M2. only SEARCH and FETCH used (no STORE/COPY/MOVE/EXPUNGE possible)", {c[1] for c in conn.calls if c[0] == "uid"} <= {"SEARCH", "FETCH"})
check("M2. password not in output", "TOPSECRET123" not in screen + log)
code, screen, log, conn = run([], inp, messages=dict(MSGS), uid_after_literal=True)
check("M3. metadata-after-header reply style gives the same result", code == 0 and "4 of 10 messages" in screen, screen)
big = {u: hdr(f"user{u}@gmail.com", f"msg {u}", "Mon, 26 Oct 2026 10:00:00 -0400") for u in range(1, 601)}
big[300] = hdr("deals@bigretailer.com", "needle", "Mon, 26 Oct 2026 10:00:00 -0400")
code, screen, log, conn = run([], inp, messages=big)
n_fetch = len([c for c in conn.calls if c[0] == "uid" and c[1] == "FETCH"])
check("M4. 600 messages fetched in 3 batches", n_fetch == 3 and "1 of 600 messages" in screen, (n_fetch, screen))
code, screen, log, conn = run([], inp, messages={})
check("M5. empty folder: '0 of 0'", code == 0 and "0 of 0 messages" in screen, screen)
code, screen, log, conn = run(["--live"], ["1", "n"], messages=dict(MSGS))
check("M6. live run: reports matches, asks to confirm; 'n' cancels and moves nothing",
      code == 0 and "Matched: [uid 1]" in screen and "Cancelled: nothing was moved." in screen and FakeIMAP.review == [] and len(FakeIMAP.messages) == 10, screen)
code, screen, log, conn = run([], inp, messages=dict(MSGS), fetch_status="NO")
check("M7. FETCH refused: friendly error, exit 1, logged out", code == 1 and "Could not read messages" in screen and ("logout",) in conn.calls, screen)
code, screen, log, conn = run([], inp, messages=dict(MSGS), fetch_raises=imaplib.IMAP4.abort("socket error: EOF"))
check("M8. connection dropped mid-fetch: friendly error, exit 1", code == 1 and "Problem reading messages" in screen and "Traceback" not in screen, screen)
code, screen, log, conn = run([], inp, messages=dict(MSGS), fetch_raises=KeyboardInterrupt())
check("M9. Ctrl-C during fetch: exit 130, logged out", code == 130 and ("logout",) in conn.calls, (code, conn.calls))
code, screen, log, conn = run([], inp, folders=PREFIXED, messages=dict(MSGS), **PFX)
check("M10. prefixed server end-to-end", code == 0 and "4 of 10 messages in INBOX" in screen and "to INBOX.IMAPorganizer." in screen, screen)
ini_replace("screen_output = verbose", "screen_output = summary")
code, screen, log, conn = run([], inp, messages=dict(MSGS))
check("M11. summary screen: counts only, no per-message lines (but they are in the log file)",
      "4 of 10 messages" in screen and "Would move: [uid" not in screen and "Would move: [uid 1]" in log, screen)
restore_ini()

# ================= keep-recent age rule and keep-flagged rule =================
# today = 2026-11-04.  Sender list: newsletter@somestore.com, *@marketing.example.org, *@bigretailer.com
N, B, MK = "newsletter@somestore.com", "deals@bigretailer.com", "promo@marketing.example.org"
def stamp(y, m, d): return f"{d:02d}-{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][m-1]}-{y} 12:00:00 +0000"
KEEP_MSGS = {
    1: hdr(N,  "old",                       "Tue, 01 Sep 2026 10:00:00 +0000"),
    2: hdr(B,  "recent",                    "Tue, 20 Oct 2026 10:00:00 +0000"),
    3: hdr(MK, "old but flagged",           "Sat, 15 Aug 2026 10:00:00 +0000"),
    4: hdr(B,  "received ON the cutoff day","Thu, 01 Oct 2026 10:00:00 +0000"),
    5: hdr(B,  "day BEFORE the cutoff",     "Wed, 30 Sep 2026 10:00:00 +0000"),
    6: hdr("friend@gmail.com", "unmatched but flagged", "Sat, 15 Aug 2026 10:00:00 +0000"),
    7: hdr(B,  "recent AND flagged",        "Sun, 25 Oct 2026 10:00:00 +0000"),
    8: hdr(N,  "Date header says NEW, received long ago",  "Sun, 01 Nov 2026 10:00:00 +0000"),
    9: hdr(N,  "Date header says OLD, received recently",  "Mon, 02 Mar 2020 10:00:00 +0000"),
}
KEEP_META = {
    1: ("\\Seen", stamp(2026, 9, 1)),
    2: ("\\Seen", stamp(2026, 10, 20)),
    3: ("\\Seen \\Flagged", stamp(2026, 8, 15)),
    4: ("", stamp(2026, 10, 1)),
    5: ("", stamp(2026, 9, 30)),
    6: ("\\Flagged", stamp(2026, 8, 15)),
    7: ("\\Flagged", stamp(2026, 10, 25)),
    8: ("", stamp(2025, 1, 1)),
    9: ("", stamp(2026, 10, 30)),
}
def keep_run(argv, inputs=("1", ""), **kw):
    return run(argv, list(inputs), messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META), **kw)
moved = lambda screen: sorted(int(u) for u in re.findall(r"Would move: \[uid (\d+)\]", screen))
kept = lambda screen, why: sorted(int(u) for u in re.findall(rf"Keeping \({re.escape(why)}\): \[uid (\d+)\]", screen))

# --- age rule + keep flagged (default yes), cutoff 2026-10-01
code, screen, log, conn = keep_run(["--keep", "2026-10-01"])
check("K1. exit 0; 8 sender matches (unmatched flagged message ignored)", code == 0 and "8 of 9 messages in INBOX match the sender list." in screen, screen)
check("K1. moved = old, unflagged, received before cutoff (uids 1, 5, 8)", moved(screen) == [1, 5, 8], moved(screen))
check("K1. kept (flagged) = 3, 7 -- flagged wins even when also recent", kept(screen, "flagged") == [3, 7], kept(screen, "flagged"))
check("K1. kept (newer) = 2, 4, 9 -- includes message received ON the cutoff day", kept(screen, "received on or after 2026-10-01") == [2, 4, 9], screen)
check("K1. received date, NOT Date header, drives the age rule (uids 8, 9)", 8 in moved(screen) and 9 in kept(screen, "received on or after 2026-10-01"))
check("K1. breakdown adds up: 3 + 2 + 3 = 8", "Of the 8 matches: 3 to move, 2 kept (flagged), 3 kept (received on or after 2026-10-01)." in screen, screen)
check("K1. per-entry lines", re.search(r"^  newsletter@somestore.com\s+3 matched,\s+2 to move$", screen, re.M)
      and re.search(r"^  \*@bigretailer.com\s+4 matched,\s+1 to move$", screen, re.M)
      and re.search(r"^  \*@marketing.example.org\s+1 matched,\s+0 to move$", screen, re.M), screen)
check("K1. dry-run line counts only what would move", "Dry run: 3 messages would be moved from INBOX to IMAPorganizer. Nothing was changed." in screen)
check("K1. run header states both rules",
      "Age rule: messages received on or after 2026-10-01 stay in place; only older matches may be moved" in screen
      and "Flagged (starred) messages: kept in place" in screen, screen)
check("K1. still strictly read-only", {c[1] for c in conn.calls if c[0] == "uid"} <= {"SEARCH", "FETCH"} and all(c[2] is True for c in conn.calls if c[0] == "select"))

# --- flagged may be moved
code, screen, log, conn = keep_run(["--keep", "2026-10-01", "--move-flagged"])
check("K2. --move-flagged: old flagged uid 3 now moves; recent flagged uid 7 kept as 'newer'",
      moved(screen) == [1, 3, 5, 8] and kept(screen, "received on or after 2026-10-01") == [2, 4, 7, 9] and not kept(screen, "flagged"), (moved(screen), screen))
check("K2. breakdown omits the flagged part", "Of the 8 matches: 4 to move, 4 kept (received on or after 2026-10-01)." in screen and "Flagged (starred) messages: may be moved" in screen, screen)

# --- now = no age limit, flagged kept
code, screen, log, conn = keep_run(["--keep", "now", "--keep-flagged"])
check("K3. --keep now: only flagged are kept", moved(screen) == [1, 2, 4, 5, 8, 9] and kept(screen, "flagged") == [3, 7], (moved(screen), screen))
check("K3. breakdown + header", "Of the 8 matches: 6 to move, 2 kept (flagged)." in screen and "Age rule: no age limit: every match is eligible to be moved" in screen, screen)

# --- no limits at all
code, screen, log, conn = keep_run(["--keep", "now", "--move-flagged"])
check("K4. now + move flagged: every match moves", moved(screen) == [1, 2, 3, 4, 5, 7, 8, 9] and "Of the 8 matches: 8 to move." in screen, screen)

# --- relative ages resolve against today (2026-11-04)
code, screen, log, conn = keep_run(["--keep", "3m"])
check("K5. --keep 3m -> cutoff 2026-08-04 shown", "messages received on or after 2026-08-04" in screen, screen)
code, screen, log, conn = keep_run(["--keep", "1 year"])
check("K5. --keep '1 year' -> cutoff 2025-11-04; uid 8 (received 2025-01-01) is the only mover", "2025-11-04" in screen and moved(screen) == [8], (moved(screen), screen))

# --- received date unknown -> safe: kept when an age limit is set
meta_nodate = dict(KEEP_META); meta_nodate[1] = ("\\Seen", None)
code, screen, log, conn = run(["--keep", "2026-10-01"], ["1", ""], messages=dict(KEEP_MSGS), msg_meta=meta_nodate)
check("K6. unreadable received date + age limit: message is kept, and counted", 1 in kept(screen, "received date unknown") and 1 not in moved(screen) and "1 kept (received date unknown)" in screen, screen)
code, screen, log, conn = run(["--keep", "now"], ["1", ""], messages=dict(KEEP_MSGS), msg_meta=meta_nodate)
check("K6. ...but with no age limit it moves normally", 1 in moved(screen), screen)

# --- uid-after-literal reply style carries flags/dates correctly
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], uid_after_literal=True)
check("K7. alternate reply style: same decisions", moved(screen) == [1, 5, 8] and kept(screen, "flagged") == [3, 7], screen)

# --- the prompts (nothing on the command line)
raw = ["1", "soon", "11/4/2026", "2027-01-01", "3m", "", ""]       # account, 4 keep answers, flagged Enter, mode Enter
code, screen, log, conn = run([], raw, auto=False, messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META))
check("K8. keep prompt re-asks on bad input, with a reason each time",
      screen.count("Keep messages newer than") == 4 and "'soon' is not understood" in screen and "'11/4/2026' is not understood" in screen and "2027-01-01 is in the future" in screen, screen)
check("K8. then accepts 3m; Enter on flagged = keep (default yes); Enter on mode = dry-run",
      "messages received on or after 2026-08-04" in screen and "Flagged (starred) messages: kept in place" in screen and "DRY-RUN" in screen and code == 0, screen)
check("K8. flagged prompt shows [Y/n]", "Keep flagged (starred) messages in place? [Y/n]" in screen, screen)

ini_replace("keep_recent = now", "keep_recent = 2w")
ini_replace_text = INI.read_text().replace("keep_flagged = yes", "keep_flagged = no")
INI.write_text(ini_replace_text)
code, screen, log, conn = run([], ["1", "", "", ""], auto=False, messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META))
check("K9. config defaults become the prompt defaults (Enter = 2w; flagged [y/N] -> may be moved)",
      "Enter = 2w" in screen and "messages received on or after 2026-10-21" in screen and "Keep flagged (starred) messages in place? [y/N]" in screen and "Flagged (starred) messages: may be moved" in screen, screen)
code, screen, log, conn = run([], ["1", "", "y", ""], auto=False, messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META))
check("K9. answering y overrides a 'no' default", "Flagged (starred) messages: kept in place" in screen, screen)
code, screen, log, conn = run(["--keep", "now", "--keep-flagged"], ["1", ""], auto=False, messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META))
check("K9. switches override config and skip both prompts", "Keep messages newer than" not in screen and "Keep flagged" not in screen and "Age rule: no age limit" in screen, screen)
restore_ini()

# --- Ctrl-D at the new prompts
code, screen, log, conn = run([], ["1"], auto=False)
check("K10. Ctrl-D at the keep-recent prompt: exit 130, nothing connected", code == 130 and conn is None, (code, screen))
code, screen, log, conn = run([], ["1", ""], auto=False)
check("K10. Ctrl-D at the flagged prompt: exit 130, nothing connected", code == 130 and conn is None, (code, screen))
check("K10. password never printed in any run", "TOPSECRET123" not in screen + log)

# --- log file carries the rules too
code, screen, log, conn = keep_run(["--keep", "2026-10-01"])
check("K11. log file records the rules and each kept message with its reason",
      "Age rule: messages received on or after 2026-10-01" in log and "Keeping (flagged): [uid 3]" in log and "TOPSECRET123" not in log, log[-600:])


# ================= per-account overrides of keep_recent / keep_flagged =================
G = "[global]\nkeep_recent = 1y\nkeep_flagged = yes\n"
def acct_block(name, extra=""):
    return f"[{name}]\nserver = x\n{extra}senders =\n    z@example.com\n"

s, accts, e = load_ini(G + acct_block("a@example.com", "keep_recent = 3m\nkeep_flagged = no\n") + acct_block("b@example.com"))
check("OV1. account override read; other account inherits the [global] values",
      not e and accts[0].keep_recent == "3m" and accts[0].keep_flagged is False
      and accts[1].keep_recent == "1y" and accts[1].keep_flagged is True, e)
s, accts, e = load_ini(acct_block("a@example.com"))
check("OV2. with nothing set anywhere: built-in defaults (now / yes)", not e and accts[0].keep_recent == "now" and accts[0].keep_flagged is True, e)
s, accts, e = load_ini(G + acct_block("a@example.com", "keep_recent =\nkeep_flagged =\n"))
check("OV3. empty account values fall back to [global] (like source_folder)", not e and accts[0].keep_recent == "1y" and accts[0].keep_flagged is True, e)
s, accts, e = load_ini(G + acct_block("a@example.com", "keep_recent = soon\nkeep_flagged = maybe\n"))
check("OV4. bad account values are reported with the account named",
      any("[a@example.com] keep_recent: 'soon' is not understood" in x for x in e) and any("[a@example.com] keep_flagged must be yes or no" in x for x in e), e)
s, accts, e = load_ini(G + acct_block("a@example.com", "keep_recent = 2027-01-01\n"))
check("OV5. future date rejected at account level too", any("[a@example.com] keep_recent: 2027-01-01 is in the future" in x for x in e), e)
s, accts, e = load_ini(G + acct_block("a@example.com", "keep_rcent = 3m\n"))
check("OV6. misspelled account setting is caught (no silent ignore)", any("unknown setting: keep_rcent" in x for x in e), e)

# --- end to end: which default does each prompt show, and what wins?
code, screen, log, conn = run([], ["2", "", "", ""], auto=False)          # account 2 has its own: 3m / no
check("OV7. account with overrides: prompts default to ITS values (Enter = 3m, [y/N])",
      "Enter = 3m" in screen and "Keep flagged (starred) messages in place? [y/N]" in screen, screen)
check("OV7. ...and pressing Enter applies them (cutoff 2026-08-04, flagged may be moved)",
      "messages received on or after 2026-08-04" in screen and "Flagged (starred) messages: may be moved" in screen and code == 0, screen)

code, screen, log, conn = run([], ["1", "", "", ""], auto=False)          # account 1 has none: global now / yes
check("OV8. account without overrides: prompts default to the [global] values (Enter = now, [Y/n])",
      "Enter = now" in screen and "[Y/n]" in screen and "Age rule: no age limit" in screen and "Flagged (starred) messages: kept in place" in screen, screen)

code, screen, log, conn = run([], ["2", "1w", "y", ""], auto=False)
check("OV9. what you TYPE beats the account default (1w -> 2026-10-28; y -> kept in place)",
      "messages received on or after 2026-10-28" in screen and "Flagged (starred) messages: kept in place" in screen, screen)

code, screen, log, conn = run(["--keep", "now", "--keep-flagged"], ["2", ""], auto=False)
check("OV10. command-line switches beat the account's own settings, and skip both prompts",
      "Age rule: no age limit" in screen and "Flagged (starred) messages: kept in place" in screen
      and "Keep messages newer than" not in screen and "Keep flagged" not in screen, screen)

# global changed, account override still wins; the OTHER account follows the global
ini_replace("keep_recent = now", "keep_recent = 1y")
code, screen, log, conn = run([], ["2", "", "", ""], auto=False)
check("OV11. [global] changed to 1y: account 2 keeps ITS own 3m", "Enter = 3m" in screen and "2026-08-04" in screen, screen)
code, screen, log, conn = run([], ["1", "", "", ""], auto=False)
check("OV11. ...while account 1 follows the new global 1y", "Enter = 1y" in screen and "2025-11-04" in screen, screen)
restore_ini()

# an override on the FIRST account, as a real user would write it
ini_replace("[you@example.com]\nserver = imap.example.com", "[you@example.com]\nserver = imap.example.com\nkeep_recent = 2026-10-01\nkeep_flagged = yes")
code, screen, log, conn = run([], ["1", "", "", ""], auto=False, messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META))
check("OV12. exact-date override on an account: used as the default and drives the result (uids 1, 5, 8 move)",
      "Enter = 2026-10-01" in screen and moved(screen) == [1, 5, 8], (moved(screen), screen))
restore_ini()

# bad override in the .ini stops the run before any prompt
ini_replace("[you@example.com]\nserver = imap.example.com", "[you@example.com]\nserver = imap.example.com\nkeep_recent = nonsense")
code, screen, log, conn = run([], ["1", ""], auto=False)
check("OV13. bad account override: refused before any prompt or connection", code == 1 and conn is None, (code, screen))
restore_ini()

# debug log records where the defaults came from
ini_replace("log_level = INFO", "log_level = DEBUG")
code, screen, log, conn = run([], ["2", "", "", ""], auto=False)
check("OV14. DEBUG log shows account default vs global", "Keep-recent default: 3m (global: now)" in log and "Keep-flagged default: no (global: yes)" in log, log[-500:])
restore_ini()


# ============================ MOVING MESSAGES (live runs) ============================
# Scenario: --keep 2026-10-01 over KEEP_MSGS  ->  uids 1, 5, 8 are the ones to move.
def mv(answers=("y",), extra=(), msgs=None, **kw):
    return run(["--live", "--keep", "2026-10-01", *extra], ["1", *answers],
               messages=dict(KEEP_MSGS if msgs is None else msgs), msg_meta=dict(KEEP_META), **kw)
cmds = lambda conn: [c[1] for c in conn.calls if c[0] == "uid"]
rw_selects = lambda conn: [c for c in conn.calls if c[0] == "select" and c[2] is False]
check("MV0. MOVE command is registered with imaplib", "MOVE" in imaplib.Commands)

# ---- happy path, MOVE capability
code, screen, log, conn = mv()
check("MV1. exit 0, 'Moved 3 messages from INBOX to IMAPorganizer.'", code == 0 and "Moved 3 messages from INBOX to IMAPorganizer." in screen, screen)
check("MV1. exactly uids 1,5,8 were moved; the other six messages are untouched",
      sorted(FakeIMAP.review) == [1, 5, 8] and sorted(FakeIMAP.messages) == [2, 3, 4, 6, 7, 9], (FakeIMAP.review, sorted(FakeIMAP.messages)))
mvcalls = [c for c in conn.calls if c[0] == "uid" and c[1] == "MOVE"]
check("MV1. one MOVE to the quoted review folder", len(mvcalls) == 1 and mvcalls[0][2] == ("1,5,8", '"IMAPorganizer"'), mvcalls)
check("MV1. no COPY/STORE/EXPUNGE needed when MOVE exists", set(cmds(conn)) <= {"SEARCH", "FETCH", "MOVE"}, cmds(conn))
check("MV1. every move is logged, one line per message", all(f"Moved: [uid {u}]" in log for u in (1, 5, 8)) and "Moved: [uid 2]" not in log, log[-900:])
check("MV1. password not in screen/log", "TOPSECRET123" not in screen + log)
order = [(c[0], c[1] if c[0] == "uid" else c[-1] if c[0] == "select" else "") for c in conn.calls if c[0] in ("select", "uid")]
check("MV1. order: read-only select -> SEARCH -> FETCH -> read-write select -> FETCH (re-check) -> MOVE",
      order == [("select", True), ("uid", "SEARCH"), ("uid", "FETCH"), ("select", False), ("uid", "FETCH"), ("uid", "MOVE")], order)
check("MV1. logged out", ("logout",) in conn.calls)

# ---- confirmation prompt
code, screen, log, conn = mv(("n",))
check("MV2. 'n' at the prompt: nothing moved, no read-write open, exit 0, says cancelled",
      code == 0 and FakeIMAP.review == [] and not rw_selects(conn) and "Cancelled: nothing was moved." in screen, screen)
check("MV2. prompt text names the count and both folders", "Move 3 messages from INBOX to IMAPorganizer? [y/N]" in screen, screen)
code, screen, log, conn = mv(("",))
check("MV2. Enter at the prompt = No (the default)", code == 0 and FakeIMAP.review == [], screen)
code, screen, log, conn = mv(("maybe", "y"))
check("MV2. invalid answer re-asks", "Please answer y or n." in screen and sorted(FakeIMAP.review) == [1, 5, 8], screen)
code, screen, log, conn = mv((), extra=["--yes"])
check("MV3. --yes skips the prompt and moves", "Move 3 messages" not in screen and sorted(FakeIMAP.review) == [1, 5, 8] and code == 0, screen)
a = parse(["--yes", "--live"])[0]
check("MV3. --yes is accepted by the argument parser", a is not None and a.yes)

# ---- dry run never moves and never opens read-write
code, screen, log, conn = run(["--keep", "2026-10-01"], ["1", ""], messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META))
check("MV4. dry run: no read-write select, no MOVE/COPY/STORE/EXPUNGE, nothing moved",
      not rw_selects(conn) and set(cmds(conn)) <= {"SEARCH", "FETCH"} and FakeIMAP.review == [] and len(FakeIMAP.messages) == 9, cmds(conn))
check("MV4. dry run says how a live run would move", "would move messages with the server's MOVE command" in screen, screen)
code, screen, log, conn = run(["--keep", "2026-10-01", "--yes"], ["1", ""], messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META))
check("MV4. --yes with a dry run still moves nothing", FakeIMAP.review == [] and not rw_selects(conn))

# ---- nothing to move
code, screen, log, conn = run(["--live", "--keep", "2025-01-01"], ["1"], messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META))
check("MV5. nothing eligible: no prompt, no read-write open, 'Nothing to move.'",
      code == 0 and "Nothing to move." in screen and "Move " not in screen.replace("Moving", "") and not rw_selects(conn) and FakeIMAP.review == [], screen)

# ---- changes between the scan and the move (the confirmation pause is a race window)
def flag_uid1_and_delete_uid5():
    FakeIMAP.msg_meta[1] = ("\\Seen \\Flagged", stamp(2026, 9, 1))     # user stars message 1
    del FakeIMAP.messages[5]                                           # message 5 deleted elsewhere
code, screen, log, conn = mv(hook_rw_select=flag_uid1_and_delete_uid5)
check("MV6. flagged-since-scan (1) and vanished (5) are left alone; only 8 moves",
      sorted(FakeIMAP.review) == [8] and "2 messages changed on the server since the scan" in screen and "Moved 1 message from INBOX" in screen, (FakeIMAP.review, screen))
check("MV6. skipped messages are logged", "Skipped (changed since the scan): [uid 1]" in log and "Skipped (changed since the scan): [uid 5]" in log, log[-700:])
check("MV6. the MOVE only named uid 8", [c[2][0] for c in conn.calls if c[0] == "uid" and c[1] == "MOVE"] == ["8"], conn.calls)
def delete_all(): FakeIMAP.messages.clear()
code, screen, log, conn = mv(hook_rw_select=delete_all)
check("MV6. everything gone before the move: 'Nothing left to move.', exit 0", code == 0 and "Nothing left to move." in screen and "MOVE" not in cmds(conn), screen)

# ---- UIDVALIDITY
code, screen, log, conn = mv(uidvalidity_values=[1001, 2002])
check("MV7. UIDVALIDITY changed between scan and move: refuses, moves nothing, exit 1",
      code == 1 and FakeIMAP.review == [] and "changed on the server since it was scanned" in screen and "MOVE" not in cmds(conn), screen)
code, screen, log, conn = mv(uidvalidity_values=[None])
check("MV7. server reports no UIDVALIDITY: refuses to move by UID", code == 1 and FakeIMAP.review == [] and "UIDVALIDITY" in screen, screen)

# ---- servers without MOVE: COPY + STORE + UID EXPUNGE (UIDPLUS required)
code, screen, log, conn = mv(capabilities=b"IMAP4rev1 UIDPLUS")
check("MV8. no MOVE but UIDPLUS: COPY, then STORE \\Deleted, then UID EXPUNGE of exactly the moved uids",
      code == 0 and cmds(conn)[-3:] == ["COPY", "STORE", "EXPUNGE"] and
      [c[2] for c in conn.calls if c[0] == "uid" and c[1] == "EXPUNGE"] == [("1,5,8",)], (code, cmds(conn), screen))
check("MV8. result is the same: 1,5,8 moved, others untouched", sorted(FakeIMAP.review) == [1, 5, 8] and sorted(FakeIMAP.messages) == [2, 3, 4, 6, 7, 9])
check("MV8. plain EXPUNGE is impossible (fake has no such method) and was never needed", not hasattr(FakeIMAP, "expunge"))
code, screen, log, conn = run(["--keep", "2026-10-01"], ["1", ""], messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META), capabilities=b"IMAP4rev1 UIDPLUS")
check("MV8. dry run reports the COPY method", "COPY + UID EXPUNGE" in screen, screen)

# a message ALREADY marked \Deleted by the user must survive (that's why plain EXPUNGE is never used)
meta = dict(KEEP_META); meta[4] = ("\\Deleted", stamp(2026, 10, 1))      # uid 4: newer message the user had marked deleted
code, screen, log, conn = run(["--live", "--keep", "2026-10-01"], ["1", "y"], messages=dict(KEEP_MSGS), msg_meta=meta, capabilities=b"IMAP4rev1 UIDPLUS")
check("MV9. a message the user had already marked \\Deleted is NOT expunged by us", 4 in FakeIMAP.messages and sorted(FakeIMAP.review) == [1, 5, 8], (sorted(FakeIMAP.messages), FakeIMAP.review))

# ---- neither MOVE nor UIDPLUS
code, screen, log, conn = mv(capabilities=b"IMAP4rev1")
check("MV10. neither MOVE nor UIDPLUS: refused before the prompt, nothing opened for writing or moved",
      code == 1 and "supports neither MOVE nor UIDPLUS" in screen and not rw_selects(conn) and FakeIMAP.review == [] and "Move 3 messages" not in screen, screen)
code, screen, log, conn = run(["--keep", "2026-10-01"], ["1", ""], messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META), capabilities=b"IMAP4rev1")
check("MV10. dry run warns that a live run could not move on this server", "A live run could NOT move messages on this server" in screen and code == 0, screen)
code, screen, log, conn = run(["--live", "--keep", "2025-01-01"], ["1"], messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META), capabilities=b"IMAP4rev1")
check("MV10. ...but if there is nothing to move it is not an error", code == 0 and "Nothing to move." in screen, screen)

# ---- server failures
code, screen, log, conn = mv(move_status="NO")
check("MV11. MOVE refused: clear error, 'Stopped after moving 0 of 3', exit 1, logged out",
      code == 1 and "Stopped after moving 0 of 3 messages" in screen and "[CANNOT] Move failed" in screen and ("logout",) in conn.calls and FakeIMAP.review == [], screen)
code, screen, log, conn = mv(capabilities=b"IMAP4rev1 UIDPLUS", copy_status="NO")
check("MV11. COPY refused: says nothing was deleted; originals intact", code == 1 and "nothing was deleted" in screen and len(FakeIMAP.messages) == 9, screen)
code, screen, log, conn = mv(capabilities=b"IMAP4rev1 UIDPLUS", store_status="NO")
check("MV11. STORE refused after COPY: says messages now exist in both folders (no loss)", code == 1 and "exist in both folders" in screen and len(FakeIMAP.messages) == 9, screen)
code, screen, log, conn = mv(capabilities=b"IMAP4rev1 UIDPLUS", expunge_status="NO")
check("MV11. EXPUNGE refused: says both folders hold them, and the \\Deleted flags were rolled back",
      code == 1 and "exist in both folders" in screen and any(c[0] == "uid" and c[1] == "STORE" and c[2][1] == "-FLAGS.SILENT" for c in conn.calls)
      and all("\\Deleted" not in FakeIMAP.msg_meta.get(u, DEFAULT_META)[0] for u in (1, 5, 8)), (screen, FakeIMAP.msg_meta))
code, screen, log, conn = mv(fetch_raises=None, move_status="OK", status_frozen=True)
check("MV12. review folder count did not grow as expected: warning, still exit 0", code == 0 and "Expected the review folder to grow by 3 but it grew by 0" in screen, screen)

# ---- batching and a failure part-way through
many = {u: hdr("deals@bigretailer.com", f"old {u}", "Mon, 01 Jun 2026 10:00:00 +0000") for u in range(1, 251)}
many_meta = {u: ("\\Seen", stamp(2026, 6, 1)) for u in many}
code, screen, log, conn = run(["--live", "--keep", "2026-10-01", "--yes"], ["1"], messages=dict(many), msg_meta=dict(many_meta))
batches = [c[2][0].count(",") + 1 for c in conn.calls if c[0] == "uid" and c[1] == "MOVE"]
check("MV13. 250 messages move in batches of 100/100/50", code == 0 and batches == [100, 100, 50] and len(FakeIMAP.review) == 250 and "Moved 250 messages" in screen, (batches, screen))
code, screen, log, conn = run(["--live", "--keep", "2026-10-01", "--yes"], ["1"], messages=dict(many), msg_meta=dict(many_meta), fail_move_on=2)
check("MV13. failure on the 2nd batch: 'Stopped after moving 100 of 250', first 100 stay moved, rest untouched, exit 1",
      code == 1 and "Stopped after moving 100 of 250 messages" in screen and len(FakeIMAP.review) == 100 and len(FakeIMAP.messages) == 150, (len(FakeIMAP.review), screen))
check("MV13. the log lists exactly the 100 messages that were moved", log.count("Moved: [uid") == 100, log.count("Moved: [uid"))

# ---- Ctrl-C during the move
code, screen, log, conn = mv(interrupt_on_move=True)
check("MV14. Ctrl-C during the move: exit 130, 'Interrupted' logged with the count, logged out",
      code == 130 and "Interrupted: 0 of 3 messages had been moved" in screen and ("logout",) in conn.calls, (code, screen))
code, screen, log, conn = mv(("\x00",))  # a junk answer, then EOF at the re-asked prompt
check("MV14. Ctrl-D at the confirmation prompt: exit 130, nothing moved", code == 130 and FakeIMAP.review == [], (code, screen))

# ---- live run where the review folder must be created first, then confirmed
code, screen, log, conn = run(["--live", "--keep", "2026-10-01"], ["1", "y", "y"], folders=FOLDERS_NO_REVIEW, messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META))
check("MV15. missing review folder: create prompt, then confirm prompt, then MOVE into the new folder",
      code == 0 and ("create", '"IMAPorganizer"') in conn.calls and sorted(FakeIMAP.review) == [1, 5, 8], (code, screen))
code, screen, log, conn = run(["--live", "--keep", "2026-10-01"], ["1", "y", "n"], folders=FOLDERS_NO_REVIEW, messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META))
check("MV15. ...create yes, move no: folder created, nothing moved", ("create", '"IMAPorganizer"') in conn.calls and FakeIMAP.review == [] and code == 0, screen)

# ---- prefixed (Dovecot-style) server: MOVE target carries the prefix
code, screen, log, conn = run(["--live", "--keep", "2026-10-01", "--yes"], ["1"], folders=PREFIXED, messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META), **PFX)
check("MV16. prefixed server: MOVE goes to \"INBOX.IMAPorganizer\"",
      code == 0 and [c[2][1] for c in conn.calls if c[0] == "uid" and c[1] == "MOVE"] == ['"INBOX.IMAPorganizer"'] and "Moved 3 messages from INBOX to INBOX.IMAPorganizer." in screen, screen)

# ---- second account (STARTTLS, own folders)
code, screen, log, conn = run(["--live", "--keep", "now", "--keep-flagged", "--yes"], ["2"], messages={1: hdr("alerts@oldservice.net", "x", "Mon, 01 Jun 2026 10:00:00 +0000"), 2: hdr("friend@gmail.com", "y", "Mon, 01 Jun 2026 10:00:00 +0000")}, folders=FOLDERS + [b'(\\HasNoChildren) "/" "ToReview"'])
check("MV17. account 2 (STARTTLS, folder ToReview): its own sender list drives the move",
      code == 0 and conn.starttls_called and FakeIMAP.review == [1] and sorted(FakeIMAP.messages) == [2] and [c[2][1] for c in conn.calls if c[0] == "uid" and c[1] == "MOVE"] == ['"ToReview"'], (code, screen))

# ---- the log file keeps the whole story
code, screen, log, conn = mv()
check("MV18. log file: rules, each move, summary; never the password",
      "Moved: [uid 8]" in log and "Moved 3 messages from INBOX to IMAPorganizer." in log and "Mode: LIVE" in log and "TOPSECRET123" not in log, log[-500:])


# ======================= unread-messages report =======================
# Flags in the KEEP scenario (see KEEP_META): uids 1,2,3 are \Seen; 4,5,7,8,9 are unread (7 is also \Flagged).
unread_line = lambda screen, p, n: re.search(rf"^  {re.escape(p)}\s+{n} unread$", screen, re.M)

check("UR0. \\Seen parsed from FLAGS (case-insensitive, among other flags)",
      pm(b'UID 1 FLAGS (\\Seen)').seen and pm(b'UID 1 FLAGS (\\SEEN \\Flagged)').seen and pm(b'UID 1 FLAGS ($Forwarded \\seen)').seen)
check("UR0. unread when \\Seen is absent (other flags, none, or no FLAGS at all)",
      not pm(b'UID 1 FLAGS (\\Flagged \\Answered)').seen and not pm(b'UID 1 FLAGS ()').seen and not pm(b'UID 1').seen)
check("UR0. flagged and seen are read independently", pm(b'UID 1 FLAGS (\\Seen \\Flagged)').flagged and pm(b'UID 1 FLAGS (\\Seen)').flagged is False)

code, screen, log, conn = keep_run(["--keep", "2026-10-01"])
check("UR1. report appears: header, only senders that have unread, total",
      "Unread messages to move:" in screen and unread_line(screen, "newsletter@somestore.com", 1)
      and unread_line(screen, "*@bigretailer.com", 1) and "Total unread to move: 2 of 3." in screen, screen)
check("UR1. senders with no unread messages to move are NOT listed",
      not re.search(r"^  \*@marketing\.example\.org\s+\d+ unread$", screen, re.M), screen)
i_matches = screen.index("Of the 8 matches")
i_header = screen.index("Unread messages to move:")
i_total = screen.index("Total unread to move:")
i_dry = screen.index("Dry run:")
check("UR1. placed right after the 'Of the N matches' line and before the dry-run line", i_matches < i_header < i_total < i_dry, (i_matches, i_header, i_total, i_dry))
check("UR1. unread messages that are KEPT (newer uids 4, 9; flagged uid 7) are not counted", "Total unread to move: 2 of 3." in screen)
check("UR1. existing report lines are unchanged",
      "Of the 8 matches: 3 to move, 2 kept (flagged), 3 kept (received on or after 2026-10-01)." in screen
      and re.search(r"^  newsletter@somestore.com\s+3 matched,\s+2 to move$", screen, re.M)
      and "Dry run: 3 messages would be moved from INBOX to IMAPorganizer. Nothing was changed." in screen, screen)

# flagged + no age limit: the protected unread flagged message (uid 7) must not be counted
code, screen, log, conn = keep_run(["--keep", "now", "--keep-flagged"])
check("UR2. --keep now --keep-flagged: unread 4,5,8,9 counted (not the kept, unread, flagged uid 7)",
      unread_line(screen, "newsletter@somestore.com", 2) and unread_line(screen, "*@bigretailer.com", 2)
      and "Total unread to move: 4 of 6." in screen, screen)
# flagged may move: uid 7 (unread, flagged) now counts; uid 3 (read, flagged) still does not
code, screen, log, conn = keep_run(["--keep", "now", "--move-flagged"])
check("UR3. --move-flagged: unread flagged uid 7 now counts; read flagged uid 3 does not",
      unread_line(screen, "newsletter@somestore.com", 2) and unread_line(screen, "*@bigretailer.com", 3)
      and not re.search(r"^  \*@marketing\.example\.org\s+\d+ unread$", screen, re.M)
      and "Total unread to move: 5 of 8." in screen, screen)

# nothing unread
read_only = {1: hdr("newsletter@somestore.com", "a", "Mon, 01 Jun 2026 10:00:00 +0000"), 2: hdr("deals@bigretailer.com", "b", "Mon, 01 Jun 2026 10:00:00 +0000")}
read_meta = {1: ("\\Seen", stamp(2026, 6, 1)), 2: ("\\Seen", stamp(2026, 6, 1))}
code, screen, log, conn = run(["--keep", "now"], ["1", ""], messages=dict(read_only), msg_meta=dict(read_meta))
check("UR4. all messages to move already read: one 'none (0 of 2)' line, no list, no total",
      "Unread messages to move: none (0 of 2)." in screen and "Total unread" not in screen and " unread\n" not in screen.replace("Unread messages", ""), screen)
code, screen, log, conn = run([], inp, messages={})
check("UR4. empty folder: 'none (0 of 0)', no crash", code == 0 and "Unread messages to move: none (0 of 0)." in screen, screen)
code, screen, log, conn = run(["--keep", "now"], ["1", ""], messages={1: hdr("friend@gmail.com", "x", "Mon, 01 Jun 2026 10:00:00 +0000")})
check("UR4. no sender matches at all: 'none (0 of 0)'", "Unread messages to move: none (0 of 0)." in screen and code == 0, screen)

# all unread
unread_only = {1: hdr("newsletter@somestore.com", "a", "Mon, 01 Jun 2026 10:00:00 +0000"), 2: hdr("newsletter@somestore.com", "b", "Mon, 01 Jun 2026 10:00:00 +0000"), 3: hdr("deals@bigretailer.com", "c", "Mon, 01 Jun 2026 10:00:00 +0000")}
unread_meta = {u: ("", stamp(2026, 6, 1)) for u in unread_only}
code, screen, log, conn = run(["--keep", "now"], ["1", ""], messages=dict(unread_only), msg_meta=dict(unread_meta))
check("UR5. everything unread: counts equal the 'to move' counts",
      unread_line(screen, "newsletter@somestore.com", 2) and unread_line(screen, "*@bigretailer.com", 1) and "Total unread to move: 3 of 3." in screen, screen)

# the report is shown in a live run too, before the confirmation question
code, screen, log, conn = mv(("n",))
check("UR6. live run: the unread report appears before the 'Move N messages?' question",
      "Total unread to move: 2 of 3." in screen and screen.index("Total unread to move") < screen.index("Move 3 messages from"), screen)

# the metadata-after-header server reply style must give the same answer
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], uid_after_literal=True)
check("UR7. alternate server reply style: same unread report", "Total unread to move: 2 of 3." in screen, screen)

# recorded in the log file, and the report changes nothing on the server
code, screen, log, conn = keep_run(["--keep", "2026-10-01"])
check("UR8. log file records the unread report", "Unread messages to move:" in log and "Total unread to move: 2 of 3." in log, log[-600:])
check("UR8. still strictly read-only", {c[1] for c in conn.calls if c[0] == "uid"} <= {"SEARCH", "FETCH"} and not rw_selects(conn) and FakeIMAP.review == [])

# summary screen: the report is part of the essentials (shown), per-message lines are not
ini_replace("screen_output = verbose", "screen_output = summary")
code, screen, log, conn = keep_run(["--keep", "2026-10-01"])
check("UR9. summary screen shows the unread report but no per-message lines",
      "Total unread to move: 2 of 3." in screen and "Would move: [uid" not in screen, screen)
ini_replace("screen_output = verbose", "screen_output = quiet")
code, screen, log, conn = keep_run(["--keep", "2026-10-01"])
check("UR9. quiet screen shows none of it", "Unread messages" not in screen and "Total unread" not in screen, screen)
restore_ini()


# =================== total size, --suggest, and the run-summary line ===================
# ---------------- #5  total size to move ----------------
check("SZ0. RFC822.SIZE parsed from the FETCH metadata",
      pm(b'UID 1 FLAGS (\\Seen) RFC822.SIZE 2048').size == 2048 and pm(b'UID 1 RFC822.SIZE 0').size == 0)
check("SZ0. size is None when the server did not send one", pm(b'UID 1 FLAGS (\\Seen)').size is None)
check("SZ0. format_size: bytes, KB, MB, GB, TB",
      [M.format_size(n) for n in (0, 1, 1023, 1024, 1536, 5 * 1024**2, 3 * 1024**3, 2 * 1024**4, 5000 * 1024**4)]
      == ["0 B", "1 B", "1023 B", "1.0 KB", "1.5 KB", "5.0 MB", "3.0 GB", "2.0 TB", "5000.0 TB"])
class _Msg:  # minimal stand-in
    def __init__(self, size): self.size = size
check("SZ0. describe_total_size: none / unknown / known / partly unknown",
      M.describe_total_size([]) == "0 B" and M.describe_total_size([_Msg(None), _Msg(None)]) == "unknown"
      and M.describe_total_size([_Msg(1000), _Msg(2000)]) == "2.9 KB"
      and M.describe_total_size([_Msg(1000), _Msg(None), _Msg(None)]) == "1000 B (size unknown for 2 messages)"
      and M.describe_total_size([_Msg(1000), _Msg(None)]) == "1000 B (size unknown for 1 message)")

SIZES = {1: 1000, 5: 2000, 8: 3000, 2: 10**7, 3: 10**7, 4: 10**7, 6: 10**7, 7: 10**7, 9: 10**7}   # 1,5,8 are the ones that move
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], msg_sizes=dict(SIZES))
check("SZ1. 'Total size to move' counts only the messages that would move (1000+2000+3000 = 5.9 KB)",
      "Total size to move: 5.9 KB" in screen, screen)
i_unread, i_size, i_dry = screen.index("Total unread to move"), screen.index("Total size to move"), screen.index("Dry run:")
check("SZ1. placed right after the unread report and before the dry-run line", i_unread < i_size < i_dry)
check("SZ1. existing lines unchanged",
      "Of the 8 matches: 3 to move, 2 kept (flagged), 3 kept (received on or after 2026-10-01)." in screen and "Total unread to move: 2 of 3." in screen)
fetches = [c for c in conn.calls if c[0] == "uid" and c[1] == "FETCH"]
check("SZ1. size is requested in the same read-only fetch (BODY.PEEK kept; still only SEARCH/FETCH)",
      fetches and all("RFC822.SIZE" in c[2][1] and "BODY.PEEK" in c[2][1] for c in fetches) and {c[1] for c in conn.calls if c[0] == "uid"} <= {"SEARCH", "FETCH"} and not rw_selects(conn))
code, screen, log, conn = keep_run(["--keep", "2026-10-01"])
check("SZ2. server sends no sizes: 'Total size to move: unknown' (no crash)", "Total size to move: unknown" in screen and code == 0, screen)
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], msg_sizes={1: 1000})
check("SZ3. some sizes unknown: says so", "Total size to move: 1000 B (size unknown for 2 messages)" in screen, screen)
code, screen, log, conn = keep_run(["--keep", "2025-01-01"], msg_sizes=dict(SIZES))
check("SZ4. nothing to move: 'Total size to move: 0 B'", "Total size to move: 0 B" in screen, screen)
code, screen, log, conn = keep_run(["--keep", "now", "--move-flagged"], msg_sizes={u: 1024**2 for u in KEEP_MSGS})
check("SZ5. large totals: 8 matches x 1 MB = 8.0 MB", "Total size to move: 8.0 MB" in screen, screen)
code, screen, log, conn = mv(("n",), msg_sizes=dict(SIZES))
check("SZ6. live run: size shown before the confirmation question", "Total size to move: 5.9 KB" in screen and screen.index("Total size to move") < screen.index("Move 3 messages from"), screen)
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], msg_sizes=dict(SIZES), uid_after_literal=True)
check("SZ7. alternate server reply style: same size", "Total size to move: 5.9 KB" in screen, screen)
ini_replace("screen_output = verbose", "screen_output = quiet")
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], msg_sizes=dict(SIZES))
check("SZ8. quiet screen: not shown; log file still has it", "Total size" not in screen and "Total size to move: 5.9 KB" in log)
restore_ini()

# ---------------- #6  --suggest ----------------
a, c, _ = parse(["--suggest"]);              check("SG0. bare --suggest means 10", c == 0 and a.suggest == 10)
a, c, _ = parse(["--suggest", "5"]);         check("SG0. --suggest 5", c == 0 and a.suggest == 5)
a, c, _ = parse(["--suggest", "--live"]);    check("SG0. --suggest followed by another switch keeps the default of 10", c == 0 and a.suggest == 10 and a.live)
a, c, _ = parse([]);                         check("SG0. off by default", a.suggest is None)
a, c, err = parse(["--suggest", "0"]);       check("SG0. --suggest 0 refused", c == 2 and "1 or more" in err, err)
a, c, err = parse(["--suggest", "abc"]);     check("SG0. --suggest abc refused", c == 2 and "1 or more" in err, err)

SUG = {}
def addm(frm, subject="s"): SUG[len(SUG) + 1] = hdr(frm, subject, "Mon, 01 Jun 2026 10:00:00 +0000")
for i in range(3): addm("LinkedIn <jobs@linkedin.com>")
for i in range(2): addm("alerts@linkedin.com")                    # 5 x linkedin.com (two addresses)
for i in range(3): addm("noreply@github.com")
addm("A@GitHub.COM")                                              # 4 x github.com (case-insensitive)
addm("friend@gmail.com"); addm("other@gmail.com")                 # 2 x gmail.com
addm("x@mail.bigretailer.com")                                    # subdomain of a listed domain: NOT matched, so suggested
addm("deals@bigretailer.com")                                     # matched: must not appear
SUG[len(SUG) + 1] = hdr(None, "no From")                          # no From header at all
SUG_META = {u: ("\\Seen", stamp(2026, 6, 1)) for u in SUG}
def sug_run(argv, answers=("1", ""), **kw):
    return run(["--keep", "now", *argv], list(answers), messages=dict(SUG), msg_meta=dict(SUG_META), **kw)
section = lambda screen: screen[screen.index("Senders not on your list"):] if "Senders not on your list" in screen else ""

code, screen, log, conn = sug_run(["--suggest"])
s = section(screen)
check("SG1. header counts: 13 of the 14 messages are not on the list",
      "Senders not on your list (13 of 14 messages in INBOX), most common domains first:" in s, s)
check("SG1. domains ranked by count, in paste-ready *@domain form",
      all(re.search(rf"^  {re.escape(d)}\s+{n}$", s, re.M) for d, n in
          [("*@linkedin.com", 5), ("*@github.com", 4), ("*@gmail.com", 2), ("*@mail.bigretailer.com", 1)]), s)
check("SG1. order is linkedin, github, gmail, mail.bigretailer",
      s.index("*@linkedin.com") < s.index("*@github.com") < s.index("*@gmail.com") < s.index("*@mail.bigretailer.com"))
check("SG1. already-matched domain (bigretailer.com) is not suggested; subdomain is", "*@bigretailer.com" not in s and "*@mail.bigretailer.com" in s)
check("SG1. a message with no From is noted, not lost", "(1 message had no readable From address)" in s, s)
check("SG1. appears after the main report (after the dry-run line)", screen.index("Dry run:") < screen.index("Senders not on your list"))
check("SG1. exit 0, strictly read-only", code == 0 and {c[1] for c in conn.calls if c[0] == "uid"} <= {"SEARCH", "FETCH"} and not rw_selects(conn) and FakeIMAP.review == [])
check("SG1. also recorded in the log file", "*@linkedin.com" in log and "Senders not on your list" in log)

code, screen, log, conn = sug_run(["--suggest", "2"])
s = section(screen)
check("SG2. --suggest 2 shows two domains and '... and 2 more domains'",
      "*@linkedin.com" in s and "*@github.com" in s and "*@gmail.com" not in s and "... and 2 more domains" in s, s)
code, screen, log, conn = sug_run(["--suggest", "3"])
check("SG2. singular wording: '... and 1 more domain'", "... and 1 more domain\n" in section(screen) + "\n", section(screen))
code, screen, log, conn = sug_run([])
check("SG3. without --suggest nothing is printed", "Senders not on your list" not in screen and "*@linkedin.com" not in screen, screen)

tie = {1: hdr("a@b.com", "x", "Mon, 01 Jun 2026 10:00:00 +0000"), 2: hdr("a@a.com", "x", "Mon, 01 Jun 2026 10:00:00 +0000"), 3: hdr("q@c.com", "x", "Mon, 01 Jun 2026 10:00:00 +0000"), 4: hdr("q@c.com", "x", "Mon, 01 Jun 2026 10:00:00 +0000")}
code, screen, log, conn = run(["--keep", "now", "--suggest"], ["1", ""], messages=dict(tie), msg_meta={u: ("\\Seen", stamp(2026, 6, 1)) for u in tie})
s = section(screen)
check("SG4. equal counts are ordered alphabetically (c.com has 2; then a.com before b.com)", s.index("*@c.com") < s.index("*@a.com") < s.index("*@b.com"), s)
allmatched = {1: hdr("deals@bigretailer.com", "x", "Mon, 01 Jun 2026 10:00:00 +0000")}
code, screen, log, conn = run(["--keep", "now", "--suggest"], ["1", ""], messages=dict(allmatched), msg_meta={1: ("\\Seen", stamp(2026, 6, 1))})
check("SG5. nothing unmatched: '(none)'", "(none)" in section(screen) and "(0 of 1 messages in INBOX)" in section(screen), section(screen))
code, screen, log, conn = run(["--suggest"], ["1", ""], messages={})
check("SG5. empty folder: no crash", code == 0 and "(0 of 0 messages in INBOX)" in section(screen), screen)

code, screen, log, conn = run(["--live", "--keep", "now", "--suggest", "3"], ["1", "n"], messages=dict(SUG), msg_meta=dict(SUG_META))
check("SG6. live run: shown before the confirmation question, and 'n' still cancels cleanly",
      screen.index("Senders not on your list") < screen.index("Move 1 message from") and "Cancelled: nothing was moved." in screen and FakeIMAP.review == [], screen)
ini_replace("screen_output = verbose", "screen_output = quiet")
code, screen, log, conn = sug_run(["--suggest"])
check("SG7. quiet screen: not shown (log file still has it)", "Senders not on your list" not in screen and "*@linkedin.com" in log)
restore_ini()

# ---------------- #2  run summary line in the log ----------------
SUMMARY = "Run summary: you@example.com | DRY-RUN | scanned 9 | matched 8 | would move 3 | unread 2 | kept 5 | size 5.9 KB"
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], msg_sizes=dict(SIZES))
check("RS1. dry run: exactly one summary line, in the log, with the right figures", log.count("Run summary:") == 1 and SUMMARY in log, log[-500:])
check("RS1. it is the last line before 'run finished'", log.index(SUMMARY) < log.index("=== IMAPorganizer run finished ===") and
      log[log.index(SUMMARY):log.index("=== IMAPorganizer run finished")].count("\n") == 1)
check("RS1. timestamped like every log line", re.search(r"^\d{4}-\d{2}-\d{2} \d\d:\d\d:\d\d INFO    Run summary:", log, re.M))
check("RS1. password never appears", "TOPSECRET123" not in log)
ini_replace("screen_output = verbose", "screen_output = summary")
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], msg_sizes=dict(SIZES))
check("RS2. summary screen: the line is NOT printed (log only); verbose would show it", "Run summary:" not in screen and SUMMARY in log)
restore_ini()
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], msg_sizes=dict(SIZES))
check("RS2. verbose screen shows it too", SUMMARY in screen)

code, screen, log, conn = mv(("y",), msg_sizes=dict(SIZES))
check("RS3. live run that moved 3", "Run summary: you@example.com | LIVE | scanned 9 | matched 8 | moved 3 | unread 2 | kept 5 | size 5.9 KB" in log, log[-500:])
code, screen, log, conn = mv(("n",), msg_sizes=dict(SIZES))
check("RS4. live run declined at the question", "| LIVE | scanned 9 | matched 8 | moved 0 (declined) |" in log, log[-500:])
code, screen, log, conn = run(["--live", "--keep", "2025-01-01"], ["1"], messages=dict(KEEP_MSGS), msg_meta=dict(KEEP_META))
check("RS5. live run with nothing to move", "| LIVE | scanned 9 | matched 8 | moved 0 | unread 0 | kept 8 |" in log, log[-500:])
code, screen, log, conn = mv(("y",), hook_rw_select=flag_uid1_and_delete_uid5)
check("RS6. changed since the scan (2 skipped): the summary reports what really moved (1)", "| LIVE | scanned 9 | matched 8 | moved 1 |" in log, log[-500:])
code, screen, log, conn = mv(("y",), move_status="NO")
check("RS7. move refused by the server: summary says FAILED", "| LIVE | scanned 9 | matched 8 | FAILED |" in log and code == 1, log[-500:])
code, screen, log, conn = mv(("y",), interrupt_on_move=True)
check("RS7. Ctrl-C during the move: summary says CANCELLED", "| LIVE | scanned 9 | matched 8 | CANCELLED |" in log and code == 130, log[-500:])
code, screen, log, conn = mv(("y",), capabilities=b"IMAP4rev1")
check("RS7. server cannot move safely: FAILED, nothing moved", "| FAILED |" in log and FakeIMAP.review == [], log[-500:])
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], login_error=b"[AUTHENTICATIONFAILED] no")
check("RS8. a run that never reached the scan (login failed) has no summary line", "Run summary" not in log and code == 1, log[-300:])
code, screen, log, conn = run([], inp, folders=FOLDERS_NO_REVIEW[:0] + FOLDERS, fetch_status="NO", messages=dict(MSGS))
check("RS8. a run that failed while reading the folder has no summary line either", "Run summary" not in log and code == 1, log[-300:])
code, screen, log, conn = run([], ["1", ""], messages=dict(MSGS))
check("RS9. empty-ish/plain run: summary present with zero kept (no keep rules active)", "| DRY-RUN | scanned 10 | matched 4 | would move 4 | unread 4 | kept 0 |" in log, log[-400:])
ini_replace("log_level = INFO", "log_level = NONE")
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], msg_sizes=dict(SIZES))
check("RS10. log_level NONE: no log file, no crash", code == 0 and not os.path.exists("IMAPorganizer.log"), log)
restore_ini()
ini_replace("log_level = INFO", "log_level = DEBUG")
code, screen, log, conn = keep_run(["--keep", "2026-10-01"], msg_sizes=dict(SIZES))
check("RS10. DEBUG level also has exactly one", log.count("Run summary:") == 1, log.count("Run summary:"))
restore_ini()

print(f"\n{passed} checks passed, {failures} failed.")
sys.exit(1 if failures else 0)
