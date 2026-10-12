#!/usr/bin/env python3
"""
v1.3.6 - Minimaler Websocket-Client für die Home-Assistant-Websocket-API

Nur Standardbibliothek (das Add-on-Image hat kein websocket-Paket). Eine Verbindung
pro Befehl: verbinden, mit SUPERVISOR_TOKEN authentifizieren, einen Befehl senden,
das Ergebnis zurückgeben. Aus dem Add-on über den Supervisor-Proxy
ws://supervisor/core/websocket erreichbar (config.yaml: homeassistant_api: true).

Gebraucht für recorder/statistics_during_period (HA-Langzeitstatistik), die es
über die REST-API nicht gibt.
"""

import base64
import json
import os
import socket
import ssl
import struct
from urllib.parse import urlsplit

MAX_MESSAGE_BYTES = 16 * 1024 * 1024

_OP_CONT, _OP_TEXT, _OP_CLOSE, _OP_PING, _OP_PONG = 0x0, 0x1, 0x8, 0x9, 0xA


class HAWebsocketError(Exception):
    """Verbindungs-, Protokoll- oder Befehlsfehler der HA-Websocket-API."""


def websocket_url(api_url):
    """'http://supervisor/core' -> ('supervisor', 80, '/core/websocket', False)."""
    parts = urlsplit(api_url)
    secure = parts.scheme in ('https', 'wss')
    port = parts.port or (443 if secure else 80)
    return parts.hostname, port, parts.path.rstrip('/') + '/websocket', secure


def encode_frame(payload, opcode=_OP_TEXT, mask_key=None):
    """Client-Frame (FIN, maskiert wie von RFC 6455 für Clients verlangt)."""
    mask_key = mask_key if mask_key is not None else os.urandom(4)
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack('!H', n)
    else:
        header.append(0x80 | 127)
        header += struct.pack('!Q', n)
    header += mask_key
    return bytes(header) + bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))


class _Connection:
    def __init__(self, sock, buffered=b''):
        self.sock = sock
        self.buf = buffered

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise HAWebsocketError("connection closed")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def send_text(self, text):
        self.sock.sendall(encode_frame(text.encode('utf-8')))

    def recv_text(self):
        """Nächste Textnachricht (Fragmente zusammengesetzt, Ping beantwortet)."""
        message = b''
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack('!H', self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self._read_exact(8))[0]
            if n > MAX_MESSAGE_BYTES or len(message) + n > MAX_MESSAGE_BYTES:
                raise HAWebsocketError(f"message too large ({n} bytes)")
            mask_key = self._read_exact(4) if b2 & 0x80 else None
            payload = self._read_exact(n)
            if mask_key:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            if opcode == _OP_PING:
                self.sock.sendall(encode_frame(payload, _OP_PONG))
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                raise HAWebsocketError("closed by server")
            if opcode not in (_OP_TEXT, _OP_CONT):
                raise HAWebsocketError(f"unexpected opcode {opcode}")
            message += payload
            if fin:
                return message.decode('utf-8')

    def recv_json(self):
        return json.loads(self.recv_text())


def _handshake(sock, host, path):
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    sock.sendall((f"GET {path} HTTP/1.1\r\n"
                  f"Host: {host}\r\n"
                  f"Upgrade: websocket\r\n"
                  f"Connection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\n"
                  f"Sec-WebSocket-Version: 13\r\n\r\n").encode('ascii'))
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = sock.recv(4096)
        if not chunk or len(buf) > 65536:
            raise HAWebsocketError("no websocket handshake response")
        buf += chunk
    head, rest = buf.split(b'\r\n\r\n', 1)
    status = head.split(b'\r\n', 1)[0].decode('latin-1')
    if ' 101 ' not in f"{status} ":
        raise HAWebsocketError(f"websocket upgrade refused: {status}")
    return rest


def ws_command(api_url, token, command, timeout=15):
    """Einen Befehl über die HA-Websocket-API ausführen.

    Args:
        api_url: REST-Basis-URL, z. B. 'http://supervisor/core'
        token: SUPERVISOR_TOKEN (bzw. Long-Lived-Token)
        command: dict mit 'type' und Parametern (ohne 'id')
        timeout: Socket-Timeout in Sekunden (Verbindung und jede Leseoperation)

    Returns:
        'result' der Antwort

    Raises:
        HAWebsocketError (auch für Socket-Fehler und success = false)
    """
    if not token:
        raise HAWebsocketError("no token")
    host, port, path, secure = websocket_url(api_url)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        raise HAWebsocketError(f"connect failed: {e}") from e
    try:
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        conn = _Connection(sock, _handshake(sock, host, path))
        msg = conn.recv_json()
        if msg.get('type') == 'auth_required':
            conn.send_text(json.dumps({'type': 'auth', 'access_token': token}))
            msg = conn.recv_json()
        if msg.get('type') != 'auth_ok':
            raise HAWebsocketError(f"auth failed: {msg.get('type')}")
        conn.send_text(json.dumps(dict(command, id=1)))
        while True:
            msg = conn.recv_json()
            if msg.get('id') == 1 and msg.get('type') == 'result':
                if not msg.get('success'):
                    raise HAWebsocketError(f"command failed: {msg.get('error')}")
                return msg.get('result')
    except HAWebsocketError:
        raise
    except (OSError, ValueError) as e:   # socket.timeout ist OSError, JSON-/UTF-8-Fehler ValueError
        raise HAWebsocketError(f"{type(e).__name__}: {e}") from e
    finally:
        try:
            sock.close()
        except OSError:
            pass
