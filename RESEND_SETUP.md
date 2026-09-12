# Email tramite Resend

Il trasporto HTTPS serve recupero password, richieste QR e promemoria licenza.

1. In Resend aggiungere un dominio di invio e verificarlo inserendo presso il
   gestore DNS gli esatti record forniti da Resend. Non sostituire i record del sito
   o della posta esistenti. Un sottodominio dedicato isola la configurazione.
2. Creare una chiave API con permesso di invio per il dominio verificato.
3. Nelle variabili Railway impostare:
   - `EMAIL_PROVIDER=resend`
   - `RESEND_API_KEY`: la chiave segreta, esclusivamente nelle variabili Railway.
   - `EMAIL_FROM`: nome e indirizzo sul dominio verificato, ad esempio
     `Alpha Menu <notifiche@mail.alphasystemsrl.it>` se si verifica quel sottodominio.
4. Conservare `QR_ORDER_RECIPIENT=alphasystemsrl@gmail.com` come destinazione
   delle richieste. Il mittente Resend non può essere un indirizzo Gmail proprio.
5. Dopo il deploy provare il recupero password con un account di test e controllare
   sia i log Resend sia la casella destinataria. L'accettazione API non prova la consegna.

Senza `EMAIL_PROVIDER` il codice conserva SMTP. Con Resend selezionato non viene
effettuato fallback SMTP né un retry automatico, per evitare duplicazioni.
Non inserire chiavi API in Git o nei messaggi. Verificare anche privacy e fornitori
del trattamento prima dell'uso in produzione.
