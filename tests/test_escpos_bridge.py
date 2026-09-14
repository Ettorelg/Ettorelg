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
