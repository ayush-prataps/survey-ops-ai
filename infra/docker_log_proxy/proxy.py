#!/usr/bin/env python3
"""
Read-Only Docker Log Proxy.
Provides minimal read-only access strictly for container stdout/stderr logs
of allowlisted target application containers.

Blocks:
- Any access to toxiproxy, postgres, prometheus, alertmanager, grafana, or runner containers.
- Any POST, PUT, DELETE, PATCH, EXEC, BUILD operations (only GET allowed).
- Filters /containers/json listing to allowed containers only.
"""

import http.server
import json
import logging
import os
import re
import socket
import sys
import urllib.parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("docker_log_proxy")

DOCKER_SOCKET_PATH = os.environ.get("DOCKER_SOCKET_PATH", "/var/run/docker.sock")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "2375"))

ALLOWED_CONTAINER_NAMES = {
    "target_orders",
    "target_payment",
    "target_inventory",
    "target_auth",
    "orders",
    "payment",
    "inventory",
    "auth",
}

FORBIDDEN_CONTAINER_NAMES = {
    "target_toxiproxy",
    "toxiproxy",
    "target_postgres",
    "postgres",
    "target_prometheus",
    "prometheus",
    "target_alertmanager",
    "alertmanager",
    "target_grafana",
    "grafana",
    "surveyops_api",
    "surveyops_runner",
    "runner",
    "docker_log_proxy",
}


def _dechunk(data: bytes) -> bytes:
    """Dechunks HTTP chunked transfer encoded bytes."""
    result = bytearray()
    while data:
        idx = data.find(b"\r\n")
        if idx == -1:
            break
        chunk_len_hex = data[:idx].split(b";")[0].strip()
        if not chunk_len_hex:
            break
        try:
            chunk_len = int(chunk_len_hex, 16)
        except ValueError:
            break
        if chunk_len == 0:
            break
        chunk_data = data[idx + 2 : idx + 2 + chunk_len]
        result.extend(chunk_data)
        data = data[idx + 2 + chunk_len + 2 :]
    return bytes(result)


def _connect_docker_socket() -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(DOCKER_SOCKET_PATH)
    return sock


def _forward_raw(method: str, full_path: str, headers: dict) -> tuple[int, dict, bytes]:
    """Forward an HTTP request to Docker unix domain socket and return (status, headers, body)."""
    sock = _connect_docker_socket()
    hdr_lines = [f"{method} {full_path} HTTP/1.1", "Host: localhost", "Connection: close"]
    for k, v in headers.items():
        if k.lower() not in ("host", "connection"):
            hdr_lines.append(f"{k}: {v}")
    raw_req = "\r\n".join(hdr_lines) + "\r\n\r\n"
    sock.sendall(raw_req.encode("utf-8"))

    # Read complete response
    resp_chunks = []
    while True:
        data = sock.recv(16384)
        if not data:
            break
        resp_chunks.append(data)
    sock.close()

    raw_resp = b"".join(resp_chunks)
    header_end = raw_resp.find(b"\r\n\r\n")
    if header_end == -1:
        return 500, {}, b'{"error": "Invalid docker daemon response"}'

    header_bytes = raw_resp[:header_end]
    body_bytes = raw_resp[header_end + 4 :]

    lines = header_bytes.decode("iso-8859-1").split("\r\n")
    status_line = lines[0]
    status_code = int(status_line.split(" ")[1])

    resp_headers = {}
    for line in lines[1:]:
        if ":" in line:
            hk, hv = line.split(":", 1)
            resp_headers[hk.strip().lower()] = hv.strip()

    if resp_headers.get("transfer-encoding") == "chunked":
        body_bytes = _dechunk(body_bytes)
        resp_headers.pop("transfer-encoding", None)
        resp_headers["content-length"] = str(len(body_bytes))

    return status_code, resp_headers, body_bytes


def _get_container_name_map() -> dict[str, str]:
    """Builds a mapping from container ID/prefix to container name."""
    try:
        status, _, body = _forward_raw("GET", "/containers/json?all=1", {})
        if status != 200:
            return {}
        containers = json.loads(body.decode("utf-8"))
        id_to_name = {}
        for c in containers:
            cid = c.get("Id", "")
            names = [n.lstrip("/") for n in c.get("Names", [])]
            for n in names:
                id_to_name[cid] = n
                id_to_name[cid[:12]] = n
                id_to_name[n] = n
        return id_to_name
    except Exception as exc:
        logger.error(f"Failed to query container name map: {exc}")
        return {}


class DockerLogProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path

        # 1. Ping / version / info (allow)
        if path.endswith("/_ping") or path.endswith("/version"):
            status, hdrs, body = _forward_raw("GET", self.path, dict(self.headers))
            self.send_response(status)
            for k, v in hdrs.items():
                if k.lower() in ("content-type", "content-length"):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            return

        # 2. Container listing: filter to ALLOWED containers only
        if "/containers/json" in path:
            status, hdrs, body = _forward_raw("GET", self.path, dict(self.headers))
            if status != 200:
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                containers = json.loads(body.decode("utf-8"))
                filtered = [
                    c for c in containers
                    if any(n.lstrip("/") in ALLOWED_CONTAINER_NAMES for n in c.get("Names", []))
                ]
                filtered_body = json.dumps(filtered).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(filtered_body)))
                self.end_headers()
                self.wfile.write(filtered_body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        # 3. Inspecting or reading logs of a specific container: /containers/{id}/...
        container_match = re.search(r"/containers/([^/]+)/", path)
        if not container_match:
            container_match = re.search(r"/containers/([^/]+)$", path)

        if container_match:
            raw_id = container_match.group(1)
            name_map = _get_container_name_map()
            resolved_name = name_map.get(raw_id, raw_id)

            # Strict guardrail check
            is_allowed = (resolved_name in ALLOWED_CONTAINER_NAMES) or (raw_id in ALLOWED_CONTAINER_NAMES)
            is_forbidden = (resolved_name in FORBIDDEN_CONTAINER_NAMES) or (raw_id in FORBIDDEN_CONTAINER_NAMES)

            if is_forbidden or not is_allowed:
                logger.warning(f"BLOCKED access attempt to container '{raw_id}' (resolved: '{resolved_name}')")
                err_body = json.dumps({
                    "message": f"Access to container '{raw_id}' is forbidden by Docker Log Proxy policy",
                    "status": "forbidden",
                }).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
                return

            # Allowed container: forward request
            status, hdrs, body = _forward_raw("GET", self.path, dict(self.headers))
            self.send_response(status)
            for k, v in hdrs.items():
                if k.lower() in ("content-type", "content-length"):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            return

        # 4. Any other endpoint blocked
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        err_msg = json.dumps({"error": f"Endpoint '{path}' is forbidden by read-only log proxy"}).encode("utf-8")
        self.send_header("Content-Length", str(len(err_msg)))
        self.end_headers()
        self.wfile.write(err_msg)

    def do_POST(self):
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error": "POST operations are forbidden on read-only log proxy"}')

    def do_DELETE(self):
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error": "DELETE operations are forbidden on read-only log proxy"}')


def main():
    logger.info(f"Starting Docker Log Proxy on 0.0.0.0:{PROXY_PORT} targeting {DOCKER_SOCKET_PATH}...")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), DockerLogProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Docker Log Proxy...")
        server.server_close()


if __name__ == "__main__":
    main()
