"""Private Unix-socket fault proxy; forwards real Docker responses unchanged."""

import http.client
import http.server
import os
import socket
import socketserver
import threading


class DockerObservationProxy:
    def __init__(self, path):
        self.block = threading.Event()
        self.timeouts = 0
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def forward(self):
                if (
                    self.command == "GET"
                    and "/containers/" in self.path
                    and self.path.endswith("/json")
                    and parent.block.is_set()
                ):
                    parent.timeouts += 1
                    # Docker client has a real five-second read timeout.
                    threading.Event().wait(6)
                    self.close_connection = True
                    return
                conn = http.client.HTTPConnection("localhost", timeout=10)
                conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.sock.settimeout(10)
                conn.sock.connect("/var/run/docker.sock")
                try:
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    headers = {
                        k: v
                        for k, v in self.headers.items()
                        if k.lower() not in {"connection", "host"}
                    }
                    conn.request(self.command, self.path, body=body, headers=headers)
                    response = conn.getresponse()
                    data = response.read(16 * 1024 * 1024)
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {
                            "connection",
                            "transfer-encoding",
                            "content-length",
                        }:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    conn.close()

            do_GET = do_POST = do_DELETE = forward

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(str(path), Handler)
        os.chmod(path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
