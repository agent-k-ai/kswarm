from __future__ import annotations

import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class WorkerMetrics:
    prefix: str
    counters: dict[str, int] = field(default_factory=dict)

    def inc(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def render(self) -> bytes:
        lines: list[str] = []
        for key in sorted(self.counters):
            metric = f"{self.prefix}_{key}_total"
            lines.append(f"# TYPE {metric} counter")
            lines.append(f"{metric} {self.counters[key]}")
        return ("\n".join(lines) + "\n").encode("utf-8")


def start_metrics_server(metrics: WorkerMetrics, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            payload = metrics.render()
            self.send_response(200)
            self.send_header("content-type", "text/plain; version=0.0.4")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server

