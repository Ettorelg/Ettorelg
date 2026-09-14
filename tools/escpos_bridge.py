"""Local-only Alpha Menu ESC/POS bridge. Run on the PC on the printer's LAN."""

import ipaddress
import json
import socket
import textwrap
import threading
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HOST = "127.0.0.1"
PORT = 17891
BRIDGE_VERSION = 7
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


def format_quantity(value, weight=False):
    try:
        number = Decimal(str(value).replace(",", "."))
        if not number.is_finite():
            raise InvalidOperation
        return (format(number, ".3f") if weight else format(number.normalize(), "f")).replace(".", ",")
    except (InvalidOperation, ValueError):
        return clean(value, 15)


def receipt(order, large_category_ids=None, summary=False):
    if not isinstance(order, dict) or not isinstance(order.get("prodotti"), list):
        raise ValueError("Dati ordine non validi.")
    output = bytearray(b"\x1b@\x1bt\x02")

    def line(value, large=False, centered=False, double_width=False, double_height=False):
        wide = large or double_width
        tall = large or double_height
        width = 21 if wide else 42
        size = (1 if wide else 0) | (0x10 if tall else 0)
        if centered:
            output.extend(b"\x1ba\x01")
        for part in textwrap.wrap(clean(value, 500), width=width, break_long_words=True) or [""]:
            output.extend(b"\x1d!" + bytes([size]))
            output.extend((part + "\n").encode("cp850", "replace"))
        if centered:
            output.extend(b"\x1ba\x00")

    if summary:
        line("RIEPILOGO", True, centered=True)
    line("ORDINE #" + clean(order.get("id"), 30), True, centered=True)
    line("AL TAVOLO" if order.get("origine") == "tavolo" else "DA ASPORTO")
    line("Data: " + clean(order.get("data_richiesta"), 30), double_width=True)
    line("Ora: " + clean(order.get("ora_richiesta"), 20), True)
    line("-" * 42)
    for field, label in (("nome", "Cliente"), ("riferimento", "Riferimento/tavolo"),
                         ("telefono", "Telefono")):
        if order.get(field):
            line(label + ": " + clean(order[field]), field == "nome")
    line("-" * 42)
    products = order["prodotti"][:100]
    if large_category_ids is not None and not summary:
        products = sorted(products, key=lambda product: (
            not isinstance(product, dict) or str(product.get("id_categoria")) not in large_category_ids))
    for product in products:
        if not isinstance(product, dict):
            continue
        name = clean(product.get("nome"), 200)
        is_weight = product.get("unita_prezzo") == "kg" or name.lower().endswith(" (kg)")
        item = format_quantity(product.get("quantita"), is_weight) + " x " + name
        is_large = not summary and (large_category_ids is None or str(product.get("id_categoria")) in large_category_ids)
        line(item, is_large, double_height=True)
    line("-" * 42)
    if order.get("note"):
        line("NOTE: " + clean(order["note"], 500))
    if summary and order.get("totale") is not None:
        line("TOTALE: EUR " + clean(order["totale"], 20), True)
    for _ in range(4):
        line("")
    # Initialize, choose PC850, print text, feed and cut. EUR is used because
    # ESC/POS code pages differ between printer models.
    return bytes(output) + b"\x1dV\x00"


def print_targets(order, printers, default_ip):
    if not isinstance(order, dict) or not isinstance(order.get("prodotti"), list):
        raise ValueError("Dati ordine non validi.")
    if not isinstance(printers, list) or len(printers) > 100:
        raise ValueError("Configurazione stampanti non valida.")
    present = {str(item.get("id_categoria")) for item in order["prodotti"] if isinstance(item, dict)}
    targets = {}
    for printer in printers:
        if not isinstance(printer, dict) or not isinstance(printer.get("id_categoria"), int):
            raise ValueError("Categoria stampante non valida.")
        ip = printer.get("ip")
        if not valid_printer_ip(ip):
            raise ValueError("IP stampante categoria non valido.")
        category_id = str(printer["id_categoria"])
        if category_id in present:
            targets.setdefault(ip, set()).add(category_id)
    if targets:
        return targets
    if printers:
        return {}
    if default_ip:
        if not valid_printer_ip(default_ip):
            raise ValueError("IP stampante generale non valido.")
        return {default_ip: None}
    return {}


def print_jobs(order, printers, default_ip, summary_ip, mode="all"):
    if mode not in ("all", "summary"):
        raise ValueError("Modalità di stampa non valida.")
    summary_ip = summary_ip or ""
    if summary_ip and not valid_printer_ip(summary_ip):
        raise ValueError("IP stampante di riepilogo non valido.")
    if mode == "summary":
        if not summary_ip:
            raise ValueError("Configura una stampante di riepilogo prima di stamparlo.")
        return [(summary_ip, None, "riepilogo")]
    targets = print_targets(order, printers, default_ip)
    jobs = [(ip, large_ids, "categoria") for ip, large_ids in targets.items()]
    if summary_ip:
        jobs.append((summary_ip, None, "riepilogo"))
    return jobs


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        # The bridge may keep running after its launching terminal closes.
        # Logging to a detached stderr would abort otherwise valid requests.
        pass

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

    def _reply(self, status, message, printed=None):
        self._headers(status)
        payload = {"message": message}
        if printed is not None:
            payload["printed"] = printed
        self.wfile.write(json.dumps(payload).encode("utf-8"))

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
        self._headers(200)
        self.wfile.write(json.dumps({"message": "Programma di stampa pronto.", "version": BRIDGE_VERSION}).encode("utf-8"))

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
            order = data.get("order")
            jobs = print_jobs(order, data.get("printers", []), data.get("printer_ip"), data.get("summary_ip"), data.get("mode", "all"))
            automatic = data.get("automatic") is True
        except (ValueError, TypeError, UnicodeError) as exc:
            return self._reply(400, str(exc))
        if not jobs:
            return self._reply(200, "Nessuna categoria dell’ordine ha una stampante assegnata; nessuna stampa inviata.", 0)
        failures = []
        with PRINT_LOCK:
            for printer_ip, large_ids, role in jobs:
                job = (printer_ip, role, clean(order.get("id"), 30), clean(order.get("creato_il"), 40))
                if automatic and job in PRINTED_JOBS:
                    continue
                try:
                    payload = receipt(order, large_ids, summary=(role == "riepilogo"))
                    with socket.create_connection((printer_ip, 9100), timeout=4) as printer:
                        printer.sendall(payload)
                except OSError as exc:
                    failures.append(printer_ip + ": " + clean(exc, 100))
                    continue
                if automatic:
                    PRINTED_JOBS.add(job)
        if failures:
            return self._reply(502, "Stampante non raggiungibile: " + "; ".join(failures))
        self._reply(200, "Inviate " + str(len(jobs)) + " ricevute.", len(jobs))


if __name__ == "__main__":
    print(f"Alpha Menu ESC/POS pronto su http://{HOST}:{PORT}")
    print("Lascia aperta questa finestra mentre usi il banco ordini. Premi Ctrl+C per fermare.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
