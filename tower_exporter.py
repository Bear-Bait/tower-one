#!/usr/bin/env python3
"""
tower_exporter.py — Prometheus metrics for WGXC tower-two broadcast machine.

Exposes audio-engine and service-health metrics that node_exporter doesn't cover.
Run as: systemd service, port 9200.

Metric philosophy: NO noise. Every metric here should be actionable.
  - Liquidsoap engine state (what's on air, is it alive)
  - Service crash tracking (how many restarts)
  - Upstream stream reachability (can we reach audio.wavefarm.org)
  - Harbor monitor stream health (is the browser feed alive)

System metrics (CPU, memory, disk, temperature, SMART) are handled by
node_exporter on port 9100 — do not duplicate them here.
"""

import socket
import subprocess
import json
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

LIQUIDSOAP_PORT = 1234
EXPORTER_PORT   = 9200
UPSTREAM_HOST   = "audio.wavefarm.org"
UPSTREAM_PORT   = 8000
HARBOR_PORT     = 8001


def liq_multi(cmds):
    """Send multiple commands in one telnet connection. Returns list of response strings."""
    try:
        with socket.create_connection(("localhost", LIQUIDSOAP_PORT), timeout=3) as s:
            s.settimeout(2)
            buf = b""
            try:
                while b"commands." not in buf:
                    buf += s.recv(4096)
            except socket.timeout:
                pass
            results = []
            for cmd in cmds:
                s.sendall(f"{cmd}\n".encode())
                res = b""
                try:
                    while b"\nEND" not in res:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        res += chunk
                except (socket.timeout, ConnectionResetError):
                    pass
                results.append(res.decode(errors="replace").split("\nEND")[0].strip())
            try:
                s.sendall(b"exit\n")
            except OSError:
                pass
            return results
    except Exception:
        return [None] * len(cmds)


def collect():
    lines = []

    def g(name, value, labels=None, help_text=None, metric_type="gauge"):
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {metric_type}")
        label_str = ""
        if labels:
            label_str = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        lines.append(f"{name}{label_str} {value}")

    # ── Liquidsoap engine ──────────────────────────────────────────────────────
    results = liq_multi(["radio.selected", "audio.get_volume"])
    selected, volume = results[0], results[1]

    liq_up = 1 if selected and not selected.startswith("Error") else 0
    g("tower_liquidsoap_up", liq_up,
      help_text="1 if Liquidsoap telnet is responsive, 0 if down")

    if liq_up:
        for src in ("automation", "live", "emergency"):
            g("tower_liquidsoap_source_active", 1 if selected == src else 0,
              labels={"source": src},
              help_text="1 for the currently selected broadcast source")
        try:
            g("tower_liquidsoap_master_volume", float(volume),
              help_text="Liquidsoap master volume (0.0–1.0+)")
        except (TypeError, ValueError):
            pass
    else:
        # Explicitly report 0 for all sources when engine is down
        for src in ("automation", "live", "emergency"):
            g("tower_liquidsoap_source_active", 0, labels={"source": src})

    # ── Service restart count ──────────────────────────────────────────────────
    # Rising count = repeated crashes. Alert if this climbs.
    try:
        r = subprocess.run(
            ["systemctl", "show", "liquidsoap-tower", "--property=NRestarts"],
            capture_output=True, text=True, timeout=3
        )
        restarts = int(r.stdout.strip().split("=")[1])
        g("tower_liquidsoap_restarts_total", restarts,
          help_text="Cumulative liquidsoap-tower service restarts since boot",
          metric_type="counter")
    except Exception:
        pass

    # ── Upstream stream reachability ───────────────────────────────────────────
    # Can we TCP-connect to audio.wavefarm.org:8000?
    # One check covers both streams (same host). Alert if 0.
    try:
        with socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=3):
            upstream_up = 1
    except Exception:
        upstream_up = 0
    g("tower_upstream_reachable", upstream_up,
      labels={"host": UPSTREAM_HOST},
      help_text="1 if audio.wavefarm.org:8000 is TCP-reachable from tower-two")

    # ── Harbor monitor stream ──────────────────────────────────────────────────
    # Is the browser monitor feed (port 8001) serving a 200 response?
    try:
        with socket.create_connection(("localhost", HARBOR_PORT), timeout=2) as s:
            s.sendall(b"GET /monitor.mp3 HTTP/1.0\r\nHost: localhost\r\n\r\n")
            resp = s.recv(256)
            harbor_up = 1 if b"200" in resp else 0
    except Exception:
        harbor_up = 0
    g("tower_harbor_stream_up", harbor_up,
      help_text="1 if harbor monitor stream (port 8001) is serving HTTP 200")

    # ── Dashboard API ──────────────────────────────────────────────────────────
    try:
        with urllib.request.urlopen("http://localhost:8080/api/status", timeout=3) as r:
            dashboard_up = 1 if r.status == 200 else 0
    except Exception:
        dashboard_up = 0
    g("tower_dashboard_up", dashboard_up,
      help_text="1 if Flask dashboard (port 8080) is responding")

    # ── ALSA output active ────────────────────────────────────────────────────
    # PipeWire sits between Liquidsoap and the hardware — lsof/fuser on PCM
    # device files will never show Liquidsoap. Instead: confirm PipeWire is
    # running (it owns the DAC) AND Liquidsoap has live audio (RMS > floor).
    try:
        pw = subprocess.run(["pgrep", "-x", "pipewire"], capture_output=True, timeout=3)
        pipewire_up = pw.returncode == 0
    except Exception:
        pipewire_up = False

    try:
        rms_raw = liq_multi(["audio.get_rms"])[0] or ""
        rms = float(rms_raw.strip())
    except (TypeError, ValueError):
        rms = 0.0

    alsa_active = 1 if (pipewire_up and rms > 0.001) else 0
    g("tower_alsa_output_active", alsa_active,
      help_text="1 if Liquidsoap holds the ALSA output device (audio flowing to transmitter)")

    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            try:
                body = collect().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
        elif self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"tower_exporter OK - /metrics")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # Suppress per-request access logs — Prometheus scrapes every 15s


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", EXPORTER_PORT), Handler)
    print(f"tower_exporter listening on :{EXPORTER_PORT}/metrics")
    server.serve_forever()
