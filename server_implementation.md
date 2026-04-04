# Mini-Splunk Server — Implementation Plan (`server.py`)

---

## 1. Overview

The server acts as a centralized **syslog indexer**. It accepts TCP connections from multiple clients simultaneously, parses uploaded syslog data, stores it in a thread-safe in-memory structure, and responds to analytical queries. This plan maps directly to the project spec and the existing `client.py` protocol.

---

## 2. High-Level Architecture

```
client.py  ──TCP──►  Connection Handler Thread
                            │
                            ▼
                     Request Router
                    /       |       \
               INGEST    QUERY     PURGE
                  │         │         │
                  ▼         ▼         ▼
            Syslog        Search    Clear
            Parser        Engine    Engine
                  \         |         /
                   ▼        ▼        ▼
                  Shared In-Memory Log Store
                    (protected by RWLock)
```

---

## 3. Module Breakdown

### 3.1 Entry Point & Server Bootstrap

```python
# server.py (bottom section)
if __name__ == "__main__":
    start_server(HOST, PORT)
```

**Tasks:**
- Parse `HOST` and `PORT` from command-line arguments (`sys.argv`) with sensible defaults (e.g., `0.0.0.0:65432`).
- Create a TCP socket with `SO_REUSEADDR` enabled.
- `bind()` → `listen()` → loop `accept()`.
- On each accepted connection, spawn a new `threading.Thread` targeting the connection handler.

---

### 3.2 Connection Handler

```python
def handle_client(conn, addr):
    ...
```

