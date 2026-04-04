import socket
import threading
import re
import sys

# ─── Configuration & Constants ────────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 65432
BUFFER_SIZE = 8192
MAX_CONNECTIONS = 10

# ─── Syslog Regex Patterns ────────────────────────────────────────────────────
SYSLOG_PATTERN = re.compile(
    r'^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+'   # timestamp  (e.g. "Feb 22 00:05:38")
    r'(\S+)\s+'                                    # hostname   (e.g. "SYSSVR1")
    r'(\w[\w\-]*)(?:\[(\d+)\])?:\s+'              # daemon[pid]: (e.g. "systemd[1]:")
    r'(.*)$'                                       # message
)

SEVERITY_PATTERN = re.compile(
    r'\b(EMERGENCY|ALERT|CRITICAL|ERROR|WARN(?:ING)?|NOTICE|INFO|DEBUG)\b',
    re.IGNORECASE
)

# ─── Readers-Writer Lock ──────────────────────────────────────────────────────
class RWLock:
    """A simple readers-writer lock built on threading.Condition.

    Multiple readers may hold the lock concurrently, but a writer requires
    exclusive access.  Writers are given priority once they begin waiting so
    that a continuous stream of readers cannot starve a writer.
    """

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

# ─── Shared In-Memory Log Store ───────────────────────────────────────────────
log_store = []        # list of parsed log-entry dicts
store_lock = RWLock()

# ─── Syslog Parser ────────────────────────────────────────────────────────────
def parse_syslog_line(line):
    """Parse a single syslog line into a dict, or return None on failure."""
    line = line.strip()
    if not line:
        return None

    match = SYSLOG_PATTERN.match(line)
    if not match:
        print(f"[Warning] Skipping unparseable line: {line[:80]}...", file=sys.stderr)
        return None

    timestamp, host, daemon, pid, message = match.groups()

    # Extract severity from the message body; default to INFO
    sev_match = SEVERITY_PATTERN.search(message)
    severity = sev_match.group(1).upper() if sev_match else "INFO"
    # Normalize "WARN" → "WARNING"
    if severity == "WARN":
        severity = "WARNING"

    return {
        "raw":       line,
        "timestamp": timestamp,
        "host":      host,
        "daemon":    daemon,
        "pid":       pid or "",
        "severity":  severity,
        "message":   message,
    }

# ─── Response Formatter ──────────────────────────────────────────────────────
def format_results(entries, label, value):
    """Return a human-readable numbered list of matching log entries."""
    if not entries:
        return f"No entries found for {label} '{value}'.\n"
    count = len(entries)
    noun = "entry" if count == 1 else "entries"
    header = f"Found {count} matching {noun} for {label} '{value}':\n"
    lines = [f"  {i+1}. {e['raw']}" for i, e in enumerate(entries)]
    return header + "\n".join(lines) + "\n"

# ─── Command Handlers ─────────────────────────────────────────────────────────
def handle_ingest(payload):
    """Parse syslog lines from *payload* and append them to the shared store."""
    lines = payload.splitlines()
    parsed = []
    for line in lines:
        entry = parse_syslog_line(line)
        if entry is not None:
            parsed.append(entry)

    store_lock.acquire_write()
    try:
        log_store.extend(parsed)
    finally:
        store_lock.release_write()

    n = len(parsed)
    return f"SUCCESS: File received and {n:,} syslog entries parsed and indexed.\n"


def handle_search_host(hostname):
    """Return all entries whose hostname contains *hostname* (case-insensitive)."""
    hostname_lower = hostname.lower()
    store_lock.acquire_read()
    try:
        results = [e for e in log_store if hostname_lower in e["host"].lower()]
    finally:
        store_lock.release_read()
    return format_results(results, "host", hostname)


def handle_search_date(date_str):
    """Return entries whose timestamp starts with *date_str*."""
    store_lock.acquire_read()
    try:
        results = [e for e in log_store if e["timestamp"].startswith(date_str)]
    finally:
        store_lock.release_read()
    return format_results(results, "date", date_str)


def handle_search_daemon(daemon_name):
    """Return entries whose daemon matches *daemon_name* (case-insensitive)."""
    daemon_lower = daemon_name.lower()
    store_lock.acquire_read()
    try:
        results = [e for e in log_store if e["daemon"].lower() == daemon_lower]
    finally:
        store_lock.release_read()
    return format_results(results, "daemon", daemon_name)


