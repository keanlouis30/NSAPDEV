import socket
import threading
import sqlite3
import re
import os

# --- Configuration ---
HOST = '0.0.0.0'
PORT = 65432
DB_FILE = 'syslog_data.db'

# --- Database setup ---
def initialize_db():
    """Initializes the database and the table for storing logs."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            hostname TEXT,
            process TEXT,
            severity TEXT,
            message TEXT
        )
    ''')
    conn.commit()
    conn.close()

def parse_syslog(line):
    """Parses a standard syslog line into its components."""
    # Example format: Feb 22 01:14:22 SYSSVR1 sshd[4421]: Failed password for invalid user admin
    pattern = r'^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+(\S+)\s+([^:\[\s]+)(?:\[(\d+)\])?:\s*(.*)$'
    match = re.match(pattern, line)
    if match:
        timestamp, hostname, process_name, pid, message = match.groups()
        severity = 'INFO'
        if 'error' in message.lower() or 'fail' in message.lower():
            severity = 'ERROR'
        elif 'warn' in message.lower():
            severity = 'WARNING'
        return (timestamp, hostname, process_name, severity, message)
    return None

def handle_client(conn, addr):
    """Handles communication with a single client."""
    try:
        data = conn.recv(1024 * 1024).decode('utf-8')
        if not data:
            return

        parts = data.split(' ', 1)
        command = parts[0].upper()

        if command == 'INGEST':
            log_entries = parts[1].strip().splitlines()
            db = sqlite3.connect(DB_FILE)
            cursor = db.cursor()
            count = 0
            for line in log_entries:
                parsed = parse_syslog(line)
                if parsed:
                    cursor.execute('''
                        INSERT INTO logs (timestamp, hostname, process, severity, message)
                        VALUES (?, ?, ?, ?, ?)
                    ''', parsed)
                    count += 1
            db.commit()
            db.close()
            conn.sendall(f"SUCCESS: {count} entries indexed.".encode('utf-8'))

        elif command == 'QUERY':
            q_parts = parts[1].split(' ', 1)
            q_type = q_parts[0].upper()
            q_val = q_parts[1].strip('"')

            db = sqlite3.connect(DB_FILE)
            cursor = db.cursor()
            
            query_map = {
                'SEARCH_DATE': "SELECT * FROM logs WHERE timestamp LIKE ?",
                'SEARCH_HOST': "SELECT * FROM logs WHERE hostname = ?",
                'SEARCH_DAEMON': "SELECT * FROM logs WHERE process = ?",
                'SEARCH_SEVERITY': "SELECT * FROM logs WHERE severity = ?",
                'SEARCH_KEYWORD': "SELECT * FROM logs WHERE message LIKE ?",
                'COUNT_KEYWORD': "SELECT COUNT(*) FROM logs WHERE message LIKE ?"
            }

            if q_type in query_map:
                param = f"%{q_val}%" if 'KEYWORD' in q_type or 'DATE' in q_type else q_val
                cursor.execute(query_map[q_type], (param,))
                results = cursor.fetchall()
                
                if q_type == 'COUNT_KEYWORD':
                    conn.sendall(f"The keyword '{q_val}' appears in {results[0][0]} indexed log entry.".encode('utf-8'))
                else:
                    response = "\n".join([f"{r[1]} {r[2]} {r[3]}: {r[5]}" for r in results])
                    conn.sendall(response.encode('utf-8') if response else "No matches found.".encode('utf-8'))
            db.close()

        elif command == 'PURGE':
            db = sqlite3.connect(DB_FILE)
            cursor = db.cursor()
            cursor.execute("DELETE FROM logs")
            db.commit()
            db.close()
            conn.sendall("SUCCESS: All logs purged.".encode('utf-8'))

    except Exception as e:
        conn.sendall(f"ERROR: {str(e)}".encode('utf-8'))
    finally:
        conn.close()

def main():
    initialize_db()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(5)
    print(f"Indexer Server listening on port {PORT}...")

    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_client, args=(conn, addr)).start()

if __name__ == "__main__":
    main()
