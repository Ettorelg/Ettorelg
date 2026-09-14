"""Local-only Alpha Menu ESC/POS bridge. Run on the PC on the printer's LAN."""

import ipaddress
import json
import socket
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HOST = "127.0.0.1"
PORT = 17891
ORIGIN = "https://menu.alphasystemsrl.it"
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
PRINTED_JOBS = set()
PRINT_LOCK = threading.Lock()


def valid_printer_ip(value):
    if not isinstance(value, str):
        return False
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError):
        return False
    return any(address in network for network in PRIVATE_NETWORKS)


def clean(value, limit=300):
    text = str(value if value is not None else "")[:limit]
    return "".join(ch if ch.isprintable() else " " for ch in text).strip()


def receipt(order):
    if not isinstance(order, dict) or not isinstance(order.get("prodotti"), list):
        raise ValueError("Dati ordine non validi.")
    output = bytearray(b"\x1b@\x1bt\x02")

    def line(value, large=False):
        width = 21 if large else 42
        for part in textwrap.wrap(clean(value, 500), width=width, break_long_words=True) or [""]:
            output.extend(b"\x1d!\x11" if large else b"\x1d!\x00")
            output.extend((part + "\n").encode("cp850", "replace"))

    line("ORDINE #" + clean(order.get("id"), 30), True)
    line("AL TAVOLO" if order.get("origine") == "tavolo" else "DA ASPORTO")
    line("Data: " + clean(order.get("data_richiesta"), 30), True)
    line("Ora: " + clean(order.get("ora_richiesta"), 20), True)
    line("-" * 42)
    for field, label in (("nome", "Cliente"), ("riferimento", "Riferimento/tavolo"),
                         ("telefono", "Telefono")):
        if order.get(field):
            line(label + ": " + clean(order[field]), field == "nome")
    line("-" * 42)
    for product in order["prodotti"][:100]:
        if not isinstance(product, dict):
            continue
        item = clean(product.get("quantita"), 15) + " x " + clean(product.get("nome"), 200)
        line(item, True)
        if product.get("totale") is not None:
            line("  EUR " + clean(product["totale"], 20))
    line("-" * 42)
    if order.get("note"):
        line("NOTE: " + clean(order["note"], 500))
    if order.get("totale") is not None:
        line("TOTALE: EUR " + clean(order["totale"], 20))
    line("")
    line("Promemoria ordine - non fiscale")
    line("")
    line("")
    # Initialize, choose PC850, print text, feed and cut. EUR is used because
    # ESC/POS code pages differ between printer models.
    return bytes(output) + b"\x1dV\x00"


class Handler(BaseHTTPRequestHandler):
    def _headers(self, status):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _reply(self, status, message):
        self._headers(status)
        self.wfile.write(json.dumps({"message": message}).encode("utf-8"))

    def _trusted(self):
        return self.headers.get("Origin") == ORIGIN and self.headers.get("Host") in (
            f"{HOST}:{PORT}", f"localhost:{PORT}")

    def do_OPTIONS(self):
        if not self._trusted():
            return self._reply(403, "Origine non consentita.")
        self._headers(204)

    def do_GET(self):
        if not self._trusted() or self.path != "/health":
            return self._reply(403, "Richiesta non consentita.")
        self._reply(200, "Programma di stampa pronto.")

    def do_POST(self):
        if not self._trusted() or self.path != "/print":
            return self._reply(403, "Richiesta non consentita.")
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._reply(415, "Formato non valido.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 65536:
                raise ValueError("Dimensione ordine non valida.")
            data = json.loads(self.rfile.read(size))
            printer_ip = data.get("printer_ip")
            if not valid_printer_ip(printer_ip):
                raise ValueError("IP stampante locale non valido.")
            payload = receipt(data.get("order"))
            automatic = data.get("automatic") is True
            job = (printer_ip, clean(data["order"].get("id"), 30), clean(data["order"].get("creato_il"), 40))
        except (ValueError, TypeError, UnicodeError) as exc:
            return self._reply(400, str(exc))
        with PRINT_LOCK:
            if automatic and job in PRINTED_JOBS:
                return self._reply(200, "Ordine già inviato automaticamente.")
            try:
                with socket.create_connection((printer_ip, 9100), timeout=4) as printer:
                    printer.sendall(payload)
            except OSError as exc:
                return self._reply(502, "Stampante non raggiungibile: " + clean(exc, 120))
            if automatic:
                PRINTED_JOBS.add(job)
        self._reply(200, "Ordine inviato alla stampante.")


if __name__ == "__main__":
    print(f"Alpha Menu ESC/POS pronto su http://{HOST}:{PORT}")
    print("Lascia aperta questa finestra mentre usi il banco ordini. Premi Ctrl+C per fermare.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