def handle_search_severity(severity):
    """Return entries whose severity matches *severity* (case-insensitive)."""
    severity_upper = severity.upper()
    store_lock.acquire_read()
    try:
        results = [e for e in log_store if e["severity"] == severity_upper]
    finally:
        store_lock.release_read()
    return format_results(results, "severity", severity)


def handle_search_keyword(keyword):
    """Return entries whose message contains *keyword* (case-insensitive)."""
    keyword_lower = keyword.lower()
    store_lock.acquire_read()
    try:
        results = [e for e in log_store if keyword_lower in e["message"].lower()]
    finally:
        store_lock.release_read()
    return format_results(results, "keyword", keyword)


def handle_count_keyword(keyword):
    """Return a count of entries whose message contains *keyword*."""
    keyword_lower = keyword.lower()
    store_lock.acquire_read()
    try:
        count = sum(1 for e in log_store if keyword_lower in e["message"].lower())
    finally:
        store_lock.release_read()
    noun = "entry" if count == 1 else "entries"
    return f"The keyword '{keyword}' appears in {count} indexed log {noun}.\n"


def handle_purge():
    """Erase all indexed log entries under an exclusive write lock."""
    store_lock.acquire_write()
    try:
        n = len(log_store)
        log_store.clear()
    finally:
        store_lock.release_write()
    return f"SUCCESS: {n:,} indexed log entries have been erased.\n"

# ─── Connection Handler ──────────────────────────────────────────────────────
def handle_client(conn, addr):
    """Receive a full request from the client, route it, and send a response."""
    print(f"[+] Connection from {addr}")
    try:
        # Receive all data until the client signals shutdown(SHUT_WR)
        chunks = []
        while True:
            data = conn.recv(BUFFER_SIZE)
            if not data:
                break
            chunks.append(data)
        raw_message = b"".join(chunks).decode("utf-8").strip()

        if not raw_message:
            conn.sendall(b"ERROR: Empty request received.\n")
            return

        # ── Route by command prefix ──────────────────────────────────────
        if raw_message.startswith("INGEST "):
            payload = raw_message[len("INGEST "):]
            response = handle_ingest(payload)

        elif raw_message.startswith("SEARCH_DATE "):
            value = raw_message[len("SEARCH_DATE "):].strip().strip('"')
            response = handle_search_date(value)

        elif raw_message.startswith("SEARCH_HOST "):
            value = raw_message[len("SEARCH_HOST "):].strip().strip('"')
            response = handle_search_host(value)

        elif raw_message.startswith("SEARCH_DAEMON "):
            value = raw_message[len("SEARCH_DAEMON "):].strip().strip('"')
            response = handle_search_daemon(value)

        elif raw_message.startswith("SEARCH_SEVERITY "):
            value = raw_message[len("SEARCH_SEVERITY "):].strip().strip('"')
            response = handle_search_severity(value)

        elif raw_message.startswith("SEARCH_KEYWORD "):
            value = raw_message[len("SEARCH_KEYWORD "):].strip().strip('"')
            response = handle_search_keyword(value)

        elif raw_message.startswith("COUNT_KEYWORD "):
            value = raw_message[len("COUNT_KEYWORD "):].strip().strip('"')
            response = handle_count_keyword(value)

        elif raw_message == "PURGE":
            response = handle_purge()

        else:
            response = "ERROR: Unknown command.\n"

        conn.sendall(response.encode("utf-8"))

    except Exception as e:
        try:
            conn.sendall(f"ERROR: {e}\n".encode("utf-8"))
        except OSError:
            pass
        print(f"[!] Error handling {addr}: {e}", file=sys.stderr)
    finally:
        conn.close()
        print(f"[-] Connection closed: {addr}")

# ─── Server Bootstrap ─────────────────────────────────────────────────────────
def start_server(host, port):
    """Create, bind, and listen on a TCP socket; spawn a thread per client."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(MAX_CONNECTIONS)
    print(f"=== Mini-Splunk Indexer Server ===")
    print(f"Listening on {host}:{port}  (Ctrl+C to stop)\n")

    try:
        while True:
            conn, addr = server.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\n[*] Server shutting down.")
    finally:
        server.close()

# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    host = HOST
    port = PORT
    if len(sys.argv) >= 2:
        host = sys.argv[1]
    if len(sys.argv) >= 3:
        try:
            port = int(sys.argv[2])
        except ValueError:
            print(f"[Error] Invalid port: {sys.argv[2]}", file=sys.stderr)
            sys.exit(1)
    start_server(host, port)