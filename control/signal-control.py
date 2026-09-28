#!/usr/bin/env python3
"""Servicio de control por Signal para el stack de n8n.

Corre en el host (no en un contenedor) como servicio de systemd del usuario.
Se conecta por websocket a signal-api (127.0.0.1:8080), lee los mensajes que
llegan a tu cuenta y:

- "encender": levanta postgres y n8n con docker compose.
- "apagar":   detiene n8n y postgres (signal-api y este servicio siguen vivos).
- estado, revisar, pausar, reanudar, ayuda: se los pasa a n8n por el webhook
  del flujo "Receptor de comandos Signal". Si n8n está apagado, te avisa.

Todo lo demás (tus chats, confirmaciones de lectura...) se descarta aquí y no
llega a n8n. Nunca se escribe el contenido de un mensaje en el log.

Solo usa la librería estándar de Python.
"""
import base64
import json
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SIGNAL_API = os.environ.get("SIGNAL_API", "http://127.0.0.1:8080")
N8N_URL = os.environ.get("N8N_URL", "http://127.0.0.1:5678")
N8N_WEBHOOK = f"{N8N_URL}/webhook/signal-comandos"
PROYECTO = Path(os.environ.get("PROYECTO", Path(__file__).resolve().parent.parent))
# Números extra que pueden mandar comandos, separados por coma (ej. +5216141234567)
AUTORIZADOS = {n.strip() for n in os.environ.get("AUTORIZADOS", "").split(",") if n.strip()}

COMANDOS_CONTROL = {"encender", "apagar"}
COMANDOS_N8N = {"estado", "revisar", "pausar", "reanudar", "ayuda"}

bloqueo = threading.Lock()  # un encender/apagar a la vez


def log(texto):
    print(texto, flush=True)


# ---------------------------------------------------------------- HTTP

