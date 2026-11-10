# IMAPorganizer

IMAPorganizer tidies an IMAP mailbox by moving mail from senders you list into
a **review folder** on the server. You then look through that folder in your
normal email client and decide what to delete or move back. The script never
deletes anything permanently.

It is a single Python script plus one `.ini` config file, with no third-party
packages. It runs interactively: you pick an account, type the password, and
choose a dry run or a live run.

## What it does

- Reads the messages in a source folder (normally `INBOX`) and matches each
  message's **From** address against a sender list you keep per account.
- Moves the matches into a review folder, **except** messages you want left
  alone:
  - messages received on or after a cutoff day (so recent mail stays put), and
  - flagged (starred) messages.
- Starts every run as a **dry run** by default: it reports exactly what it
  would move and changes nothing.
- Logs everything it does, including every message it moves.

## Requirements

- Python 3.10 or newer. Developed and tested on Ubuntu 24.04 with Python 3.12.
- Standard library only; there is nothing to install.
- An IMAP account reachable over TLS (port 993) or STARTTLS (port 143).
- For live runs, the server must support `MOVE` or `UIDPLUS` (nearly every
  modern server does; see [How messages are moved](#how-messages-are-moved)).

## Quick start

1. Put `IMAPorganizer.py` in a folder of your choice, and copy the sample
   config next to it:

   ```
   cp IMAPorganizer.sample.ini IMAPorganizer.ini
   ```
2. Edit `IMAPorganizer.ini`: set your server and sender list (see
   [Configuration](#configuration)).
3. Run it:

   ```
   python3 IMAPorganizer.py
   ```

   (or `chmod +x IMAPorganizer.py` and run `./IMAPorganizer.py`)
4. Choose the account, type the password (not shown on screen), accept the
   defaults for the keep rules, and press Enter at the dry-run question.
5. Read the report. When it looks right, run it again and answer **n** to the
   dry-run question (or use `--live`) to move the messages for real.

`python3 IMAPorganizer.py --help` lists all command-line switches.

## Configuration

The script reads `IMAPorganizer.ini` from **the same folder as the script**.
It uses INI syntax: a `[global]` section for defaults, then one section per
account. Lines starting with `#` are comments; put comments on their own lines,
never at the end of a setting.

The file never contains passwords. The script asks for the password every run.

> **Starting point and version control.** The project includes
> `IMAPorganizer.sample.ini`, a template with placeholder accounts. Copy it to
> `IMAPorganizer.ini` and edit that copy; the script only ever reads
> `IMAPorganizer.ini`. Your real `IMAPorganizer.ini` lists your accounts and the
> senders you filter, and `IMAPorganizer.log` records sender addresses and
> subject lines, so keep both out of version control by listing them in
> `.gitignore`:
>
> ```
> IMAPorganizer.ini
> IMAPorganizer.log
> __pycache__/
> ```

### Example

```ini
[global]
source_folder   = INBOX
review_folder   = IMAPorganizer
dry_run_default = yes
log_file        = IMAPorganizer.log
log_level       = INFO
screen_output   = summary
keep_recent     = now
keep_flagged    = yes

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
review_folder = ToReview
keep_recent = 3m
keep_flagged = no
senders =
    *@spamdomain.com
```

### `[global]` settings

All are optional; the values shown are the built-in defaults.

| Setting | Default | Meaning |
|---|---|---|
| `source_folder` | `INBOX` | Folder that is scanned. |
| `review_folder` | `IMAPorganizer` | Folder that matched messages are moved into. |
| `dry_run_default` | `yes` | The default answer to the dry-run prompt. |
| `log_file` | `IMAPorganizer.log` | Log file. A bare name is created next to the script. |
| `log_level` | `INFO` | `NONE` (no log file), `INFO`, or `DEBUG`. |
| `screen_output` | `summary` | `quiet`, `summary`, or `verbose`. |
| `keep_recent` | `now` | How much recent mail stays in place (see [below](#the-two-keep-rules)). |
| `keep_flagged` | `yes` | Whether flagged (starred) messages stay in place. |

### Account sections

The section name is the account's email address, and it must contain an `@`.
The accounts appear in the account prompt in the order they are written here,
and the first one is the default.

| Setting | Required | Meaning |
|---|---|---|
| `server` | yes | IMAP server name. |
| `senders` | yes | The sender list (see below). |
| `port` | no | Default `993`. Port `143` uses STARTTLS; any other port uses TLS from the start. |
| `username` | no | Login name. Defaults to the email address. |
| `source_folder` | no | Overrides the `[global]` value for this account. |
| `review_folder` | no | Overrides the `[global]` value for this account. |
| `keep_recent` | no | Overrides the `[global]` value for this account. |
| `keep_flagged` | no | Overrides the `[global]` value for this account. |

Misspelled setting names are reported as errors rather than silently ignored,
and all configuration problems are reported together before anything runs.

### The sender list

One entry per line, indented under `senders =`. Matching is **case-insensitive**
and uses the message's **From** address only.

| Entry | Matches |
|---|---|
| `name@example.com` | exactly that address |
| `*@example.com` | any address at exactly `example.com` |

`*@example.com` does **not** match subdomains such as `mail.example.com`, or
lookalikes such as `notexample.com`. If you need a subdomain, list it
separately (`*@mail.example.com`). Comments are not allowed inside the list.

### Folder names

Folder names in the config are relative to the server's personal folder
prefix. Some servers (commonly Dovecot setups such as cPanel) keep every folder
under `INBOX.`, so a folder you think of as `IMAPorganizer` is really
`INBOX.IMAPorganizer`. The script asks the server for its prefix and adds it
for you, so plain names work everywhere; the full name also works if you
prefer to write it. Names with spaces and accented characters are supported.
The source and review folders must be different folders.

## Running it

### The prompts

In order, with the default shown by pressing Enter:

1. **Account**: a numbered list; Enter picks the first.
2. **Password**: typed without echo, held in memory only.
3. **Keep recent**: how much recent mail to leave alone.
4. **Keep flagged**: whether flagged messages stay in place.
5. **Dry run**: report only, or move for real (default: dry run).

The defaults for steps 3 to 5 come from the config. Each prompt is skipped if
you give the matching command-line switch.

### Command-line switches

| Switch | Effect |
|---|---|
| `--dry-run` | Dry run; skips the dry-run prompt. |
| `--live` | Live run; skips the dry-run prompt. |
| `--keep AGE` | Sets the keep-recent rule; skips that prompt. |
| `--keep-flagged` | Never move flagged messages; skips that prompt. |
| `--move-flagged` | Treat flagged messages like any other; skips that prompt. |
| `--yes` | Live run only: skip the final "Move N messages?" confirmation. |
| `--suggest [N]` | Also list the N (default 10) most common sender domains that are **not** on your sender list, as ready-to-paste entries. See [Finding senders to add](#finding-senders-to-add). |

`--dry-run` with `--live`, and `--keep-flagged` with `--move-flagged`, are
refused as conflicting.

**Precedence** for the keep rules, strongest first: command-line switch, what
you type at the prompt, the account's own setting, the `[global]` setting, the
built-in default.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Finished (including a live run you declined at the confirmation). |
| `1` | A problem: bad config, connection or login failure, missing folder, refused move. |
| `2` | Invalid or conflicting command-line switches. |
| `130` | Cancelled with Ctrl-C or Ctrl-D. |

## The two keep rules

A message is moved only if it matches the sender list **and** no keep rule
protects it.

### Keep recent

Messages **received on or after the cutoff day stay in place**; only older
matches may be moved. Accepted values (the same for the prompt, `--keep`, and
the config):

| Value | Cutoff |
|---|---|
| `now` | No age limit: every match is eligible. |
| `30d`, `2w`, `3m`, `1y` | That many days, weeks, months or years before today. Spelled-out forms such as `3 months` also work. |
| `2026-08-04` | An exact date, in `YYYY-MM-DD` form only. Dates in the future are refused. |

The script always prints the exact cutoff date it worked out, for example
`Age rule: messages received on or after 2026-08-04 stay in place`. Months and
years are calendar-based, and the day is clamped when needed (one month before
March 31 is February 28). "Today" is the date on the machine running the
script.

The age rule uses the date the **server received** the message, not the Date
header written by the sender, because the header can be wrong or forged. If a
message's received date cannot be read and an age limit is set, the message is
kept, and it is counted separately in the report.

### Keep flagged

Messages carrying the IMAP `\Flagged` flag stay in place when this is on (the
default). That is the flag Roundcube calls **Flagged** and Thunderbird calls
**Starred**. Colored tags and labels are a different mechanism and are not
considered. A message that is both flagged and recent is counted as flagged in
the report.

## Dry run and live run

### Dry run

Connects, logs in, checks the folders, reads the messages, and reports:

```
9 of 11 messages in INBOX match the sender list.
  newsletter@somestore.com           3 matched,   2 to move
  *@marketing.example.org            1 matched,   0 to move
  *@bigretailer.com                  5 matched,   2 to move
Of the 9 matches: 4 to move, 2 kept (flagged), 3 kept (received on or after 2026-10-01).
Unread messages to move:
  newsletter@somestore.com           1 unread
  *@bigretailer.com                  2 unread
Total unread to move: 3 of 4.
Total size to move: 41.3 MB
Dry run: 4 messages would be moved from INBOX to INBOX.IMAPorganizer. Nothing was changed.
```

The unread report counts only messages that would actually be moved and that
have not been read (they lack the IMAP `\Seen` flag). It lists just the
sender-list entries that have some, then a total. If none of the messages to
move are unread, it prints a single line saying so.

`Total size to move` adds up the sizes of the messages that would be moved
(1 KB is 1024 bytes). If the server does not report sizes it says `unknown`.

A sender-list entry that shows 0 matches is worth a look: it may have a typo
or may be stale. Use `screen_output = verbose`, or read the log, to see every
message with the reason it would move or stay. A dry run also reports whether a
live run could move mail on this server.

A dry run changes nothing on the server. It never creates the review folder,
and it never marks messages as read.

### Live run

1. If the review folder does not exist, the script says so and asks whether to
   create it (default: stop). When it creates the folder it also subscribes to
   it so that email clients show it.
2. It scans and reports exactly as a dry run does.
3. It asks `Move N messages from X to Y? [y/N]`. The default is No. `--yes`
   skips the question.
4. After you confirm, it re-opens the folder for changes and checks that the
   folder's UIDVALIDITY (the server's stamp for "message numbers are still
   valid") has not changed. It then re-reads the flags of the messages about to
   move. Anything flagged, moved or deleted since the scan is skipped and logged.
5. It moves the messages in batches of 100 and logs every message as
   `Moved: [uid …] date | sender | subject`. It finishes with the count and the
   review folder's new size.

### How messages are moved

- If the server supports the `MOVE` command, that is used.
- Otherwise, if it supports `UIDPLUS`, the script copies the messages, marks
  the originals deleted, and expunges **only those messages** (`UID EXPUNGE`).
  It never issues a plain `EXPUNGE`, which would also remove other messages you
  had marked deleted.
- If the server supports neither, a live run is refused before the
  confirmation prompt and nothing is touched.

If something fails part-way, the script stops and says how many messages had
already been moved. If a copy succeeds but the delete step fails, the messages
exist in both folders, and the script tells you so; nothing is lost.

### Undoing a move

Moved messages are in the review folder with their flags and dates intact.
Drag them back to the source folder in your email client. The log lists every
message that was moved.

## Finding senders to add

To see which senders are *not* on your list yet, add `--suggest`:

```
python3 IMAPorganizer.py --suggest        # the top 10 domains
python3 IMAPorganizer.py --suggest 25     # the top 25
```

After the normal report it prints the most common sender domains among the
messages that did not match, most frequent first:

```
Senders not on your list (1164 of 1248 messages in INBOX), most common domains first:
  *@linkedin.com                      94
  *@github.com                        61
  *@example-shop.com                  37
  ... and 34 more domains
```

The entries are written the way the sender list wants them, so you can copy a
line straight into `senders =`. Domains are counted case-insensitively, and a
subdomain such as `mail.example.com` counts separately from `example.com`.
Messages with no readable From address are noted separately. `--suggest` only
reads what the scan already fetched, so it changes nothing; it works in dry and
live runs, and it is hidden if `screen_output = quiet` (the log file still has it).

## Logging

The log file is appended to on every run, with a timestamp on each line.

| `log_level` | What the file contains |
|---|---|
| `NONE` | No log file is created. |
| `INFO` | Run settings, each matched message with its decision, each move, results, warnings and errors. |
| `DEBUG` | Everything in `INFO`, plus the server's folder list and capabilities and the messages that did not match. |

`screen_output` controls the screen separately: `quiet` shows only warnings
and errors, `summary` shows the essentials (the account, mode, counts and
results), and `verbose` adds a line per message. The prompts always appear.

If the log file cannot be opened, the script stops rather than run without the
audit trail you asked for. Set `log_level = NONE` to run without a log.

### Run summary line

Each run that gets as far as reading the folder ends with one line in the log
file, which makes it easy to search or compare runs:

```
2026-11-08 18:29:57 INFO    Run summary: you@example.com | LIVE | scanned 1248 | matched 84 | moved 20 | unread 3 | kept 64 | size 41.3 MB
```

`scanned` is every message in the source folder, `matched` is the number that
matched the sender list, and `kept` is how many of those were left in place by
the keep rules. `unread` and `size` describe the messages that were to be
moved. The action reads `would move N` for a dry run, `moved N` for a live run
(`moved 0 (declined)` if you answered no), and `FAILED` or `CANCELLED` if the
run stopped early. The line goes in the log at the `INFO` level, so it appears
in the log file and on a `verbose` screen, but not on the `summary` screen. A
run that fails before the folder is read (for example a login failure) has no
summary line; its error is in the log.

## Security and privacy

- **Passwords** are never stored, printed or logged. The script holds the
  password in memory for the run only. The password code is isolated in one
  function so another source can be added later.
- **TLS certificates are always verified.** There is no option to turn this off.
  If your server's certificate doesn't match the name in `server`, or isn't
  issued by a trusted authority, the script stops with an explanation.
- The script reads messages **read-only** and uses the server's "peek" form of
  fetch, so scanning never marks mail as read.
- The log file is created readable by its owner only. It does contain sender
  addresses and subject lines, so treat it as private.
- IMAP library debugging, which can print login commands, is never enabled.

## Troubleshooting

| Message | What to do |
|---|---|
| `Problems found in IMAPorganizer.ini` | Fix each listed problem; they are all reported at once. |
| `Login failed … [AUTHENTICATIONFAILED]` | Check the password. If your server uses a login that is not the email address, add a `username` setting. |
| `The TLS certificate … could not be verified` | The `server` name must match the name on the server's certificate, and the certificate must come from a trusted authority. Use the host name your provider documents for IMAP. |
| `TLS problem … wrong version number` | The port and connection type disagree: 993 expects TLS from the start, 143 expects STARTTLS. |
| `Source folder … was not found` | The message lists the folders that do exist. Check the spelling in the config. |
| `The server would not create …` | The server refused to make the review folder. Create it yourself in your email client, or try another name. |
| `supports neither MOVE nor UIDPLUS` | The server can't move mail safely. A dry run still works. |
| `… UIDVALIDITY is different or missing` | The folder was rebuilt on the server between the scan and the move. Nothing was moved; run again. |
| `Warning: Password input may be echoed` | You are not running in a real terminal, so the password prompt can't hide typing. |

## Running the tests

`test_IMAPorganizer.py` checks the script against a simulated IMAP server. It
needs no network, no account and no extra packages. It runs in a temporary
folder with its own built-in config, so it never reads or changes your
`IMAPorganizer.ini` or `IMAPorganizer.log`.

```
python3 test_IMAPorganizer.py        # failures and a summary
python3 test_IMAPorganizer.py -v     # also lists every passing check
```

The exit code is `0` if every check passed. Run it after any change to the
script, and fix failures before using the script on real mail.

It covers configuration validation, sender matching, both keep rules, the
prompts and switches, folder checks (including servers with an `INBOX.`
prefix), connection and login errors, dry runs (which must never change
anything), and live moves: confirmation, the re-check before moving, `MOVE` and
the `COPY` fallback, partial failures, and Ctrl-C.

A simulated server cannot reproduce every quirk of a real one. After changing
the script, also do a dry run on a real account, and try a live run on a
throwaway account first.

## Limitations

- Matching uses the **From header only**. That header is written by the sender
  and can be forged, so this is a tidying tool and not a spam or security filter.
- One account is processed per run.
- Every run reads the headers of every message in the source folder. On a very
  large mailbox this takes a while.
- The received date used by the keep-recent rule can be reset by mailbox
  migrations or imports, which makes old mail look new.
- Passwords containing non-ASCII characters are not supported.
- Password prompting is the only supported login method for now. OAuth2 (for
  example Gmail or Microsoft 365 accounts that no longer accept passwords) is
  not supported.

## Files

| File | Purpose |
|---|---|
| `IMAPorganizer.py` | The script. |
| `test_IMAPorganizer.py` | Automated tests (simulated server; no network needed). |
| `IMAPorganizer.sample.ini` | Template config with placeholder accounts. Copy it to `IMAPorganizer.ini`. |
| `IMAPorganizer.ini` | Your configuration (accounts, sender lists, defaults). No passwords. Not committed to git. |
| `IMAPorganizer.log` | Created on the first run (unless `log_level = NONE`). Not committed to git. |
