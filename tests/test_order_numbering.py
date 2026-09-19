from pathlib import Path


source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


def test_order_counter_cycles_from_200_back_to_one_without_time_reset():
    endpoint = source[source.index('def api_crea_ordine_menu'):source.index('def order_push_keys')]
    assert "contatori_ordini_menu.ultimo_numero>=200 THEN 1" in endpoint
    assert "ELSE contatori_ordini_menu.ultimo_numero+1" in endpoint
    assert "RETURNING ultimo_numero" in endpoint
    assert "INTERVAL '3 hours'" not in endpoint


def test_visible_order_number_is_separate_from_database_id():
    assert "numero_progressivo INTEGER" in source
    assert '"ordine_id": order_id, "numero_ordine": progressive_number' in source