def http_json(url, datos=None, timeout=10):
    cuerpo = None if datos is None else json.dumps(datos).encode()
    req = urllib.request.Request(url, data=cuerpo, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        texto = r.read().decode()
        return json.loads(texto) if texto.strip().startswith(("{", "[")) else texto


def responder(cuenta, destino, mensaje):
    try:
        http_json(f"{SIGNAL_API}/v2/send", {"message": mensaje, "number": cuenta, "recipients": [destino]})
    except Exception as e:
        log(f"No se pudo enviar la respuesta por Signal: {e}")


def n8n_arriba():
    try:
        with urllib.request.urlopen(f"{N8N_URL}/healthz", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------- comandos

def leer_comando(msg, cuenta):
    """Regresa (comando, responderA) si el mensaje es un comando válido, si no (None, None)."""
    env = msg.get("envelope") or (msg.get("params") or {}).get("envelope")
    if not isinstance(env, dict):
        return None, None
    desde = env.get("sourceNumber") or env.get("source")
    enviado = (env.get("syncMessage") or {}).get("sentMessage")
    texto = responder_a = None

    if enviado and desde == cuenta and (
        (enviado.get("destinationNumber") or enviado.get("destination")) == cuenta
        or (enviado.get("destinationUuid") and enviado.get("destinationUuid") == env.get("sourceUuid"))
    ):
        texto, responder_a = enviado.get("message"), cuenta      # tu "Nota personal"
    elif env.get("dataMessage") and desde in AUTORIZADOS:
        texto, responder_a = env["dataMessage"].get("message"), desde

    if not isinstance(texto, str):
        return None, None
    comando = texto.strip().lower().lstrip("/!")
    if comando in COMANDOS_CONTROL | COMANDOS_N8N:
        return comando, responder_a
    return None, None


def compose(*args):
    r = subprocess.run(["docker", "compose", *args], cwd=PROYECTO,
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1])


def encender(cuenta, destino):
    with bloqueo:
        if n8n_arriba():
            return responder(cuenta, destino, "✅ n8n ya estaba encendido.")
        responder(cuenta, destino, "⏳ Encendiendo n8n y Postgres...")
        try:
            compose("up", "-d", "postgres", "n8n")
        except Exception as e:
            log(f"encender falló: {e}")
            return responder(cuenta, destino, f"❌ No se pudo encender: {e}")
        for _ in range(60):
            if n8n_arriba():
                log("n8n encendido")
                return responder(cuenta, destino, "✅ n8n encendido. El monitoreo vuelve a correr cada 5 minutos.")
            time.sleep(2)
        responder(cuenta, destino, "⚠️ Los contenedores arrancaron pero n8n no responde después de 2 minutos. Revisa: docker compose logs n8n")


def apagar(cuenta, destino):
    with bloqueo:
        try:
            compose("stop", "n8n", "postgres")
        except Exception as e:
            log(f"apagar falló: {e}")
            return responder(cuenta, destino, f"❌ No se pudo apagar: {e}")
        log("n8n apagado")
        responder(cuenta, destino, '⏹️ n8n y Postgres apagados. No hay monitoreo hasta que escribas "encender".')


def pasar_a_n8n(msg, cuenta, comando, destino):
    try:
        http_json(N8N_WEBHOOK, msg)
        log(f"comando '{comando}' enviado a n8n")
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        responder(cuenta, destino, '💤 n8n está apagado. Escribe "encender" para prenderlo.')


def atender(msg, cuenta):
    comando, destino = leer_comando(msg, cuenta)
    if not comando:
        return
    log(f"comando recibido: {comando}")
    if comando == "encender":
        threading.Thread(target=encender, args=(cuenta, destino), daemon=True).start()
    elif comando == "apagar":
        threading.Thread(target=apagar, args=(cuenta, destino), daemon=True).start()
    else:
        pasar_a_n8n(msg, cuenta, comando, destino)


# ---------------------------------------------------------------- websocket mínimo (RFC 6455)

class WebSocket:
    def __init__(self, url):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=15)
        clave = base64.b64encode(os.urandom(16)).decode()
        ruta = u.path + (f"?{u.query}" if u.query else "")
        self.sock.sendall((
            f"GET {ruta} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {clave}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            parte = self.sock.recv(4096)
            if not parte:
                raise ConnectionError("signal-api cerró la conexión")
            self.buf += parte
        cabecera, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in cabecera.split(b"\r\n", 1)[0]:
            raise ConnectionError(cabecera.split(b"\r\n", 1)[0].decode(errors="replace"))
        self.sock.settimeout(60)

    def _leer(self, n):
        while len(self.buf) < n:
            parte = self.sock.recv(65536)
            if not parte:
                raise ConnectionError("signal-api cerró la conexión")
            self.buf += parte
        datos, self.buf = self.buf[:n], self.buf[n:]
        return datos

    def enviar(self, opcode, datos=b""):
        mascara = os.urandom(4)
        n = len(datos)
        if n < 126:
            cab = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            cab = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            cab = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        self.sock.sendall(cab + mascara + bytes(b ^ mascara[i % 4] for i, b in enumerate(datos)))

    def recibir(self):
        """Regresa el siguiente mensaje de texto. Contesta pings y junta fragmentos."""
        partes = []
        while True:
            try:
                b1, b2 = self._leer(2)
            except socket.timeout:
                self.enviar(0x9)          # ping para detectar conexiones muertas
                continue
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._leer(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._leer(8))[0]
            mascara = self._leer(4) if b2 & 0x80 else None
            datos = self._leer(n)
            if mascara:
                datos = bytes(b ^ mascara[i % 4] for i, b in enumerate(datos))
            if opcode == 0x8:
                raise ConnectionError("signal-api cerró el websocket")
            if opcode == 0x9:
                self.enviar(0xA, datos)
                continue
            if opcode in (0x1, 0x0):
                partes.append(datos)
                if fin:
                    return b"".join(partes).decode()

    def cerrar(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- principal

def obtener_cuenta():
    while True:
        try:
            cuentas = http_json(f"{SIGNAL_API}/v1/accounts")
            if cuentas:
                return cuentas[0]
            log("signal-api no tiene ninguna cuenta vinculada; reintento en 30 s")
        except Exception as e:
            log(f"signal-api no responde ({e}); reintento en 10 s")
            time.sleep(10)
            continue
        time.sleep(30)


def main():
    log(f"Servicio de control iniciado. Proyecto: {PROYECTO}")
    espera = 2
    while True:
        cuenta = obtener_cuenta()
        ws_url = SIGNAL_API.replace("http", "ws", 1) + "/v1/receive/" + urllib.parse.quote(cuenta)
        ws = None
        try:
            ws = WebSocket(ws_url)
            log("Conectado a signal-api, esperando comandos")
            espera = 2
            while True:
                texto = ws.recibir()
                try:
                    msg = json.loads(texto)
                except ValueError:
                    continue
                try:
                    atender(msg, cuenta)
                except Exception as e:
                    log(f"Error atendiendo un mensaje: {e}")
        except Exception as e:
            log(f"Conexión con signal-api perdida ({e}); reconecto en {espera} s")
        finally:
            if ws:
                ws.cerrar()
        time.sleep(espera)
        espera = min(espera * 2, 60)


if __name__ == "__main__":
    main()
