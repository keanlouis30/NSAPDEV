###
# Server
# Rosales, Kean Louis R.
# Pinca, Evan
# NSAPDEV S30
###

import socket
import threading
import re
import sys
import json
import os
import tempfile

HOST = "0.0.0.0"
PORT = 65432
BUFFER_SIZE = 8192
MAX_CONNECTIONS = 10
MAX_RESULTS = 100
PERSIST_FILE = os.environ.get("LOG_STORE_PATH", "log_store.jsonl")

INGEST_BATCH_SIZE = 500

SYSLOG_PATTERN = re.compile(
    r'^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+'   
    r'(\S+)\s+'                                    
    r'(\w[\w\-]*)(?:\[(\d+)\])?:\s+'             
    r'(.*)$'                                      
)

SEVERITY_PATTERN = re.compile(
    r'\b(EMERGENCY|ALERT|CRITICAL|ERROR|WARN(?:ING)?|NOTICE|INFO|DEBUG)\b',
    re.IGNORECASE
)

class RWLock:
    def __init__(self):
        self._lock = threading.Lock()           
        self._no_writers = threading.Condition(self._lock)
        self._no_readers = threading.Condition(self._lock)
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0

    def acquire_read(self):
        with self._lock:
            while self._writer_active or self._writers_waiting > 0:
                self._no_writers.wait()
            self._readers += 1

    def release_read(self):
        with self._lock:
            self._readers -= 1
            if self._readers == 0:
                self._no_readers.notify_all()

    def acquire_write(self):
        with self._lock:
            self._writers_waiting += 1
            while self._readers > 0 or self._writer_active:
                self._no_readers.wait()
            self._writers_waiting -= 1
            self._writer_active = True

    def release_write(self):
        with self._lock:
            self._writer_active = False
            self._no_readers.notify_all()
            self._no_writers.notify_all()


log_store = []       
store_lock = RWLock()

def _load_from_disk():
    if not os.path.exists(PERSIST_FILE):
        return
    loaded = []
    try:
        with open(PERSIST_FILE, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    loaded.append(entry)
                except json.JSONDecodeError as e:
                    print(f"[Warning] Skipping corrupt JSONL line {lineno}: {e}",
                          file=sys.stderr)
        log_store.extend(loaded)
        print(f"[Persistence] Loaded {len(loaded):,} entries from '{PERSIST_FILE}'.")
    except OSError as e:
        print(f"[Warning] Could not read persist file '{PERSIST_FILE}': {e}",
              file=sys.stderr)


def _append_to_disk(entries):
    if not entries:
        return
    try:
        with open(PERSIST_FILE, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[Warning] Could not write to persist file '{PERSIST_FILE}': {e}",
              file=sys.stderr)


def _rewrite_disk(entries):
    dir_ = os.path.dirname(os.path.abspath(PERSIST_FILE)) or "."
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(tmp_path, PERSIST_FILE)
        except Exception:
            os.unlink(tmp_path)
            raise
    except OSError as e:
        print(f"[Warning] Could not rewrite persist file '{PERSIST_FILE}': {e}",
              file=sys.stderr)


def parse_syslog_line(line):
    line = line.strip()
    if not line:
        return None

    match = SYSLOG_PATTERN.match(line)
    if not match:
        print(f"[Warning] Skipping unparseable line: {line[:80]}...", file=sys.stderr)
        return None

    timestamp, host, daemon, pid, message = match.groups()

    sev_match = SEVERITY_PATTERN.search(message)
    severity = sev_match.group(1).upper() if sev_match else "INFO"
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

def format_results(entries, label, value):
    if not entries:
        return f"No entries found for {label} '{value}'.\n"
    count = len(entries)
    limited_entries = entries[:MAX_RESULTS]
    noun = "entry" if count == 1 else "entries"
    header = f"Found {count} matching {noun} for {label} '{value}':\n"
    lines = [f"  {i+1}. {e['raw']}" for i, e in enumerate(limited_entries)]

    if count > MAX_RESULTS:
        lines.append(f"\n[Output truncated to first {MAX_RESULTS} results]")

    return header + "\n".join(lines) + "\n"

def handle_ingest(payload):
    lines = payload.splitlines()
    total_committed = 0
    batch = []

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
            _append_to_disk(batch)
            total_committed += len(batch)
            batch = []
            threading.Event().wait(0)

    if batch:
        store_lock.acquire_write()
        try:
            log_store.extend(batch)
        finally:
            store_lock.release_write()
        _append_to_disk(batch)
        total_committed += len(batch)

    return f"SUCCESS: File received and {total_committed:,} syslog entries parsed and indexed.\n"


def handle_search_host(hostname):
    hostname_lower = hostname.lower()
    store_lock.acquire_read()
    try:
        results = [e for e in log_store if hostname_lower in e["host"].lower()]
    finally:
        store_lock.release_read()
    return format_results(results, "host", hostname)


def handle_search_date(date_str):
    store_lock.acquire_read()
    try:
        results = [e for e in log_store if e["timestamp"].startswith(date_str)]
    finally:
        store_lock.release_read()
    return format_results(results, "date", date_str)


def handle_search_daemon(daemon_name):
    daemon_lower = daemon_name.lower()
    store_lock.acquire_read()
    try:
        results = [e for e in log_store if e["daemon"].lower() == daemon_lower]
    finally:
        store_lock.release_read()
    return format_results(results, "daemon", daemon_name)


def handle_search_severity(severity):
    severity_upper = severity.upper()
    store_lock.acquire_read()
    try:
        results = [e for e in log_store if e["severity"] == severity_upper]
    finally:
        store_lock.release_read()
    return format_results(results, "severity", severity)


def handle_search_keyword(keyword):
    keyword_lower = keyword.lower()
    store_lock.acquire_read()
    try:
        results = [e for e in log_store if keyword_lower in e["message"].lower()]
    finally:
        store_lock.release_read()
    return format_results(results, "keyword", keyword)


def handle_count_keyword(keyword):
    keyword_lower = keyword.lower()
    store_lock.acquire_read()
    try:
        count = sum(1 for e in log_store if keyword_lower in e["message"].lower())
    finally:
        store_lock.release_read()
    noun = "entry" if count == 1 else "entries"
    return f"The keyword '{keyword}' appears in {count} indexed log {noun}.\n"


def handle_purge():
    store_lock.acquire_write()
    try:
        n = len(log_store)
        log_store.clear()
    finally:
        store_lock.release_write()

    _rewrite_disk([])   
    return f"SUCCESS: {n:,} indexed log entries have been erased.\n"

def handle_client(conn, addr):
    print(f"[+] Connection from {addr}")
    try:
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

def start_server(host, port):
    _load_from_disk()   

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
