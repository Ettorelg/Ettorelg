# Axon-Micrelec Dado RT / RT30

Requires Alpha Menu Windows 1.1.9 (bridge 15), HTTP port 80 on the same LAN.
Source: supplied G100 communication protocol v5.9, commands 3, 4, 5, X,
0/4, v, a, e, b, :, N, {, E, O, L. Reading verified on D100.137.

Dashboard saves IP, model, category/department mappings, cash/card indexes,
and an explicit verified/live opt-in. Preview never contacts the printer.
Contanti and Carta do not drive or confirm a bank POS transaction.

Configuration reads actual department/payment counts from the device, all
12 VAT slots including nature/ATECO and all 12 header lines. Selected sections
are validated before the first write. Writes require a closed fiscal day and
no open document. Blank optional department/payment fields preserve settings
not exposed for editing. A partial write stops immediately and reports the
confirmed command count; reread before retrying. No automatic fiscal closure.
Header centering is explicit; existing read-back whitespace is preserved until
edited. Font values follow Axon, not Epson. Logo bitmap readback is not
implemented; use the vendor utility for logos.

Sales use server-authoritative amounts and department mappings. Before sending,
the bridge verifies idle state, compatible payment type and department flags.
POS-linked/offline payments and automatic payment discounts are unsupported.
Receipt completion requires a closed document, exactly one increment of the
document number, matching last-document metadata and matching VAT gross total
read using 0/4/z/document. Otherwise the job remains uncertain.

The shared durable local ledger and atomic server claim prevent replay, even
after transport/callback failure. Recover only republishes a saved result;
it never retransmits sale commands. If the application crashes before saving
the printer response, manual device verification is required.

Read-only live check: 60 departments, 20 payments, 12 VAT entries, 12 header
lines at 192.168.2.30. Payment 1=CONTANTI, 4=BANCOMAT, 5=CARTA DI CRED.
These are observed device settings, not universal defaults.