**Tasks:**
- Receive the full message from the client in a loop (using `recv(8192)`) until the connection closes (mirrors `client.py`'s `shutdown(SHUT_WR)` signal).
- Decode the complete byte stream.
- Strip leading/trailing whitespace.
- Detect the command prefix (`INGEST`, `SEARCH_HOST`, `SEARCH_DATE`, `SEARCH_DAEMON`, `SEARCH_SEVERITY`, `SEARCH_KEYWORD`, `COUNT_KEYWORD`, `PURGE`) and dispatch to the appropriate handler function.
- Send the response string back via `conn.sendall()`.
- Close the connection in a `finally` block.

---

### 3.3 Shared Log Store & Thread Safety

```python
import threading

log_store = []          # list of parsed log entry dicts
store_lock = threading.RWLock()   # use threading.Lock() as fallback
```

> **Note:** Python's `threading` module does not ship with a built-in `RWLock`. Use `threading.Lock()` for simplicity, or implement a simple readers-writer lock using `threading.Condition` for better read concurrency.

**Log entry dict structure:**

| Key        | Type   | Example                        |
|------------|--------|--------------------------------|
| `raw`      | `str`  | `"Feb 22 00:05:38 SYSSVR1 systemd[1]: Started..."` |
| `timestamp`| `str`  | `"Feb 22 00:05:38"`            |
| `host`     | `str`  | `"SYSSVR1"`                   |
| `daemon`   | `str`  | `"systemd"`                   |
| `pid`      | `str`  | `"1"` (optional)               |
| `severity` | `str`  | `"ERROR"` (extracted from msg) |
| `message`  | `str`  | `"Started OpenBSD Secure Shell server"` |

**Locking rules:**
- `INGEST` and `PURGE` → acquire **write lock** (exclusive).
- All `QUERY` operations → acquire **read lock** (shared, or same lock for simplicity).

---

### 3.4 Syslog Parser

```python
import re

SYSLOG_PATTERN = re.compile(
    r'^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+'   # timestamp
    r'(\S+)\s+'                                    # hostname
    r'(\w[\w\-]*)(?:\[(\d+)\])?:\s+'              # daemon[pid]:
    r'(.*)$'                                       # message
)

SEVERITY_PATTERN = re.compile(
    r'\b(EMERGENCY|ALERT|CRITICAL|ERROR|WARN(?:ING)?|NOTICE|INFO|DEBUG)\b',
    re.IGNORECASE
)
```

**Tasks:**
- Iterate over every line of the received `INGEST` payload (after stripping the `"INGEST "` prefix).
- Skip blank lines.
- Apply `SYSLOG_PATTERN.match(line)`. On match, extract groups into a dict.
- Apply `SEVERITY_PATTERN.search(message)` to extract severity from the message body. Default to `"INFO"` if none found.
- Append the dict to `log_store` under the write lock.
- Return count of successfully parsed entries.

---

### 3.5 Command Handlers

#### `handle_ingest(payload)`
1. Strip `"INGEST "` prefix.
2. Split payload into lines.
3. Acquire write lock.
4. Parse each line and append to `log_store`.
5. Release lock.
6. Return: `"SUCCESS: File received and {n} syslog entries parsed and indexed.\n"`

#### `handle_search_host(hostname)`
1. Acquire read lock.
2. Filter `log_store` where `entry['host']` contains `hostname` (case-insensitive).
3. Release lock.
4. Format and return numbered results.

#### `handle_search_date(date_str)`
1. Acquire read lock.
2. Filter where `entry['timestamp']` starts with `date_str`.
3. Return formatted results.

#### `handle_search_daemon(daemon_name)`
1. Filter where `entry['daemon']` matches `daemon_name` (case-insensitive).

#### `handle_search_severity(severity)`
1. Filter where `entry['severity']` matches `severity` (case-insensitive).

#### `handle_search_keyword(keyword)`
1. Filter where `keyword` appears in `entry['message']` (case-insensitive).

#### `handle_count_keyword(keyword)`
1. Count matching entries (same filter as `SEARCH_KEYWORD`).
2. Return: `"The keyword '{keyword}' appears in {n} indexed log entries.\n"`

#### `handle_purge()`
1. Acquire **write lock**.
2. Record current count: `n = len(log_store)`.
3. `log_store.clear()`.
4. Release lock.
5. Return: `"SUCCESS: {n} indexed log entries have been erased.\n"`

---

### 3.6 Response Formatter

```python
def format_results(entries, label, value):
    if not entries:
        return f"No entries found for {label} '{value}'.\n"
    header = f"Found {len(entries)} matching entr{'y' if len(entries)==1 else 'ies'} for {label} '{value}':\n"
    lines = [f"  {i+1}. {e['raw']}" for i, e in enumerate(entries)]
    return header + "\n".join(lines) + "\n"
```

---

## 4. Concurrency Model

| Scenario | Mechanism |
|---|---|
| Multiple clients uploading simultaneously | Each client connection runs in its own `threading.Thread` |
| Concurrent reads (queries) | Allowed simultaneously (with `RWLock` read acquisition) |
| Write during reads (INGEST/PURGE) | Blocked until all readers release; exclusive access |
| PURGE during active INGEST | Exclusive write lock ensures atomicity |

### Simple RWLock Recipe (if needed)

```python
class RWLock:
    def __init__(self):
        self._read_ready = threading.Condition(threading.Lock())
        self._readers = 0

    def acquire_read(self):
        with self._read_ready:
            self._readers += 1

    def release_read(self):
        with self._read_ready:
            self._readers -= 1
            if self._readers == 0:
                self._read_ready.notify_all()

    def acquire_write(self):
        self._read_ready.acquire()
        while self._readers > 0:
            self._read_ready.wait()

    def release_write(self):
        self._read_ready.release()
```

---

## 5. File Structure

```
project/
├── server.py          ← to be built (this plan)
├── client.py          ← provided, do not modify
├── README.md
└── sample_logs/
    └── syslog.txt     ← for testing
```

---

## 6. Implementation Order (Step-by-Step)

| Step | Task | Est. Effort |
|------|------|-------------|
| 1 | Set up socket, `bind`, `listen`, `accept` loop | 30 min |
| 2 | Implement `handle_client` with full receive loop | 30 min |
| 3 | Build `log_store` + `RWLock` wrapper | 20 min |
| 4 | Implement syslog regex parser | 45 min |
| 5 | Implement `handle_ingest` with locking | 30 min |
| 6 | Implement all `QUERY` handlers + formatter | 60 min |
| 7 | Implement `handle_purge` with exclusive lock | 15 min |
| 8 | Wire command router in `handle_client` | 20 min |
| 9 | Test with `client.py` against sample logs | 45 min |

---

## 7. Testing Checklist

- [ ] Single client `INGEST` → correct entry count returned
- [ ] `SEARCH_HOST` returns only matching hostnames
- [ ] `SEARCH_DATE` with partial date string (`"Feb 22"`) works
- [ ] `SEARCH_DAEMON` is case-insensitive
- [ ] `SEARCH_SEVERITY ERROR` catches lines with `ERROR` in message
- [ ] `SEARCH_KEYWORD "Failed password"` matches multi-word phrases
- [ ] `COUNT_KEYWORD` returns correct aggregate
- [ ] `PURGE` clears all entries and reports correct count
- [ ] Two clients ingesting simultaneously do not corrupt `log_store`
- [ ] `PURGE` during concurrent `INGEST` does not cause a race condition

---

## 8. Edge Cases to Handle

- Lines that do not match the syslog regex → skip and log a warning to `stderr`, do not crash.
- Empty `INGEST` payload → return `"SUCCESS: 0 entries parsed."`.
- Unknown query command → return `"ERROR: Unknown command."`.
- Client disconnects mid-send → `recv` will return `b""`, exit the receive loop gracefully.
- Very large files → the chunked `send_request` in `client.py` is already handled; server just needs to keep receiving until `shutdown(SHUT_WR)`.

---

## 9. Key Constants (suggested defaults)

```python
HOST = "0.0.0.0"
PORT = 65432
BUFFER_SIZE = 8192
MAX_CONNECTIONS = 10   # passed to socket.listen()
```