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
    assert b"\x1ba\x01\x1d!\x11\x1bE\x00ORDINE #42\n\x1ba\x00" in payload
    assert b"\x1d!\x10\x1bE\x00DATA: 2026-09-14" in payload
    assert b"\x1d!\x10\x1bE\x00ORA: 20:00" in payload
    assert b"\x1d!\x11\x1bE\x00Cliente: Mario" in payload
    assert b"\x1d!\x00\x1bE\x00Riferimento: 7" in payload
    assert b"\x1d!\x01\x1bE\x012 x Pizza" in payload
    assert b"Pizza [0m" in payload
    assert b"\x1bE\x01" in payload
    assert b"Promemoria ordine" not in payload
    assert payload.endswith((b"\x1d!\x00\x1bE\x00\n" * 4) + b"\x1dV\x00")


def test_receipt_prints_progressive_number_but_keeps_internal_id_separate():
    payload = bridge.receipt({"id": 208, "numero": 3, "prodotti": []})
    assert b"ORDINE #3" in payload
    assert b"ORDINE #208" not in payload


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
    assert b"\x1d!\x01\x1bE\x011 x Pizza" in pizza_receipt
    assert b"\x1d!\x00\x1bE\x011 x Birra" in pizza_receipt
    assert b"\x1d!\x01\x1bE\x011 x Dolce" in pizza_receipt
    assert pizza_receipt.index(b"1 x Pizza") < pizza_receipt.index(b"RIEPILOGO") < pizza_receipt.index(b"1 x Birra")


def test_unmatched_category_printer_receives_nothing():
    order = {"id": 11, "prodotti": [{"id_categoria": 2, "nome": "Birra", "quantita": 1}]}
    printers = [{"id_categoria": 1, "ip": "192.168.1.10"}]
    assert bridge.print_targets(order, printers, "192.168.1.10") == {}
    assert bridge.print_targets(order, [], "192.168.1.10") == {"192.168.1.10": None}


def test_summary_printer_adds_full_order_receipt_even_when_no_category_matches():
    order = {"id": 12, "prodotti": [{"id_categoria": 2, "nome": "Birra", "quantita": 1}]}
    printers = [{"id_categoria": 1, "ip": "192.168.1.10"}]
    jobs = bridge.print_jobs(order, printers, "", "192.168.1.60", "auto")
    assert jobs == [("192.168.1.60", None, "riepilogo")]
    payload = bridge.receipt(order, jobs[0][1], summary=True)
    assert b"\x1ba\x01\x1d!\x11\x1bE\x00RIEPILOGO\n\x1ba\x00" in payload
    assert b"\x1d!\x00\x1bE\x011 x Birra" in payload


def test_same_ip_can_print_category_and_separate_summary_ticket():
    order = {"id": 13, "prodotti": [{"id_categoria": 1, "nome": "Pizza", "quantita": 1}]}
    printers = [{"id_categoria": 1, "ip": "192.168.1.10"}]
    assert bridge.print_jobs(order, printers, "", "192.168.1.10", "auto") == [
        ("192.168.1.10", {"1"}, "categoria"), ("192.168.1.10", None, "riepilogo")]


def test_manual_summary_mode_sends_only_the_summary_printer():
    order = {"id": 17, "prodotti": [{"id_categoria": 1, "nome": "Pizza", "quantita": 1}]}
    printers = [{"id_categoria": 1, "ip": "192.168.1.10"}]
    assert bridge.print_jobs(order, printers, "", "192.168.1.20", "summary") == [
        ("192.168.1.20", None, "riepilogo")]
    assert bridge.print_jobs(order, printers, "", "192.168.1.20", "all") == [
        ("192.168.1.10", {"1"}, "categoria")]
    assert bridge.print_jobs(order, printers, "", "192.168.1.20", "auto") == [
        ("192.168.1.10", {"1"}, "categoria"), ("192.168.1.20", None, "riepilogo")]


def test_manual_summary_mode_requires_summary_printer():
    import pytest
    order = {"id": 18, "prodotti": []}
    with pytest.raises(ValueError, match="Configura una stampante di riepilogo"):
        bridge.print_jobs(order, [], "192.168.1.10", "", "summary")
    with pytest.raises(ValueError, match="Modalità di stampa non valida"):
        bridge.print_jobs(order, [], "", "", "unexpected")


def test_named_printer_routes_products_and_respects_copy_count():
    order = {"id": 20, "prodotti": [
        {"id_prodotto": 11, "id_categoria": 2, "nome": "Acqua", "quantita": 1},
        {"id_prodotto": 12, "id_categoria": 1, "nome": "Pizza", "quantita": 1},
    ]}
    printers = [{"name": "BIBITE", "ip": "192.168.2.31", "role": "comanda",
                 "copies": 2, "category_ids": [], "product_ids": [11]}]
    jobs = bridge.print_jobs(order, printers, "", "", "all")
    assert len(jobs) == 2
    assert jobs[0][0] == "192.168.2.31"
    assert jobs[0][1] == {"categories": set(), "products": {"11"}}
    payload = bridge.receipt(order, jobs[0][1])
    assert payload.index(b"1 x Acqua") < payload.index(b"RIEPILOGO") < payload.index(b"1 x Pizza")


