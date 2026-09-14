"""Local-only Alpha Menu ESC/POS bridge. Run on the PC on the printer's LAN."""

import ipaddress
import json
import socket
import textwrap
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HOST = "127.0.0.1"
PORT = 17891
ORIGIN = "https://menu.alphasystemsrl.it"
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


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
    lines = ["ORDINE #" + clean(order.get("id"), 30),
             "AL TAVOLO" if order.get("origine") == "tavolo" else "DA ASPORTO",
             "Data: " + clean(order.get("data_richiesta"), 30),
             "Ora: " + clean(order.get("ora_richiesta"), 20), "-" * 42]
    for field, label in (("nome", "Cliente"), ("riferimento", "Riferimento/tavolo"),
                         ("telefono", "Telefono")):
        if order.get(field):
            lines.extend(textwrap.wrap(label + ": " + clean(order[field]), width=42))
    lines.append("-" * 42)
    for product in order["prodotti"][:100]:
        if not isinstance(product, dict):
            continue
        item = clean(product.get("quantita"), 15) + " x " + clean(product.get("nome"), 200)
        lines.extend(textwrap.wrap(item, width=42, break_long_words=True))
        if product.get("totale") is not None:
            lines.append("  EUR " + clean(product["totale"], 20))
    lines.append("-" * 42)
    if order.get("note"):
        lines.extend(textwrap.wrap("NOTE: " + clean(order["note"], 500), width=42))
    if order.get("totale") is not None:
        lines.append("TOTALE: EUR " + clean(order["totale"], 20))
    lines.extend(["", "Promemoria ordine - non fiscale", "", ""])
    # Initialize, choose PC850, print text, feed and cut. EUR is used because
    # ESC/POS code pages differ between printer models.
    return b"\x1b@\x1bt\x02" + ("\n".join(lines) + "\n").encode("cp850", "replace") + b"\x1dV\x00"


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
        except (ValueError, TypeError, UnicodeError) as exc:
            return self._reply(400, str(exc))
        try:
            with socket.create_connection((printer_ip, 9100), timeout=4) as printer:
                printer.sendall(payload)
        except OSError as exc:
            return self._reply(502, "Stampante non raggiungibile: " + clean(exc, 120))
        self._reply(200, "Ordine inviato alla stampante.")


if __name__ == "__main__":
    print(f"Alpha Menu ESC/POS pronto su http://{HOST}:{PORT}")
    print("Lascia aperta questa finestra mentre usi il banco ordini. Premi Ctrl+C per fermare.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
