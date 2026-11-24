#!/usr/bin/env python3
"""Servicio web containerizado de telemetria CanSat (balanceo de cargas / cluster).

Disenado para ejecutarse dentro de Docker y Kubernetes sin dependencias de GUI
(PyQt5/OpenGL). Expone:
  - GET  /              Dashboard web (incluye ID de replica para validar balanceo)
  - GET  /health        Sonda liveness/readiness para Kubernetes y Nginx
  - GET  /api/metrics   Ultimo paquete y metricas acumuladas en JSON
  - POST /api/telemetry Ingesta de tramas de telemetria en JSON
  - GET  /events        Stream Server-Sent Events (SSE) en tiempo real
"""

from __future__ import annotations

import json
import math
import os
import queue
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
SIM_MODE = os.environ.get("SIM_MODE", "1") == "1"
REPLICA_ID = os.environ.get("HOSTNAME", socket.gethostname())
START_TIME = time.time()

_lock = threading.Lock()
_clients: list[queue.Queue[str]] = []
_latest: dict = {
    "mission_time_s": 0.0,
    "altitude_m": 0.0,
    "velocity_ms": 0.0,
    "accel_ms2": 0.0,
    "temperature_c": 24.0,
    "pressure_hpa": 1013.25,
    "voltage_v": 8.4,
    "pitch_deg": 0.0,
    "roll_deg": 0.0,
    "yaw_deg": 0.0,
    "state": 0,
    "satellite": 0,
    "chute": 0,
    "packet": 0,
    "rssi_dbm": -55.0,
    "snr_db": 9.5,
    "replica": REPLICA_ID,
}
_max_altitude = 0.0
_total_packets = 0


def _publish(packet: dict) -> None:
    global _latest, _max_altitude, _total_packets
    packet["replica"] = REPLICA_ID
    payload = json.dumps(packet, ensure_ascii=False)
    with _lock:
        _latest = packet
        _total_packets += 1
        alt = float(packet.get("altitude_m", 0.0))
        if alt > _max_altitude:
            _max_altitude = alt
        for q in list(_clients):
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except queue.Empty:
                    pass


def _simulator_loop() -> None:
    """Genera un perfil de vuelo CanSat sintetico (450 m -> despliegue 200 m -> suelo)."""
    t = 0.0
    pkt = 0
    while True:
        pkt += 1
        # Ciclo de 180 s: ascenso (0..90s), caida libre (90..98s), autogiro (98..141s), suelo (141..180s)
        phase_t = t % 180.0
        if phase_t < 90.0:
            alt = 5.0 * phase_t
            vel = 5.0
            acc = 0.0
            state, sat, chute = 1, 0, 0
        elif phase_t < 98.0:
            dt = phase_t - 90.0
            alt = max(200.0, 450.0 - 0.5 * 9.81 * dt * dt * 0.8)
            vel = -9.81 * dt * 0.8
            acc = -7.8
            state, sat, chute = 2, 0, 0
        elif phase_t < 141.0:
            dt = phase_t - 98.0
            alt = max(0.0, 200.0 - 4.7 * dt)
            vel = -4.7
            acc = 0.0
            state, sat, chute = 2, 1, 1
        else:
            alt, vel, acc = 0.0, 0.0, 0.0
            state, sat, chute = 3, 1, 1

        pres = 1013.25 * math.exp(-alt / 8434.5)
        temp = 24.0 - 0.0065 * alt
        _publish({
            "mission_time_s": round(phase_t, 2),
            "altitude_m": round(alt, 2),
            "velocity_ms": round(vel, 2),
            "accel_ms2": round(acc, 2),
            "temperature_c": round(temp, 2),
            "pressure_hpa": round(pres, 2),
            "voltage_v": round(8.4 - 0.002 * phase_t, 2),
            "pitch_deg": round(2.5 * math.sin(phase_t / 3.0), 2),
            "roll_deg": round(5.0 * math.sin(phase_t / 2.5), 2),
            "yaw_deg": round((phase_t * 9.0) % 360.0, 2),
            "state": state,
            "satellite": sat,
            "chute": chute,
            "packet": pkt,
            "rssi_dbm": round(-55.0 - 0.08 * alt, 1),
            "snr_db": round(9.5 - 0.008 * alt, 1),
        })
        time.sleep(0.5)
        t += 0.5


