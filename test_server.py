"""
Multi-Client TCP Server (Python)
---------------------------------
Uses the `threading` module to handle each client in its own thread.
Run: python tcp_server.py
Test: telnet localhost 9999  OR  python tcp_client.py
"""

import socket
import threading
import logging

# ── Configuration ────────────────────────────────────────────────────────────
HOST = "0.0.0.0"   # Listen on all interfaces
PORT = 9999
BUFFER_SIZE = 1024
MAX_CONNECTIONS = 10

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# ── Shared state (thread-safe) ────────────────────────────────────────────────
clients: dict[socket.socket, str] = {}   # socket → address string
clients_lock = threading.Lock()


def broadcast(message: str, sender: socket.socket | None = None) -> None:
    """Send a message to every connected client except the sender."""
    with clients_lock:
        targets = [(sock, addr) for sock, addr in clients.items() if sock is not sender]

    for sock, addr in targets:
        try:
            sock.sendall(message.encode())
        except OSError:
            log.warning("Failed to send to %s", addr)


def handle_client(conn: socket.socket, addr: tuple) -> None:
    """Thread target: read from a client and echo/broadcast its messages."""
    addr_str = f"{addr[0]}:{addr[1]}"
    log.info("Connected: %s", addr_str)

    with clients_lock:
        clients[conn] = addr_str

    broadcast(f"[Server] {addr_str} joined the chat.\n", sender=conn)

    try:
        while True:
            data = conn.recv(BUFFER_SIZE)
            if not data:
                break  # Client disconnected gracefully

            message = data.decode(errors="replace").strip()
            log.info("Received from %s: %s", addr_str, message)

            # Echo back to sender
            conn.sendall(f"[You]: {message}\n".encode())

            # Broadcast to all others
            broadcast(f"[{addr_str}]: {message}\n", sender=conn)

    except (ConnectionResetError, OSError) as exc:
        log.warning("Connection error with %s: %s", addr_str, exc)
    finally:
        with clients_lock:
            clients.pop(conn, None)
        conn.close()
        log.info("Disconnected: %s", addr_str)
        broadcast(f"[Server] {addr_str} left the chat.\n")


def start_server() -> None:
    """Create the server socket, accept clients, and spawn handler threads."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(MAX_CONNECTIONS)
        log.info("Server listening on %s:%d", HOST, PORT)

        try:
            while True:
                conn, addr = server.accept()
                thread = threading.Thread(
                    target=handle_client,
                    args=(conn, addr),
                    name=f"Client-{addr[0]}:{addr[1]}",
                    daemon=True,   # Threads die when main thread exits
                )
                thread.start()
                log.info("Active threads: %d", threading.active_count() - 1)

        except KeyboardInterrupt:
            log.info("Server shutting down.")


if __name__ == "__main__":
    start_server()