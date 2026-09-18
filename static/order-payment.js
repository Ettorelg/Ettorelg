window.AlphaPayment = (() => {
  const extraFunctions = new Map();
  const euro = value => Number(value).toLocaleString('it-IT',{style:'currency',currency:'EUR'});
  function element(parent,tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;parent.append(el);return el}
  async function api(url,body,csrf){const local=url.startsWith('http://127.0.0.1:17891/');if(local&&window.pywebview?.api?.fiscal_request){const result=await window.pywebview.api.fiscal_request(new URL(url).pathname,body||{});if(!result.ok)throw Error(result.message||'Operazione locale non riuscita.');return result.data}const response=await fetch(url,{cache:'no-store',...(body?{method:'POST',headers:local?{'Content-Type':'text/plain'}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)}:{})});const data=await response.json();if(!response.ok)throw Error(data.error||data.message||'Operazione non riuscita.');return data}
  async function open(order, {csrf, onComplete=()=>{}, initialPayment='contanti', initialTender=''}={}) {
    if(document.querySelector('.payment-dialog'))return;
    const paymentUrl=(order.sale?'/api/banco/vendite/':'/api/ordini/')+order.id+'/pagamento';
    const dialog=element(document.body,'dialog',undefined,'payment-dialog');dialog.setAttribute('aria-label','Pagamento ordine');
    const head=element(dialog,'header',undefined,'payment-head');element(head,'h2',order.sale?'Pagamento · vendita al banco':'Pagamento · ordine #'+(order.numero||order.id));const close=element(head,'button','✕');close.setAttribute('aria-label','Chiudi pagamento');
    const body=element(dialog,'div',undefined,'payment-body');const left=element(body,'section');const right=element(body,'section',undefined,'payment-controls');
    const feedback=element(dialog,'p','Caricamento ordine…','payment-message');feedback.setAttribute('role','status');
    const tools=element(dialog,'nav',undefined,'payment-tools');tools.setAttribute('aria-label','Funzioni pagamento');
    const preview=element(dialog,'pre',undefined,'payment-preview');preview.hidden=true;
    const submit=element(dialog,'button','Caricamento…','payment-submit');submit.disabled=true;
    let busy=false,info=null,payment=initialPayment==='carta'?'carta':'contanti',discountType='euro',active=null,replace=true;
    const setMessage=(text,error=false)=>{feedback.textContent=text;feedback.classList.toggle('error',error)};
    const setBusy=value=>{busy=value;dialog.querySelectorAll('button,input').forEach(el=>el.disabled=value);if(!value){submit.disabled=!!info?.pagamento; if(payment==='carta'&&tender)tender.disabled=true}};
    close.onclick=()=>{if(!busy)dialog.close()};dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault()});dialog.addEventListener('close',()=>dialog.remove(),{once:true});dialog.showModal();
    let tender,discount,dueLabel,discountLabel,changeLabel;
    const number=input=>Number((input?.value||'0').replace(',','.'));
    function update(){const gross=Number(info.ordine.totale),reduction=discountType==='percent'?Math.round(gross*number(discount))/100:number(discount);const due=Math.round((gross-reduction)*100)/100;discountLabel.textContent='− '+euro(reduction);dueLabel.textContent=euro(due);changeLabel.textContent=euro(payment==='carta'?0:Math.max(0,number(tender)-due));preview.hidden=true}
    function select(input){active=input;replace=true;[tender,discount].forEach(el=>el.classList.toggle('active',el===input))}
    function payload(action){return {action,payment,discount_type:discountType,discount: String(number(discount)),tendered:tender.value?String(number(tender)):null}}
    async function recover(){setBusy(true);try{const attempt=await api(paymentUrl+'/recupera',{},csrf);if(['emesso','registrato'].includes(attempt.state)){await onComplete();dialog.close();return}let route='recover';if(attempt.state==='in_attesa'){if(!confirm('Questo tentativo non è mai stato acquisito per l’invio. Avviare ora l’emissione con gli importi già confermati?'))return;route='emit'}const result=await api('http://127.0.0.1:17891/fiscal/'+route,attempt,csrf);if(!result.ok)throw Error(result.result?.error||'Esito da verificare sul registratore.');await onComplete();dialog.close()}catch(error){setMessage(error.message,true)}finally{setBusy(false)}}
    try {
      info=await api(paymentUrl);
      element(left,'strong',info.ordine.nome||'Ordine');const products=element(left,'ul',undefined,'payment-products');
      for(const row of info.ordine.prodotti){const li=element(products,'li');element(li,'span',row.quantita+' × '+row.nome);element(li,'b',euro(row.totale))}
      const summary=element(left,'div',undefined,'payment-summary');for(const [label,key] of [['Subtotale','gross'],['Sconto','discount'],['Da pagare','due'],['Resto','change']]){const row=element(summary,'p',undefined,key==='due'?'payment-total':'');element(row,'span',label);const val=element(row,'b',key==='gross'?euro(info.ordine.totale):'');if(key==='discount')discountLabel=val;if(key==='due')dueLabel=val;if(key==='change')changeLabel=val}
      const methods=element(right,'div',undefined,'payment-methods');
      for(const [value,label] of [['contanti','💶 Contanti'],['carta','💳 Carta']]){const button=element(methods,'button',label);button.dataset.method=value;button.setAttribute('aria-pressed',String(value===payment));button.onclick=()=>{payment=value;methods.querySelectorAll('button').forEach(el=>el.setAttribute('aria-pressed',String(el===button)));tender.disabled=value==='carta';select(value==='carta'?discount:tender);update()}}
      element(right,'p','Carta: conferma sul POS esterno prima di registrare.','payment-notice');
      let label=element(right,'label','Importo ricevuto · contanti');tender=element(label,'input');tender.inputMode='decimal';tender.placeholder='Importo esatto se vuoto';tender.value=payment==='contanti'?String(initialTender||''):'';
      label=element(right,'label','Sconto sul totale');discount=element(label,'input');discount.inputMode='decimal';discount.value='0';
      const types=element(right,'div',undefined,'payment-discount-types');for(const [value,text] of [['euro','Sconto €'],['percent','Sconto %']]){const button=element(types,'button',text);button.setAttribute('aria-pressed',String(value===discountType));button.onclick=()=>{discountType=value;types.querySelectorAll('button').forEach(el=>el.setAttribute('aria-pressed',String(el===button)));select(discount);update()}}
      for(const input of [tender,discount]){input.onfocus=()=>select(input);input.oninput=()=>{replace=false;update()}}
      const keypad=element(right,'div',undefined,'payment-keypad');for(const key of ['7','8','9','4','5','6','1','2','3','C','0',',']){const button=element(keypad,'button',key);button.setAttribute('aria-label',key==='C'?'Cancella importo':key);button.onclick=()=>{if(!active||active.disabled)return;if(key==='C'){active.value='';replace=true}else{const value=(replace?'':active.value)+key;if(/^\d{0,6}(,\d{0,2})?$/.test(value)){active.value=value;replace=false}}update()}}
      select(tender);update();
      if(!order.sale){const print=element(tools,'button','🖨 Comanda');print.onclick=()=>AlphaOrderPrint.direct(info.ordine);}
      const reset=element(tools,'button','Azzera sconto');reset.onclick=()=>{discount.value='0';update()};
      for(const [label,handler] of extraFunctions){const button=element(tools,'button',label);button.onclick=()=>handler(info.ordine)}
      const mode=info.config?.status;
      submit.textContent=!info.config?.brand?'Registra pagamento · senza fiscale':mode==='live'?'Incassa ed emetti documento':'Prova pagamento · nessuna emissione';
      setMessage(!info.config?.brand?'Nessun registratore configurato: il pagamento non produce un documento fiscale.':mode==='live'?'Modalità reale · il documento verrà emesso dal registratore '+info.config.model+'.':'Modalità prova · genera l’anteprima, senza stampare e senza evadere l’ordine.');
      submit.disabled=false;
      if(info.pagamento){submit.disabled=true;const done=['emesso','registrato'].includes(info.pagamento.stato);setMessage(done?'Pagamento già completato.':'Tentativo fiscale già presente. Non riprovare l’emissione: verifica il registratore e recupera l’esito sullo stesso PC.',!done);if(!done){const recoverButton=element(tools,'button','Recupera esito');recoverButton.onclick=recover;if(info.can_resolve){const verify=element(tools,'button','Verifica manuale · titolare');verify.onclick=async()=>{const confirmation=prompt('Solo dopo aver verificato il giornale del registratore e risolto eventuali documenti aperti con il tecnico. Se il documento esiste NON sbloccare: recupera l’esito. Per confermare che NON è stato emesso, scrivi CONFERMO NON EMESSO');if(confirmation!=='CONFERMO NON EMESSO')return;const note=prompt('Descrivi la verifica effettuata sul registratore (minimo 12 caratteri):');if(!note)return;setBusy(true);try{await api(paymentUrl+'/verifica-non-emesso',{confirmation,note},csrf);dialog.close();dialog.remove();await open(order,{csrf,onComplete})}catch(error){setMessage(error.message,true)}finally{setBusy(false)}}}}}
      submit.onclick=async()=>{
        setBusy(true);
        try{
          const quote=await api(paymentUrl,payload('preview'),csrf);
          dueLabel.textContent=euro(quote.totals.due);changeLabel.textContent=euro(quote.totals.change);
          if(info.config?.brand&&mode!=='live'){preview.textContent=typeof quote.xml==='string'?quote.xml:JSON.stringify(quote.xml,null,2);preview.hidden=false;setMessage('Anteprima verificata. Nessun documento emesso; ordine invariato.');return}
          if(!confirm((info.config?.brand?'Emettere il documento fiscale':'Registrare il pagamento senza documento fiscale')+' di '+euro(quote.totals.due)+' con '+payment+'?'+(payment==='carta'?' Conferma solo dopo l’esito positivo del POS.':'')))return;
          if(info.config?.brand){const health=await api('http://127.0.0.1:17891/health');if(!health.fiscal)throw Error('Aggiorna Alpha Menu Windows alla versione 1.1.0 e chiudi il vecchio programma di stampa.')}
          const result=await api(paymentUrl,payload('confirm'),csrf);
          if(result.job){info.pagamento={id:result.id,stato:'in_attesa'};setMessage('Emissione in corso. Non chiudere l’app e non ripetere l’operazione.');const printed=await api('http://127.0.0.1:17891/fiscal/emit',{id:result.job.id,secret:result.job.secret},csrf);if(!printed.ok)throw Error(printed.result?.error||'Emissione non confermata. Verifica il registratore.');}
          await onComplete();dialog.close();
        }catch(error){setMessage(error.message+' Se l’invio è già iniziato, chiudi e riapri il pagamento per recuperare l’esito: non emettere di nuovo.',true)}finally{setBusy(false)}
      };
    } catch(error){setMessage(error.message,true)}
  }
  return {open,registerFunction:(label,handler)=>extraFunctions.set(label,handler)};
})();
