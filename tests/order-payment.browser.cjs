const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROME_PATH,headless:true});
 try{
  for(const [width,mode] of [[390,'preview'],[1366,'live']]){
   const page=await browser.newPage({viewport:{width,height:850}});const calls=[];
   const order={id:123,numero:16,nome:'CLIENTE PROVA',totale:'12.50',prodotti:[{nome:'PIZZA MARGHERITA · DOPPIA',quantita:'1',totale:'12.50'}]};
   await page.route('**/*',async route=>{const req=route.request(),url=req.url();calls.push([req.method(),url]);let data={};
    if(url.endsWith('/health'))data={fiscal:true};
    else if(url.endsWith('/fiscal/emit'))data={ok:true};
    else if(req.method()==='GET')data={ordine:order,config:{brand:'epson',status:mode},pagamento:null};
    else{const body=req.postDataJSON();data=body.action==='preview'?{totals:{due:'11.25',change:'8.75'},xml:'<printerFiscalReceipt />'}:{id:'attempt',job:{id:'attempt',secret:'secret'}}}
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
   });
   await page.goto('https://menu.alphasystemsrl.it/test-payment');
   await page.setContent('<html><head></head><body style="background:#0d1727"><button>Banco</button></body></html>');
   await page.addStyleTag({path:path.resolve('static/order-payment.css')});
   await page.addScriptTag({path:path.resolve('static/order-payment.js')});
   await page.evaluate(order=>{window.completed=0;AlphaPayment.open(order,{csrf:'test',onComplete:()=>{window.completed++}})},order);
   await page.getByLabel('Sconto sul totale').fill('10');
   await page.getByRole('button',{name:'Sconto %',exact:true}).click();
   await page.getByLabel('Importo ricevuto · contanti').fill('20');
   await page.screenshot({path:`windows-app/build/payment-${width}.png`,fullPage:true});
   assert.ok(await page.locator('.payment-dialog').evaluate(el=>el.scrollWidth<=el.clientWidth),'No modal overflow');
   page.on('dialog',dialog=>dialog.accept());
   await page.locator('.payment-submit').click();
   if(mode==='preview'){
    await page.getByText('Anteprima verificata. Nessun documento emesso; ordine invariato.').waitFor();
    assert.equal(calls.filter(([,url])=>url.endsWith('/fiscal/emit')).length,0);
    assert.equal(await page.evaluate(()=>window.completed),0);
   }else{
    await page.waitForFunction(()=>window.completed===1);
    assert.equal(calls.filter(([,url])=>url.endsWith('/fiscal/emit')).length,1);
   }
   await page.close();console.log(`${width}px ${mode} payment passed`);
  }
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exit(1)});