def test_named_summary_printer_uses_its_own_copy_count():
    order = {"id": 21, "prodotti": []}
    printers = [{"name": "PRECONTO", "ip": "192.168.2.40", "role": "preconto",
                 "copies": 3, "category_ids": [], "product_ids": []}]
    jobs = bridge.print_jobs(order, printers, "", "", "summary")
    assert jobs == [("192.168.2.40", None, "riepilogo")] * 3


def test_prices_only_appear_as_final_total_on_summary():
    order = {"id": 14, "prodotti": [
        {"id_categoria": 1, "nome": "Pizza", "quantita": 2.0, "totale": 20},
        {"id_categoria": 2, "nome": "Vino", "quantita": "1,5", "totale": 12},
    ], "totale": 32}
    category = bridge.receipt(order, {"1"})
    summary = bridge.receipt(order, summary=True)
    assert b"2 x Pizza" in category
    assert b"1,5 x Vino" in category
    assert b"EUR" not in category
    assert b"EUR 20" not in summary and b"EUR 12" not in summary
    assert summary.count(b"EUR") == 1
    assert b"TOTALE: EUR 32" in summary
    assert b"\x1d!\x11\x1bE\x00TOTALE: EUR 32" in summary


def test_category_products_print_first_and_other_products_follow_under_summary():
    order = {"id": 15, "prodotti": [
        {"id_categoria": 2, "nome": "Birra", "quantita": 1},
        {"id_categoria": 1, "nome": "Pizza", "quantita": 2},
        {"id_categoria": 3, "nome": "Dolce", "quantita": 1},
    ]}
    payload = bridge.receipt(order, {"1"})
    assert payload.index(b"2 x Pizza") < payload.index(b"RIEPILOGO") < payload.index(b"1 x Birra") < payload.index(b"1 x Dolce")
    assert b"\x1d!\x01\x1bE\x012 x Pizza" in payload
    assert b"\x1d!\x00\x1bE\x011 x Birra" in payload
    assert b"\x1d!\x00\x1bE\x011 x Dolce" in payload


def test_takeaway_receipt_omits_takeaway_label_and_prints_products_bold_double_width():
    order = {"id": 19, "origine": "asporto", "riferimento": "Banco", "prodotti": [
        {"id_categoria": 2, "nome": "Bibita", "quantita": 1}]}
    payload = bridge.receipt(order)
    assert b"DA ASPORTO" not in payload
    assert b"Riferimento: Banco" in payload
    assert b"Riferimento/tavolo" not in payload
    assert b"\x1d!\x01\x1bE\x011 x Bibita" in payload


def test_weighted_products_always_print_three_decimal_places():
    order = {"id": 16, "prodotti": [
        {"nome": "Salame (kg)", "quantita": "1.000"},
        {"nome": "Formaggio", "unita_prezzo": "kg", "quantita": "0.125"},
        {"nome": "Pane", "unita_prezzo": "pezzo", "quantita": "2.000"},
    ]}
    payload = bridge.receipt(order, summary=True)
    assert b"1,000 x Salame (kg)" in payload
    assert b"0,125 x Formaggio" in payload
    assert b"2 x Pane" in payload


def test_configured_multitaste_product_prints_flavors_and_changes_hierarchically():
    order = {"id": 23, "prodotti": [{"id_categoria": 1, "quantita": 1, "nome": "fallback",
        "configurazione": {"_stampa": {"tipo": "Pizza multigusto", "formato": "Singola", "impasto": "Integrale", "gusti": [
                {"quota": "1/2", "nome": "Rianella", "senza": ["Aglio"], "aggiunte": ["Pomodoro"]},
                {"quota": "1/2", "nome": "Margherita", "senza": ["Salame"], "aggiunte": ["Prosciutto"]}]}}}]}
    payload = bridge.receipt(order, {"1"})
    assert b"1 x PIZZA MULTIGUSTO" in payload
    assert b"  >> 1/2 RIANELLA" in payload and b"  >> 1/2 MARGHERITA" in payload
    assert b"     -AGLIO" in payload and b"     +PROSCIUTTO" in payload
    assert b"\x1d!\x10\x1bE\x00     -AGLIO" in payload
    assert b"\x1d!\x10\x1bE\x00     +PROSCIUTTO" in payload
    assert b"\x1d!\x01\x1bE\x01     IMPASTO:" in payload
