import socket
import os
import sys
import shlex

HELP_TEXT = """

Mini-Splunk Client Commands

INGEST <file_path> <IP:Port>
    Uploads a log file to the server (shows progress for large files).

QUERY <IP:Port> SEARCH_HOST <hostname>
QUERY <IP:Port> SEARCH_DATE "<date>"
QUERY <IP:Port> SEARCH_DAEMON <daemon_name>
QUERY <IP:Port> SEARCH_SEVERITY <severity_level>
QUERY <IP:Port> SEARCH_KEYWORD "<keyword>"
QUERY <IP:Port> COUNT_KEYWORD "<keyword>"

PURGE <IP:Port>
    Deletes all logs stored on the server.

HELP
    Displays this help menu.

EXIT
    Closes the client.
"""

def parse_ip_port(ip_port):
    if ":" not in ip_port:
        print("[Error] Invalid IP:Port format.")
        return None, None
    ip, port = ip_port.split(":")
    try:
        return ip, int(port)
    except ValueError:
        print("[Error] Port must be a number.")
        return None, None

def send_request(ip, port, message):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((ip, port))
            CHUNK_SIZE = 4096
            total_bytes = len(message.encode())
            sent_bytes = 0
            for i in range(0, len(message), CHUNK_SIZE):
                chunk = message[i:i+CHUNK_SIZE].encode()
                s.sendall(chunk)
                sent_bytes += len(chunk)
                # Progress bar for large files
                if total_bytes > 10000:
                    percent = int(sent_bytes / total_bytes * 100)
                    sys.stdout.write(f"\r[Uploading] {percent}%")
                    sys.stdout.flush()
            if total_bytes > 10000:
                print()  

            s.shutdown(socket.SHUT_WR)

            response = b""
            while True:
                data = s.recv(8192)
                if not data:
                    break
                response += data
        print(response.decode())
    except Exception as e:
        print(f"[Error] {e}")

def handle_ingest(parts):
    if len(parts) < 3:
        print("[Error] Invalid INGEST format.")
        return
    file_path = parts[1].strip('"')
    ip, port = parse_ip_port(parts[2])
    if not ip:
        return
    if not os.path.exists(file_path):
        print("[Error] File not found.")
        return
    print(f"[System] Connecting to {ip}:{port}...")
    print(f"[System] Reading '{file_path}'...")
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read().replace("\r\n", "\n")
    print(f"[System] Uploading {len(content.splitlines())} log entries...")
    message = "INGEST " + content
    send_request(ip, port, message)

def handle_query(parts):
    if len(parts) < 3:
        print("[Error] Invalid QUERY format.")
        return
    ip, port = parse_ip_port(parts[1])
    if not ip:
        return
    query_command = " ".join(parts[2:])
    send_request(ip, port, query_command)

def handle_purge(parts):
    if len(parts) < 2:
        print("[Error] Invalid PURGE format.")
        return
    ip, port = parse_ip_port(parts[1])
    if not ip:
        return
    print(f"[System] Connecting to {ip}:{port} to purge records...")
    send_request(ip, port, "PURGE")

def main():
    print("=== Mini-Splunk Client ===")
    print("Type HELP to see available commands.\n")
    while True:
        command = input("client> ").strip()
        if not command:
            continue
        parts = shlex.split(command)
        cmd = parts[0].upper()
        if cmd == "EXIT":
            break
        elif cmd == "HELP":
            print(HELP_TEXT)
        elif cmd == "INGEST":
            handle_ingest(parts)
        elif cmd == "QUERY":
            handle_query(parts)
        elif cmd == "PURGE":
            handle_purge(parts)
        else:
            print("[Error] Unknown command. Type HELP for instructions.")

if __name__ == "__main__":
    main()