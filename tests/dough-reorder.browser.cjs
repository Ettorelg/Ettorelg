const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||undefined,headless:true});
 try {
  for(const touch of [false,true]){
   const page=await browser.newPage({viewport:{width:390,height:700},hasTouch:touch});
   await page.setContent('<style>#stocks{display:flex;width:360px;overflow:auto;gap:8px}.card{flex:0 0 100px;height:60px;touch-action:none;user-select:none}</style><div id="stocks">'+[0,1,2,3,4].map(i=>`<div class="card" tabindex="0" data-stock-index="${i}">Classico<br>Formato ${i}</div>`).join('')+'</div>');
   await page.addScriptTag({path:path.resolve('static/dough-reorder.js')});
   await page.evaluate(()=>{window.saves=0;AlphaDoughReorder(document.querySelector('#stocks'),{onFinish:save=>{if(save)window.saves++}})});
   const order=()=>page.locator('.card').evaluateAll(cards=>cards.map(c=>Number(c.dataset.stockIndex)));
   await page.mouse.move(300,30);await page.mouse.move(40,30);
   assert.equal(await page.locator('#stocks').evaluate(el=>el.scrollLeft),0,'Hover must not scroll');
   if(touch){
    const cdp=await page.context().newCDPSession(page);
    await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:45,y:30}]});
    await page.waitForTimeout(450);
    await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:280,y:30}]});
    await page.waitForTimeout(100);
    await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
   }else{
    await page.mouse.move(45,30);await page.mouse.down();await page.mouse.move(280,30,{steps:12});await page.waitForTimeout(100);await page.mouse.up();
   }
   assert.deepEqual(await order(),[1,2,0,3,4],touch?'Touch long press reorder':'Mouse reorder');
   assert.equal(await page.evaluate(()=>window.saves),1,'Save exactly once after dropping');
   await page.locator('[data-stock-index="0"]').focus();await page.keyboard.press('Alt+ArrowLeft');
   assert.deepEqual(await order(),[1,0,2,3,4]);
   await page.close();console.log((touch?'Touch':'Mouse')+' reorder, hover and keyboard: passed');
  }
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exit(1)});
