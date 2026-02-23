"""
Simple TCP Test Client (Python)
Run: python tcp_client.py
"""

import socket
import threading
import sys

HOST = "127.0.0.1"
PORT = 9999


def receive(sock: socket.socket) -> None:
    """Continuously read from server and print to stdout."""
    while True:
        try:
            data = sock.recv(1024)
            if not data:
                print("\n[Disconnected from server]")
                break
            print(data.decode(errors="replace"), end="", flush=True)
        except OSError:
            break


def main() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((HOST, PORT))
        print(f"Connected to {HOST}:{PORT}. Type messages below (Ctrl+C to quit).\n")

        threading.Thread(target=receive, args=(sock,), daemon=True).start()

        try:
            while True:
                msg = input()
                sock.sendall((msg + "\n").encode())
        except KeyboardInterrupt:
            print("\nDisconnecting...")
        except BrokenPipeError:
            print("Server closed the connection.")
        sys.exit(0)


if __name__ == "__main__":
    main()