_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CanSat Cluster · Telemetria</title>
<style>
  body{background:#0b111a;color:#e6eef8;font-family:system-ui,sans-serif;padding:18px;margin:0}
  .top{display:flex;justify-content:space-between;align-items:center;background:#131d2b;
       border:1px solid #23344a;border-radius:10px;padding:12px 16px;margin-bottom:14px}
  .badge{font-family:monospace;background:#0f2e1d;color:#22c55e;padding:4px 10px;border-radius:999px;font-size:13px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
  .card{background:#131d2b;border:1px solid #23344a;border-radius:10px;padding:12px}
  .lbl{font-size:11px;color:#8aa0b8}
  .val{font-family:monospace;font-size:22px;font-weight:700;color:#38bdf8;margin-top:4px}
</style>
</head>
<body>
  <div class="top">
    <strong>CanSat Pepe el Pollo · Cluster Web</strong>
    <span id="replica" class="badge">REPLICA: __REPLICA__</span>
  </div>
  <div class="grid">
    <div class="card"><div class="lbl">ALTITUD</div><div id="alt" class="val">--</div></div>
    <div class="card"><div class="lbl">VELOCIDAD</div><div id="vel" class="val">--</div></div>
    <div class="card"><div class="lbl">TEMPERATURA</div><div id="temp" class="val">--</div></div>
    <div class="card"><div class="lbl">PRESION</div><div id="pres" class="val">--</div></div>
    <div class="card"><div class="lbl">BATERIA</div><div id="vol" class="val">--</div></div>
    <div class="card"><div class="lbl">RSSI / SNR</div><div id="rf" class="val">--</div></div>
    <div class="card"><div class="lbl">DESACOPLE</div><div id="stage" class="val">--</div></div>
    <div class="card"><div class="lbl">PAQUETE</div><div id="pkt" class="val">--</div></div>
  </div>
<script>
const es = new EventSource("/events");
es.onmessage = (e) => {
  const d = JSON.parse(e.data);
  document.getElementById("replica").textContent = "REPLICA: " + d.replica;
  document.getElementById("alt").textContent = d.altitude_m.toFixed(1) + " m";
  document.getElementById("vel").textContent = d.velocity_ms.toFixed(2) + " m/s";
  document.getElementById("temp").textContent = d.temperature_c.toFixed(1) + " C";
  document.getElementById("pres").textContent = d.pressure_hpa.toFixed(1) + " hPa";
  document.getElementById("vol").textContent = d.voltage_v.toFixed(2) + " V";
  document.getElementById("rf").textContent = d.rssi_dbm + " / " + d.snr_db;
  document.getElementById("stage").textContent = d.chute ? "AUTOGIRO" : "ACOPLADO";
  document.getElementById("pkt").textContent = "#" + d.packet;
};
</script>
</body>
</html>
"""


class TelemetryHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        return

    def _send_json(self, data: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-CanSat-Replica", REPLICA_ID)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = _HTML.replace("__REPLICA__", REPLICA_ID).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-CanSat-Replica", REPLICA_ID)
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/health":
            self._send_json({
                "status": "ok",
                "replica": REPLICA_ID,
                "uptime_s": round(time.time() - START_TIME, 1),
                "sim_mode": SIM_MODE,
                "packets": _total_packets,
            })
            return

        if self.path == "/api/metrics":
            with _lock:
                snapshot = dict(_latest)
                max_alt = _max_altitude
                total = _total_packets
            self._send_json({
                "replica": REPLICA_ID,
                "max_altitude_m": round(max_alt, 2),
                "total_packets": total,
                "latest": snapshot,
            })
            return

        if self.path == "/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-CanSat-Replica", REPLICA_ID)
            self.end_headers()

            q: queue.Queue[str] = queue.Queue(maxsize=32)
            with _lock:
                _clients.append(q)
                first = json.dumps(_latest, ensure_ascii=False)
            try:
                self.wfile.write(f"data: {first}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15.0)
                        self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (OSError, BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with _lock:
                    if q in _clients:
                        _clients.remove(q)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/telemetry":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "invalid json"}, status=HTTPStatus.BAD_REQUEST)
            return
        _publish(data)
        self._send_json({"accepted": True, "replica": REPLICA_ID}, status=HTTPStatus.ACCEPTED)


def main() -> None:
    if SIM_MODE:
        threading.Thread(target=_simulator_loop, daemon=True, name="SimLoop").start()
    server = ThreadingHTTPServer((HOST, PORT), TelemetryHandler)
    print(f"[cansat-web] replica={REPLICA_ID} listening on http://{HOST}:{PORT} (sim={SIM_MODE})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
