# Alpha Menu per Windows 1.1.10

App Windows 10/11 x64 con logo Alpha Menu, finestra WebView2 dedicata,
sessione persistente e bridge ESC/POS integrato (stessa implementazione del PC).
Richiede Internet e stampanti Ethernet sulla stessa rete, porta TCP 9100.
Non modifica i dati degli ordini o le impostazioni esistenti durante l'installazione.

## Uso

1. Installa `AlphaMenu-Setup-1.1.10.exe` e apri Alpha Menu dal desktop.
2. Accedi con l'account titolare/staff.
3. Menu **Alpha Menu → Stampanti e pagamenti**: configura gli IP.
4. **Stato collegamento stampa** verifica il bridge, non la raggiungibilità fisica delle stampanti.
5. Mantieni l'app aperta per la stampa automatica, da attivare nel banco.

Se un bridge versione 14 con capacità fiscale è aperto, viene riutilizzato.
Un bridge precedente o senza capacità fiscale viene segnalato: chiuderlo e riaprire l'app.
Evitare più banchi con stampa automatica attiva: potrebbero stampare duplicati.
Il riconoscimento chiamate richiede l'app Android/centralino; questa versione non
legge chiamate Windows. L'emissione Epson FP-81II RT richiede modalità reale
esplicitamente abilitata e reparti/totalizzatori verificati con il tecnico.
La modalità prova è predefinita e non trasmette documenti.
Vedi `../docs/fiscal-checkout.md` per il collaudo e gli esiti incerti.

## Aggiornamenti

Il menu **Cerca aggiornamenti** legge `/static/windows/latest.json` via HTTPS.
Il setup viene scaricato solo dallo stesso sito, verificato SHA-256 e avviato
solo dopo conferma. Gli ordini non salvati vanno completati prima di aggiornare.
Il sito si aggiorna senza reinstallare l'app. Il setup non è firmato Authenticode:
Windows potrebbe mostrare un avviso SmartScreen; non disabilitare le protezioni.

## Build

Python 3.12 x64, dipendenze di `requirements.txt`, Inno Setup (build provata con
6.4.3). Eseguire `build.ps1 -Python <python-del-venv> -Compiler <ISCC.exe>`.
Lo script usa il logo esistente, include il bootstrapper WebView2 firmato Microsoft
e genera il setup in `static/windows/`. Installazione per utente, senza admin;
WebView2 richiede Internet se assente. Disinstallazione dalle Impostazioni Windows.
Il profilo/login in `%LOCALAPPDATA%\AlphaMenu` è conservato negli aggiornamenti.

Per una nuova release aggiornare VERSION, AppVersion e OutputBaseFilename,
ricompilare e aggiornare versione/percorso/hash nel manifest nello stesso commit.
Conservare i setup precedenti. `python -m unittest discover -s windows-app` verifica
la validazione aggiornamenti; `AlphaMenu.exe --smoke-test` apre una finestra nascosta
con profilo separato e verifica pagina e bridge dal vero WebView2, senza stampare.
