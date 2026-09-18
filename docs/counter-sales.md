# Vendita diretta al banco

`POST /api/banco/vendite` uses the existing server-side catalogue, quantity,
pizzeria and stock validation, then stores an immutable draft in `vendite_banco`.
It does not insert orders/lines, allocate an order number, consume stock,
send notifications or contribute to the preparation queue.

`/api/banco/vendite/<id>/pagamento` provides the existing preview, confirmation,
recovery and owner-only non-emission verification workflow. Payment confirmation
locks shop and sale, reserves dough once, and creates an attempt with `id_vendita`
instead of `id_ordine`. Confirmed fiscal completion closes the sale only.
Uncertain attempts retain reservations and the existing no-replay guarantees.
After verified non-emission a retry reuses the sale's reservation.

The bank POS remains external. Without a fiscal device the UI explicitly offers
registration without a fiscal document, as for the existing payment screen.

The Banco UI defaults to sale/payment. Creating an order is a separate explicit
button leading to the existing order form. A draft can be abandoned for editing
only before stock/payment commitment. Pending fiscal sales can be reopened via
`Vendite da verificare`, including after browser restart. Existing orders are
not migrated or removed. This web-only change does not require a new installer.
