# Alpha Menu Android

APK privato per il banco ordini Alpha Menu.

Funzioni della prima versione:

- apre il banco evasione e la creazione manuale degli ordini;
- sincronizza sul telefono la rubrica clienti del negozio;
- riconosce il numero delle chiamate in ingresso tramite il ruolo Android di identificazione chiamate;
- apre `Aggiungi ordine` con cliente o numero già compilato;
- espone sul telefono il bridge locale usato dal banco per inviare le comande alle stampanti ESC/POS di rete sulla porta 9100.

## Prima configurazione

1. Installa l'APK consentendo temporaneamente l'installazione da questa origine.
2. Accedi ad Alpha Menu nell'app.
3. Premi **Attiva riconoscimento chiamate** e conferma la richiesta Android.
4. Premi **Sincronizza clienti**.
5. Collega telefono e stampanti alla stessa rete Wi-Fi.

La rubrica viene aggiornata anche automaticamente quando si apre una pagina Alpha Menu autenticata.

## Menu app e aggiornamenti (1.3.0)

I comandi del telefono sono nel menu **⋮**: riconoscimento chiamate, sincronizzazione,
prova banco, stato collegamento e aggiornamenti. **Aggiornamenti → Cerca aggiornamenti**
consulta `/static/android/latest.json` via HTTPS. Il download usa il gestore Android;
aprire la notifica completata oppure **Aggiornamenti → Apri download** per installare.
Android richiede la conferma dell'installazione e, se necessario, il permesso per l'origine.

Per pubblicare una versione: incrementare versionCode/versionName, compilare con la
stessa chiave delle versioni distribuite, copiare l'APK in `static/android/` e aggiornare
`latest.json` nello stesso commit. Conservare i vecchi APK per i download già avviati.
La versione privata attuale mantiene la firma delle precedenti build debug distribuite;
non cambiare la chiave durante un aggiornamento.
