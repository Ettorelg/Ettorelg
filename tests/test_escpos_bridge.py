import importlib.util
from pathlib import Path


script = Path(__file__).resolve().parents[1] / "tools" / "escpos_bridge.py"
spec = importlib.util.spec_from_file_location("escpos_bridge", script)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def test_only_private_ipv4_printers_are_allowed():
    assert bridge.valid_printer_ip("192.168.1.50")
    assert bridge.valid_printer_ip("10.0.0.2")
    assert not bridge.valid_printer_ip("8.8.8.8")
    assert not bridge.valid_printer_ip("127.0.0.1")
    assert not bridge.valid_printer_ip("::1")


def test_receipt_contains_order_and_escpos_cut_without_control_injection():
    payload = bridge.receipt({"id": 42, "origine": "tavolo", "data_richiesta": "2026-09-14",
                              "ora_richiesta": "20:00", "nome": "Mario", "riferimento": "7",
                              "prodotti": [{"nome": "Pizza\x1b[0m", "quantita": 2, "totale": 20}],
                              "note": "Senza olive", "totale": 20})
    assert payload.startswith(b"\x1b@\x1bt\x02")
    assert payload.endswith(b"\x1dV\x00")
    assert b"ORDINE #42" in payload and b"AL TAVOLO" in payload
    assert b"\x1d!\x11Data: 2026-09-14" in payload
    assert b"\x1d!\x11Ora: 20:00" in payload
    assert b"\x1d!\x11Cliente: Mario" in payload
    assert b"\x1d!\x112 x Pizza" in payload
    assert b"Pizza [0m" in payload
    assert payload.count(b"\x1b") == 2


def test_mixed_category_order_routes_once_per_matching_printer():
    order = {"id": 10, "prodotti": [
        {"id_categoria": 1, "nome": "Pizza", "quantita": 1},
        {"id_categoria": 2, "nome": "Birra", "quantita": 1},
        {"id_categoria": 3, "nome": "Dolce", "quantita": 1},
    ]}
    printers = [{"id_categoria": 1, "ip": "192.168.1.10"},
                {"id_categoria": 2, "ip": "192.168.1.20"},
                {"id_categoria": 3, "ip": "192.168.1.10"},
                {"id_categoria": 4, "ip": "192.168.1.30"}]
    targets = bridge.print_targets(order, printers, "192.168.1.99")
    assert targets == {"192.168.1.10": {"1", "3"}, "192.168.1.20": {"2"}}
    pizza_receipt = bridge.receipt(order, targets["192.168.1.10"])
    assert b"\x1d!\x111 x Pizza" in pizza_receipt
    assert b"\x1d!\x001 x Birra" in pizza_receipt
    assert b"\x1d!\x111 x Dolce" in pizza_receipt


def test_unmatched_category_printer_receives_nothing():
    order = {"id": 11, "prodotti": [{"id_categoria": 2, "nome": "Birra", "quantita": 1}]}
    printers = [{"id_categoria": 1, "ip": "192.168.1.10"}]
    assert bridge.print_targets(order, printers, "192.168.1.10") == {}
    assert bridge.print_targets(order, [], "192.168.1.10") == {"192.168.1.10": None}
