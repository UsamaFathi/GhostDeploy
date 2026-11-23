"""Synthetic loopback service with deterministic startup and health failures."""

import argparse
import configparser
import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", type=Path)
    parser.add_argument("--version", action="version", version="Synthetic Odoo 19.0")
    args = parser.parse_args()
    config = configparser.ConfigParser()
    config.read(args.c)
    owned = Path(config["fixture"]["work"])
    mode = (owned / "mode").read_text().strip()
    if mode == "failed-start":
        raise SystemExit(42)

    class Probe(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(503 if mode == "bad-health" else 200)
            self.end_headers()

        def log_message(self, *_args) -> None:
            pass

    with HTTPServer(("127.0.0.1", int((owned / "port").read_text())), Probe) as server:
        if mode != "startup-timeout":
            notify = os.environ["NOTIFY_SOCKET"]
            address = "\0" + notify[1:] if notify.startswith("@") else notify
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
                client.sendto(b"READY=1", address)
        server.serve_forever()


if __name__ == "__main__":
    main()
