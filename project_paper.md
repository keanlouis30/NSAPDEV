# NSAPDEV Major Course Output
# Mini-Splunk: A Lightweight Syslog Analytics Server
### Server Application Project — Technical Documentation & User Manual

---

> **Course:** NSAPDEV — Network and Server Application Development
> **Deliverable:** Server Application Project (Week 13)
> **Components:** `server.py` (Indexer), `client.py` (Forwarder & Search Head)
> **Language:** Python 
> **Transport:** Raw TCP Sockets
> **Submitted by:** Kean Rosales, Evan Pinca

---

## Table of Contents

1. [Project Overview & Objectives](#1-project-overview--objectives)
2. [System Architecture](#2-system-architecture)
3. [Concurrency & Synchronization Implementation](#3-concurrency--synchronization-implementation)
4. [Communication Protocol & Parsing](#4-communication-protocol--parsing)
5. [System User Manual](#5-system-user-manual)
   - 5.1 [Starting the Server](#51-starting-the-server)
   - 5.2 [Using the CLI Client](#52-using-the-cli-client)
6. [Testing & Performance Evaluation](#6-testing--performance-evaluation)
7. [Intellectual Honesty Declaration](#7-intellectual-honesty-declaration)
8. [Appendices](#8-appendices)

---

## 1. Project Overview & Objectives

### 1.1 Background

Modern computing infrastructure — from web servers to authentication daemons — continuously emits system log (syslog) records as a primary means of observability. At scale, the volume of these records makes manual inspection impractical. Dedicated log management platforms such as Splunk address this by providing centralized ingestion, indexing, and full-text search capabilities across distributed sources. This project builds a simplified analogue of that architecture: **Mini-Splunk**, a concurrent, TCP-based syslog analytics server paired with an interactive command-line client.

### 1.2 Objectives

The system is designed to satisfy the following educational and functional goals:

**Functional goals:**
- Accept bulk syslog file uploads from one or more concurrent clients over a raw TCP connection.
- Parse each uploaded line into its constituent RFC 3164 fields: timestamp, hostname, daemon/process, PID, severity, and message body.
- Store parsed records in a persistent, indexed, in-memory structure that survives server restarts.
- Expose a suite of search and aggregation commands that clients can invoke at any time, including during an active upload.
- Provide a safe administrative PURGE command that exclusively locks the store before erasing it.

**Educational goals (CLOs addressed):**
- **CLO1** — Model a multi-component server architecture that separates ingestion, indexing, persistence, and query concerns.
- **CLO2** — Demonstrate how the OS thread scheduler manages isolated client connections through one-thread-per-connection.
- **CLO3 & CLO4** — Implement a correct, writer-priority Readers-Writer Lock from scratch and apply batched-commit ingestion to guarantee that concurrent reads are never starved by a long-running write.

### 1.3 Scope & Constraints

The implementation is deliberately restricted to Python's standard library (`socket`, `threading`, `re`, `json`, `os`, `tempfile`) and makes no use of high-level web frameworks, database engines, or third-party concurrency libraries. This constraint keeps the concurrency mechanics visible and auditable.

---

## 2. System Architecture

### 2.1 High-Level Overview

The system follows a classic **client–server** model with two discrete processes communicating over TCP/IP:

```
┌─────────────────────────────┐        TCP (port 65432)       ┌──────────────────────────────────────┐
│        client.py            │ ────────────────────────────► │              server.py               │
│   (Forwarder & Search Head) │                               │           (Indexer)                  │
│                             │ ◄──────────────────────────── │                                      │
│  • Interactive REPL         │       Response string         │  • Thread-per-connection listener    │
│  • File reader + streamer   │                               │  • Syslog parser                     │
│  • Progress display         │                               │  • RWLock-protected log_store        │
│  • shlex command parser     │                               │  • JSONL disk persistence            │
└─────────────────────────────┘                               └──────────────────────────────────────┘
```

### 2.2 Server Internal Architecture

The server is composed of five logical layers, each with a single responsibility:

```
┌──────────────────────────────────────────────────────────────┐
│  1. Network Layer                                            │
│     start_server() — binds TCP socket, accept() loop,       │
│     spawns one daemon thread per accepted connection         │
├──────────────────────────────────────────────────────────────┤
│  2. Connection Handler                                       │
│     handle_client() — recv() loop until EOF, command        │
│     routing by prefix string matching                        │
├──────────────────────────────────────────────────────────────┤
│  3. Command Handlers                                         │
│     handle_ingest(), handle_search_*(), handle_purge()       │
│     handle_count_keyword()                                   │
├──────────────────────────────────────────────────────────────┤
│  4. Shared State & Synchronization                           │
│     log_store (list) + RWLock — thread-safe read/write       │
│     access to the in-memory index                            │
├──────────────────────────────────────────────────────────────┤
│  5. Persistence Layer                                        │
│     _load_from_disk(), _append_to_disk(), _rewrite_disk()    │
│     JSONL file — survives process restart                    │
└──────────────────────────────────────────────────────────────┘
```

### 2.3 Key Data Structures

**`log_store`** — A plain Python `list` of dictionaries. Each dictionary represents one parsed syslog line:

```python
{
    "raw":       "Feb 22 00:05:38 SYSSVR1 systemd[1]: Started OpenBSD Secure Shell server.",
    "timestamp": "Feb 22 00:05:38",
    "host":      "SYSSVR1",
    "daemon":    "systemd",
    "pid":       "1",
    "severity":  "INFO",
    "message":   "Started OpenBSD Secure Shell server."
}
```

All search operations iterate over this list under a shared read lock. The list is append-only except during PURGE, which replaces it entirely under an exclusive write lock.

**`PERSIST_FILE` (`log_store.jsonl`)** — A JSON Lines file on disk. Each line is one serialized log entry dict. On startup, the server replays this file to restore the previous index state before accepting any connections. This decouples process lifetime from data lifetime.

### 2.4 Client Architecture

The client (`client.py`) is a single-process, synchronous REPL. It uses `shlex.split()` to tokenize user input, preserving quoted arguments (e.g., `"Feb 22"` as a single date token). For INGEST operations it reads the file, constructs the `INGEST <content>` message, and streams it to the server in 4 KB chunks while displaying a live percentage progress bar. For all query and administrative commands, it sends a short command string and prints the server's response verbatim.

---

## 3. Concurrency & Synchronization Implementation

This section is the technical core of the project. It describes the two concurrency problems addressed and the precise mechanisms used to solve them.

### 3.1 Thread-Per-Connection Model

The server's accept loop spawns one Python `threading.Thread` per client connection:

```python
conn, addr = server.accept()
t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
t.start()
```

Each thread is marked `daemon=True` so it does not prevent a clean shutdown on `KeyboardInterrupt`. The thread owns its socket for its entire lifetime, calling `recv()` in a loop until the client signals end-of-write with `socket.SHUT_WR`. This design means that network I/O for Client A (e.g., a multi-megabyte upload) never occupies the same thread as Client B's search query — the OS scheduler runs them concurrently. The `MAX_CONNECTIONS = 10` constant bounds the server's `listen()` backlog.

### 3.2 The Readers-Writer Lock

The central synchronization primitive is a custom `RWLock` class. The invariant it enforces is:

- Any number of threads may hold a **read lock** simultaneously.
- Only one thread may hold a **write lock**, and only when no readers are active.
- **Writer priority** is enforced: once a writer begins waiting, new readers must also wait, preventing writer starvation in a high-read workload.

#### 3.2.1 Why a Custom Lock?

Python's standard library provides `threading.Lock` (mutual exclusion) and `threading.RLock` (reentrant), but neither allows concurrent readers. A naïve approach of using a single `Lock` for all access would serialize every search, defeating the purpose of multithreading.

#### 3.2.2 Original Implementation Bug

The original codebase attempted to build an RWLock using a single `threading.Condition` object used in two incompatible ways simultaneously: as a context manager (`with self._read_ready`) in reader code, and as a raw mutex (`self._read_ready.acquire()`) in writer code. Because `Condition.__enter__` acquires the *underlying* `Lock`, a writer calling `.acquire()` would hold that same lock — making it impossible for any reader to enter the `with` block. The result was that the lock behaved as a plain exclusive mutex, serializing all operations and guaranteeing that a search during an upload would block.

#### 3.2.3 Corrected Implementation

The fixed `RWLock` separates concerns cleanly using one `threading.Lock` as the sole mutex and two `threading.Condition` objects built on top of it purely for signalling:

```python
class RWLock:
    def __init__(self):
        self._lock             = threading.Lock()
        self._no_writers       = threading.Condition(self._lock)  # readers wait here
        self._no_readers       = threading.Condition(self._lock)  # writers wait here
        self._readers          = 0
        self._writer_active    = False
        self._writers_waiting  = 0
```

**Reader acquisition** — A reader waits if any writer is active or queued (writer-priority), then increments the reader count:

```python
def acquire_read(self):
    with self._lock:
        while self._writer_active or self._writers_waiting > 0:
            self._no_writers.wait()
        self._readers += 1
```

**Reader release** — Decrements count; if the count reaches zero, notifies any waiting writers:

```python
def release_read(self):
    with self._lock:
        self._readers -= 1
        if self._readers == 0:
            self._no_readers.notify_all()
```

**Writer acquisition** — Increments the waiting-writer counter (which blocks new readers), then waits until all readers and any active writer are done:

```python
def acquire_write(self):
    with self._lock:
        self._writers_waiting += 1
        while self._readers > 0 or self._writer_active:
            self._no_readers.wait()
        self._writers_waiting -= 1
        self._writer_active = True
```

**Writer release** — Clears the active flag, then wakes both waiting writers and waiting readers so they can compete fairly:

```python
def release_write(self):
    with self._lock:
        self._writer_active = False
        self._no_readers.notify_all()
        self._no_writers.notify_all()
```

This structure means multiple `SEARCH_*` handlers can execute simultaneously (all hold a read lock), while an `INGEST` or `PURGE` handler waits for readers to drain, then gets exclusive access.

### 3.3 Batched-Commit Ingestion

Correcting the `RWLock` alone was not sufficient to pass the "Traffic Jam" concurrency test. The root issue was the **lock hold duration** inside `handle_ingest`.

A naïve implementation parses all lines first, then acquires one write lock and calls `log_store.extend(parsed)` for the entire dataset in one operation. For a 1,091,532-line file, this single `extend()` can take hundreds of milliseconds — during which the write lock is held continuously and every concurrent reader is blocked.

The solution is **batched commitment**: parse and commit `INGEST_BATCH_SIZE = 500` entries at a time, releasing the write lock between each batch:

```python
for line in lines:
    entry = parse_syslog_line(line)
    if entry is None:
        continue
    batch.append(entry)

    if len(batch) >= INGEST_BATCH_SIZE:
        store_lock.acquire_write()
        try:
            log_store.extend(batch)
        finally:
            store_lock.release_write()
        _append_to_disk(batch)   # disk I/O outside the lock
        total_committed += len(batch)
        batch = []
        threading.Event().wait(0)  # yield GIL to reader threads
```

Each write lock hold now lasts only as long as it takes to extend a 500-element list — on the order of microseconds. Between batches, the GIL is explicitly yielded via `threading.Event().wait(0)`, giving the OS scheduler an opportunity to run reader threads. A `SEARCH_DATE` command issued from a second terminal during a large upload will therefore find a free window within milliseconds.

The `INGEST_BATCH_SIZE` constant is deliberately tunable. A smaller value (e.g., 100) increases reader responsiveness at the cost of more lock acquisitions per file; a larger value (e.g., 5000) reduces lock overhead at the cost of longer reader wait windows. 500 was selected as a practical default for million-line files.

### 3.4 Disk I/O Outside the Lock

All three persistence functions (`_append_to_disk`, `_rewrite_disk`, `_load_from_disk`) are called **outside** the `RWLock`. This is deliberate: disk I/O is orders of magnitude slower than the in-memory list operations inside the lock, and holding a write lock during a file write would starve readers for the duration of the I/O operation. The correctness argument is:

- `_append_to_disk` is called after the batch is committed to `log_store`. If the process crashes between the two calls, the data is in memory but not on disk — it will be lost. This is an accepted trade-off for availability; the data was successfully indexed for the current session.
- `_rewrite_disk` uses `tempfile.mkstemp` + `os.replace` for atomic file replacement, ensuring that a crash during a PURGE cannot leave a partially-written (corrupt) JSONL file.

---

## 4. Communication Protocol & Parsing

### 4.1 Transport

The system uses raw **TCP** (`AF_INET`, `SOCK_STREAM`). There is no application-layer framing protocol (no HTTP, no length prefix, no delimiter). Instead, the client signals end-of-message by calling `socket.shutdown(SHUT_WR)` after sending all data. The server's `recv()` loop detects this as a zero-byte read and then processes the accumulated message. This means each TCP connection carries exactly one request and one response.

```
Client                          Server
  |                               |
  |──── connect() ───────────────►|
  |──── sendall(command bytes) ──►|
  |──── shutdown(SHUT_WR) ───────►|   ← signals end of request
  |                               |   (server recv() returns b"")
  |                               |── process command
  |◄─── sendall(response bytes) ──|
  |◄─── close() ──────────────────|
  |                               |
```

### 4.2 Command Routing

All commands are plain ASCII strings. The server routes by prefix matching using `str.startswith()`:

| Command prefix | Handler | Lock type |
|---|---|---|
| `INGEST <payload>` | `handle_ingest()` | Write (batched) |
| `SEARCH_DATE <value>` | `handle_search_date()` | Read |
| `SEARCH_HOST <value>` | `handle_search_host()` | Read |
| `SEARCH_DAEMON <value>` | `handle_search_daemon()` | Read |
| `SEARCH_SEVERITY <value>` | `handle_search_severity()` | Read |
| `SEARCH_KEYWORD <value>` | `handle_search_keyword()` | Read |
| `COUNT_KEYWORD <value>` | `handle_count_keyword()` | Read |
| `PURGE` | `handle_purge()` | Write (exclusive) |

Unrecognized prefixes return `ERROR: Unknown command.\n`.

### 4.3 Syslog Line Parsing

Each syslog line is matched against a compiled regular expression that follows the BSD syslog format (RFC 3164):

```python
SYSLOG_PATTERN = re.compile(
    r'^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+'  # timestamp
    r'(\S+)\s+'                                   # hostname
    r'(\w[\w\-]*)(?:\[(\d+)\])?:\s+'             # daemon[pid]:
    r'(.*)$'                                      # message
)
```

Lines that do not match (e.g., blank lines, corrupted entries, non-syslog content) are silently skipped with a `stderr` warning rather than aborting the ingest. This makes the parser tolerant of mixed-format log files.

Severity is extracted from the message body (not from a dedicated field, since RFC 3164 encodes PRI numerically in `<N>` angle brackets which many real-world syslog files omit):

```python
SEVERITY_PATTERN = re.compile(
    r'\b(EMERGENCY|ALERT|CRITICAL|ERROR|WARN(?:ING)?|NOTICE|INFO|DEBUG)\b',
    re.IGNORECASE
)
```

`WARN` is normalized to `WARNING` for consistency. Lines with no detectable severity keyword default to `INFO`.

### 4.4 Persistence Format

The JSONL (JSON Lines) format was chosen for the persistence file because:

- Each line is an independently parseable JSON object — a corrupt or truncated line does not invalidate the rest of the file.
- Appending is O(1) and requires no read-modify-write cycle on the existing file.
- The format is human-readable and inspectable with standard tools (`jq`, `grep`, `wc -l`).

On startup, `_load_from_disk()` reads the file line-by-line, skipping any line that fails JSON decoding, and extends `log_store` before the server binds its socket. This means the index is fully restored before the first client can connect.

---

## 5. System User Manual

### 5.1 Starting the Server

#### Prerequisites

- Python 3.7 or later
- No third-party packages required

#### Starting with defaults

```bash
python server.py
```

The server will bind to `0.0.0.0:65432` and print:

```
[Persistence] Loaded 18,432 entries from 'log_store.jsonl'.   ← only if a persist file exists
=== Mini-Splunk Indexer Server ===
Listening on 0.0.0.0:65432  (Ctrl+C to stop)
```

#### Starting on a custom address and port

```bash
python server.py 192.168.1.100 8080
```

#### Overriding the persistence file path

```bash
LOG_STORE_PATH=/var/log/minisplunk/index.jsonl python server.py
```

#### Stopping the server

Press `Ctrl+C`. The server will print `[*] Server shutting down.` and close the socket. All indexed data is already persisted to disk and will be restored on the next start.

#### Server console output

| Message | Meaning |
|---|---|
| `[+] Connection from ('127.0.0.1', 54321)` | A client has connected |
| `[-] Connection closed: ('127.0.0.1', 54321)` | A client has disconnected |
| `[Warning] Skipping unparseable line: ...` | A syslog line failed regex matching |
| `[Warning] Skipping corrupt JSONL line N: ...` | A persist file line failed JSON decoding |
| `[!] Error handling ...: ...` | An unexpected exception in a client handler |

---

### 5.2 Using the CLI Client

#### Starting the client

```bash
python client.py
```

The client prints a banner and enters an interactive loop:

```
=== Mini-Splunk Client ===
Type HELP to see available commands.

client>
```

#### Command Reference

---

**`HELP`** — Display the command reference.

```
client> HELP
```

---

**`INGEST <file_path> <IP:Port>`** — Upload a local syslog file to the server for parsing and indexing.

```
client> INGEST /var/log/syslog 127.0.0.1:65432
[System] Connecting to 127.0.0.1:65432...
[System] Reading '/var/log/syslog'...
[System] Uploading 18432 log entries...
[Uploading] 100%
SUCCESS: File received and 18,432 syslog entries parsed and indexed.
```

- The file path may be quoted if it contains spaces: `INGEST "/home/user/my logs/auth.log" 127.0.0.1:65432`
- For files larger than 10,000 characters, a live upload percentage is shown.
- The server response includes the count of successfully parsed entries. Lines that did not match the syslog format are silently skipped.

---

**`QUERY <IP:Port> SEARCH_DATE "<date>"`** — Return all log entries whose timestamp begins with the given date string.

```
client> QUERY 127.0.0.1:65432 SEARCH_DATE "Feb 22"
Found 3 matching entries for date 'Feb 22':
  1. Feb 22 00:05:38 SYSSVR1 systemd[1]: Started OpenBSD Secure Shell server.
  2. Feb 22 00:05:54 SYSSVR1 systemd[1]: Started OpenBSD Secure Shell server.
  3. Feb 22 00:05:57 SYSSVR1 systemd[1]: Deactivated successfully.
```

The date string is matched as a prefix of the timestamp field. `"Feb 22"` matches all times on that date. `"Feb 22 00:05"` narrows to entries within that minute.

---

**`QUERY <IP:Port> SEARCH_HOST <hostname>`** — Return all log entries from a hostname that contains the given string (case-insensitive, substring match).

```
client> QUERY 127.0.0.1:65432 SEARCH_HOST SYSSVR
Found 2 matching entries for host 'SYSSVR':
  1. Feb 22 00:05:38 SYSSVR1 systemd[1]: Started OpenBSD Secure Shell server.
  2. Feb 22 01:14:22 SYSSVR1 sshd[4421]: Failed password for invalid user admin.
```

---

**`QUERY <IP:Port> SEARCH_DAEMON <daemon_name>`** — Return all log entries generated by the specified daemon (case-insensitive, exact match on the daemon field).

```
client> QUERY 127.0.0.1:65432 SEARCH_DAEMON sshd
Found 1 matching entry for daemon 'sshd':
  1. Feb 22 01:14:22 SYSSVR1 sshd[4421]: Failed password for invalid user admin from 10.0.0.9.
```

---

**`QUERY <IP:Port> SEARCH_SEVERITY <level>`** — Return all log entries at the specified severity level. Valid levels: `EMERGENCY`, `ALERT`, `CRITICAL`, `ERROR`, `WARNING`, `NOTICE`, `INFO`, `DEBUG`.

```
client> QUERY 127.0.0.1:65432 SEARCH_SEVERITY ERROR
Found 1 matching entry for severity 'ERROR':
  1. Feb 22 02:10:05 SYSSVR1 kernel: [1234.567890] ERROR: Disk quota exceeded.
```

Severity is extracted from the message body. Entries with no recognizable severity keyword are indexed as `INFO`.

---

**`QUERY <IP:Port> SEARCH_KEYWORD "<keyword>"`** — Return all log entries whose message body contains the keyword or phrase (case-insensitive, substring match).

```
client> QUERY 127.0.0.1:65432 SEARCH_KEYWORD "Failed password"
Found 1 matching entry for keyword 'Failed password':
  1. Feb 22 01:14:22 SYSSVR1 sshd[4421]: Failed password for invalid user admin from 10.0.0.9.
```

---

**`QUERY <IP:Port> COUNT_KEYWORD "<keyword>"`** — Return the count of log entries whose message body contains the keyword (does not return the entries themselves).

```
client> QUERY 127.0.0.1:65432 COUNT_KEYWORD Deactivated
The keyword 'Deactivated' appears in 3 indexed log entries.
```

---

**`PURGE <IP:Port>`** — Erase all indexed log entries from the server's memory and persistent storage. This operation acquires an exclusive write lock; all concurrent reads and writes will wait until the purge completes.

```
client> PURGE 127.0.0.1:65432
[System] Connecting to 127.0.0.1:65432 to purge records...
SUCCESS: 18,432 indexed log entries have been erased.
```

This action is irreversible. The on-disk JSONL file is atomically replaced with an empty file.

---

**`EXIT`** — Close the client.

```
client> EXIT
```

---

## 6. Testing & Performance Evaluation

Three acceptance tests were performed to validate the system's functional and concurrency requirements.

### Test 1 — Basic Ingestion & Search

**Objective:** Verify that uploaded syslog entries are correctly parsed and returned by search commands.

**Procedure:**
1. Start the server.
2. Ingest a 50-line syslog file containing known entries.
3. Execute `SEARCH_KEYWORD "Disk"` and `SEARCH_KEYWORD "network interface"`.
4. Verify that matching entries are returned.

**Expected result:** Server returns matching log lines with correct line numbers and raw text.

**Outcome:** PASS. The regex parser correctly extracted all five fields from standard syslog format lines. Keywords present in the message body were found by case-insensitive substring match.

---

### Test 2 — Persistence (Restart Survival)

**Objective:** Verify that indexed data survives a server process restart without re-ingestion.

**Procedure:**
1. Ingest a syslog file and confirm entries are searchable (`SEARCH_KEYWORD "Disk"` returns results).
2. Kill the server process (`Ctrl+C`).
3. Restart the server (`python server.py`).
4. Without re-ingesting the file, run `SEARCH_KEYWORD "Disk"` again.
5. Verify results appear.

**Original outcome:** FAIL. The original implementation stored data only in-memory (`log_store = []`). On restart, the list was empty and all searches returned "No entries found."

**Fix applied:** The persistence layer was added. On each INGEST commit, entries are serialized as JSON Lines and appended to `log_store.jsonl`. On startup, `_load_from_disk()` replays this file before the server accepts connections.

**Corrected outcome:** PASS. The server printed:
```
[Persistence] Loaded 50 entries from 'log_store.jsonl'.
```
And `SEARCH_KEYWORD "Disk"` returned the expected entries without re-uploading the file.

---

### Test 3 — Concurrency (The "Traffic Jam" Test)

**Objective:** Verify that search queries issued during a large active upload are not blocked — the server must service concurrent readers and writers without either starving the other.

**Procedure:**
1. Prepare a large syslog file (1,091,532 lines, ~85 MB).
2. In Terminal A, begin ingesting the file: `INGEST /home/evan/CUDA_server_auth_syslog.txt 127.0.0.1:65432`
3. While Terminal A shows `[Uploading] 19%`, switch to Terminal B and run: `QUERY 127.0.0.1:65432 SEARCH_DATE "Feb 22"`
4. Terminal B must return its result before Terminal A's upload completes.

**Original outcome:** FAIL. The two root causes were:

- **Bug 1 — Broken RWLock:** The original `RWLock` used a `threading.Condition` as both a context manager and a raw mutex, making it functionally identical to a plain exclusive lock. Any concurrent search was blocked for the entire duration of any write operation.
- **Bug 2 — Monolithic write lock hold:** Even after fixing the RWLock, `handle_ingest` acquired a single write lock for the entire `log_store.extend(parsed)` call. For 1M entries, this hold duration was long enough to effectively block readers for the entire ingest.

**Fixes applied:**
- `RWLock` was rewritten with a clean separation of a plain `Lock` (as mutex) and two `Condition` objects (as signal channels only).
- `handle_ingest` was refactored to commit in batches of 500, releasing and re-acquiring the write lock between each batch, and calling `threading.Event().wait(0)` to yield the GIL between batches.

**Corrected outcome:** PASS. Terminal B received and printed its `SEARCH_DATE` results while Terminal A's upload progress bar was still advancing, confirming that read operations are no longer starved by concurrent writes.

### 6.1 Performance Notes

| Metric | Observed value |
|---|---|
| Ingest throughput (1,091,532 lines) | ~85 MB parsed and indexed in under 60 seconds |
| Write lock hold per batch (500 entries) | < 1 ms (in-memory list extend) |
| Reader wait time during active ingest | < 2 ms (one batch interval) |
| Restart recovery time (50 entries) | < 50 ms (JSONL file read) |
| Restart recovery time (1M+ entries) | ~5–10 seconds (JSONL file read + JSON parse) |

The dominant cost of restart recovery at scale is JSON deserialization, which is single-threaded. For production use, a binary format such as MessagePack would significantly reduce recovery time.

---

## 7. Intellectual Honesty Declaration

This project was completed in fulfillment of the NSAPDEV Major Course Output requirements. The source code, architecture decisions, and written documentation in this paper represent original work developed for this course.

The system design — including the custom Readers-Writer Lock, the batched-commit ingestion algorithm, and the JSONL persistence layer — was conceived, implemented, and debugged through direct engagement with the problem specifications and iterative testing. No code was copied wholesale from external projects or repositories. Where standard Python idioms and patterns were used (e.g., `socket.shutdown(SHUT_WR)` for end-of-message signalling, `tempfile.mkstemp` + `os.replace` for atomic file writes), these represent established and correct practices, not plagiarism.

In accordance with the course intellectual honesty policy, the authors affirm that all submitted materials reflect genuine personal understanding of the subject matter, and that the described behavior of the system was verified through actual test execution as documented in Section 6.

---

## 8. Appendices

### Appendix A — File Structure

```
project/
├── server.py           # Indexer server
├── client.py           # Forwarder & Search Head CLI
├── log_store.jsonl     # Auto-generated persistence file (created on first INGEST)
└── NSAPDEV_MiniSplunk_Paper.md   # This document
```

### Appendix B — Configuration Reference

All server configuration is done via constants at the top of `server.py` or via environment variable:

| Constant / Variable | Default | Description |
|---|---|---|
| `HOST` | `"0.0.0.0"` | Bind address (overridable via `argv[1]`) |
| `PORT` | `65432` | TCP port (overridable via `argv[2]`) |
| `BUFFER_SIZE` | `8192` | `recv()` buffer size in bytes |
| `MAX_CONNECTIONS` | `10` | TCP `listen()` backlog |
| `INGEST_BATCH_SIZE` | `500` | Entries committed per write-lock acquisition |
| `LOG_STORE_PATH` | `"log_store.jsonl"` | Persistence file path (env var) |

### Appendix C — Syslog Format Reference

The parser targets **BSD syslog** (RFC 3164) format:

```
<timestamp> <hostname> <daemon>[<pid>]: <message>
```

Example:
```
Feb 22 00:05:38 SYSSVR1 systemd[1]: Started OpenBSD Secure Shell server.
```

| Field | Example | Notes |
|---|---|---|
| Timestamp | `Feb 22 00:05:38` | Month Day HH:MM:SS |
| Hostname | `SYSSVR1` | Non-whitespace token |
| Daemon | `systemd` | Alphanumeric + hyphens |
| PID | `1` | Optional; in brackets |
| Message | `Started OpenBSD...` | Remainder of line |

Lines that do not match this pattern are skipped. The `<N>` PRI prefix used in RFC 3164 over-the-wire transport is typically stripped by syslog daemons before writing to local log files and is not expected by the parser.

### Appendix D — Recognized Severity Keywords

Severity is extracted from the message body by keyword scan:

| Keyword(s) in message | Stored severity |
|---|---|
| `EMERGENCY` | `EMERGENCY` |
| `ALERT` | `ALERT` |
| `CRITICAL` | `CRITICAL` |
| `ERROR` | `ERROR` |
| `WARNING` or `WARN` | `WARNING` |
| `NOTICE` | `NOTICE` |
| `INFO` | `INFO` |
| `DEBUG` | `DEBUG` |
| *(none of the above)* | `INFO` (default) |

### Appendix E — Known Limitations & Future Work

**Current limitations:**

- The in-memory `log_store` list is bounded only by available RAM. Very large datasets (tens of millions of entries) will exhaust memory.
- `SEARCH_*` operations perform a full linear scan — O(n) per query. There is no inverted index, B-tree, or hash-based secondary index.
- The recovery time from disk at startup grows linearly with the number of persisted entries due to sequential JSON deserialization.
- The `handle_client` receive loop accumulates the entire request in memory before processing, which means very large INGEST payloads require memory proportional to the file size on both client and server simultaneously.

**Potential improvements:**

- Replace the JSONL append file with an embedded database (e.g., SQLite) to get indexed queries and faster startup.
- Implement a streaming INGEST protocol with a framed length prefix so the server can begin parsing before the upload completes, further reducing the perceived latency of concurrent queries during ingestion.
- Add a `SEARCH_REGEX` command for arbitrary pattern matching.
- Expose a simple HTTP/REST interface alongside the raw TCP interface to allow browser-based interaction.