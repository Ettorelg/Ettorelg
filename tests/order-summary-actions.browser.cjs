const {chromium}=require('playwright');
const fs=require('node:fs');
const assert=require('node:assert/strict');
(async()=>{
  const html=fs.readFileSync('templates/fulfillment_dashboard.html','utf8').replace(/\r\n/g,'\n');
  const cardCode=html.slice(html.indexOf('          const done=order.stato'),html.indexOf('\n        }\n      }\n    }',html.indexOf('          const done=order.stato')));
  const nodeCode=html.match(/    function node\(parent[^\n]+/)[0];
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH,headless:true});
  try{
    for(const width of [360,650,1366]){
      const page=await browser.newPage({viewport:{width,height:800}});
      await page.setContent('<style>'+html.match(/<style>([\s\S]*?)<\/style>/)[1]+fs.readFileSync('static/fulfillment-cards.css','utf8')+'</style><div id="groups"><div class="order-grid" id="grid"></div></div>');
      await page.evaluate(({nodeCode,cardCode})=>{
        window.calls=[];
        const setup=`${nodeCode}
          const grid=document.getElementById('grid'),order={id:123,numero:16,nome:'Cliente prova',stato:'da_evadere',ora_richiesta:'12:00',creato_il:'17/09/2026'},expandedOrderIds=new Set(),visibleProducts=()=>[],quantityFormat=new Intl.NumberFormat('it-IT'),AlphaOrderPrint={direct:o=>window.calls.push(['print',o.id])},csrf='test',historyPanel={open:false},status={};
          const AlphaPayment={open:o=>window.calls.push(['payment',o.id])};
          const fetch=async(url,options)=>{window.calls.push([url,JSON.parse(options.body).stato]);return {ok:true,json:async()=>({})}},load=async()=>{},loadHistory=async()=>{};
        `;
        new Function(setup+cardCode)();
      },{nodeCode,cardCode});
      const card=page.locator('.order-card');
      assert.equal(await card.evaluate(e=>e.open),false);
      assert.equal(await page.locator('.actions button').count(),4);
      const widths=await page.locator('.actions button').evaluateAll(es=>es.map(e=>e.getBoundingClientRect().width));
      assert.ok(widths[3]>widths[0]);
      await page.getByRole('button',{name:'Stampa ordine #16',exact:true}).click();
      await page.getByRole('button',{name:'Pronto ordine #16',exact:true}).click();
      page.once('dialog',dialog=>dialog.dismiss());
      await page.getByRole('button',{name:'Annulla ordine #16',exact:true}).click();
      await page.getByRole('button',{name:'Segna evaso ordine #16',exact:true}).focus();
      await page.keyboard.press('Enter');
      assert.deepEqual(await page.evaluate(()=>window.calls),[['print',123],['/api/ordini/123','in_lavorazione'],['payment',123]]);
      assert.equal(await card.evaluate(e=>e.open),false,'Actions must not toggle details');
      await page.locator('.order-summary-main strong').click();
      assert.equal(await card.evaluate(e=>e.open),true);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'No horizontal overflow');
      await page.close();console.log(`Summary actions: ${width}px passed`);
    }
  }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exit(1)});
