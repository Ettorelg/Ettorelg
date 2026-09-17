# Pagamento e collaudo Epson FP-81II RT

Il tasto verde apre il pagamento, anche dallo storico e dal banco staff.
Supporta contanti, carta (POS esterno), sconto sul totale in euro o percentuale,
importo ricevuto e resto. La barra funzioni è estensibile con
`AlphaPayment.registerFunction(label, handler)`; non esegue codice da configurazioni utente.

## Configurazione

Dashboard → Stampanti e pagamenti (sotto Lingue): IP comande, riepilogo,
categorie, marca/modello RT, IP/porta HTTP, operatore, reparti IVA,
totalizzatori contanti/carta. I dati delle stampanti non sono impostati nell'app.
Altre marche sono memorizzabili ma non emettono documenti. Per ora l'emissione
è limitata a FP-81II RT. Windows 1.1.3 include il trasporto; Android non ancora.

Prima salvare in **Prova** e usare **Verifica collegamento** dall'app Windows
sulla stessa LAN: invia solo `queryPrinterStatus`, non una vendita.
La prova pagamento genera XML dal totale server, senza inviarlo e senza chiudere l'ordine.
Per il collaudo fisico verificare firmware, ePOS Fiscal/fpmate.cgi, modalità fiscale,
reparti IVA e totalizzatori con il tecnico; poi abilitare **Reale** e confermare la verifica.
Premere **Incassa ed emetti documento** e confermare importo/metodo per emettere.
La selezione Carta non addebita il POS: confermare prima il pagamento sul terminale.
La prova fisica non è stata eseguita durante lo sviluppo e non vengono emessi documenti automaticamente.

## Protezioni e riconciliazione

- Importi calcolati in Decimal dal server; prezzi per quantità non rappresentabili vengono rifiutati.
- XML SOAP verso il solo IP privato configurato, endpoint `/cgi-bin/fpmate.cgi`.
- Un tentativo attivo per ID interno ordine, non per numero progressivo.
- Acquisizione atomica sul server prima dell'invio, valida anche tra PC diversi.
- Esito salvato sul PC prima di riportarlo al server: recuperarlo non ristampa.
- Il pagamento passa da `in_attesa` a `inviato`, poi `emesso` o `incerto`.
- Solo risposta Epson positiva con numero/data e importo corrispondente chiude l'ordine.
- Nessun reinvio automatico dopo timeout o errore. Un esito incerto blocca ulteriori emissioni del locale.
- Il titolare può archiviare come `non_emesso` solo dichiarando esplicitamente di aver verificato
  il giornale del registratore e registrando una nota. Dopo un invio servono almeno cinque minuti.
  Questa operazione non annulla documenti fiscali e non va usata se un documento è stato emesso.
- Gli annulli fiscali, i resi, i pagamenti misti, i buoni e le chiusure Z non sono implementati.
- Senza registratore il pagamento è registrato separatamente e l'ordine evaso; NON viene emesso
  un documento fiscale. Il totale originale dell'ordine resta invariato; netto/sconto/resto sono nel registro
  pagamenti e non ancora nel riepilogo statistico degli incassi.

Il registro locale è `%LOCALAPPDATA%/AlphaMenu/fiscal-jobs.sqlite3` e non va cancellato per riprovare.
Le credenziali per ogni tentativo sono casuali a 256 bit. Le due API di callback sono autenticabili
senza sessione tramite queste credenziali, gli altri endpoint richiedono sessione e CSRF.

Riferimento: Epson ePOS Fiscal Print Solution Development Guide Rev T, sezioni 2.2,
5.4.1, 5.4.12, 5.4.14 e 5.9.15:
https://download4.epson.biz/sec_pubs/bs/pdf/ePOS%20Fiscal%20Print%20Solution%20Development%20Guide%20Rev%20T.pdf
