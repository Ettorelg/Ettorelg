const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const root=path.join(__dirname,'..');
function context(extra={}) {
  const values=new Map();
  const ctx={console,URLSearchParams,setTimeout,clearTimeout,crypto:require('node:crypto').webcrypto,sessionStorage:{getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)},...extra};
  ctx.window=ctx;return vm.createContext(ctx);
}
test('interrupted order response reuses its key; a later new order gets a new key',async()=>{
  const keys=[];let attempt=0;
  const ctx=context({fetch:async(url,options)=>{keys.push(options.headers['Idempotency-Key']);if(++attempt===1)throw Error('connection lost');return {ok:true,clone:()=>({json:async()=>({ok:true})})};}});
  vm.runInContext(fs.readFileSync(path.join(root,'static/order-request.js'),'utf8'),ctx);
  const options={method:'POST',body:'{"prodotti":[1]}',headers:{}};
  await assert.rejects(ctx.AlphaOrders.send('/api/ordini/manuale',options));
  await ctx.AlphaOrders.send('/api/ordini/manuale',options);
  await ctx.AlphaOrders.send('/api/ordini/manuale',options);
  assert.equal(keys[0],keys[1]);assert.notEqual(keys[1],keys[2]);
});
test('all menu languages translate ordering labels and preserve numeric suffix spacing',()=>{
  const code=fs.readFileSync(path.join(root,'static/menu-order-i18n.js'),'utf8');
  for(const lang of ['it','en','fr','de','es','pt','nl','pl','ro','zh']){
    const ctx=context({document:{documentElement:{lang},querySelectorAll:()=>[]}});
    vm.runInContext(code,ctx);
    const text=ctx.menuText('Prenota un ordine');
    assert.ok(text);if(lang!=='it')assert.notEqual(text,'Prenota un ordine');
    assert.ok(ctx.menuText(' prodotto').startsWith(' '));
    assert.ok(ctx.menuError('unknown server error'));
  }
});
test('Google return restores quantities with current prices and discards expired drafts',async()=>{
  const html=fs.readFileSync(path.join(root,'templates/public_menu.html'),'utf8');
  const start=html.indexOf('restoreOrderDraft=()=>{');
  const code=html.slice(start,html.indexOf('      const orderCsrf',start));
  let shown=0;
  const field={value:''};
  const ctx=context({draftKey:'draft',productCards:[{dataset:{orderId:'3',available:'true',orderName:'Pasta',orderPrice:'12.50',orderUnit:'kg'}}],orderCart:new Map(),orderForm:{elements:{nome:{value:'Google name'},email:{value:'google@test.it'},telefono:field,data_richiesta:{},riferimento:{},note:{},salva_cliente:{}}},renderOrderCart:()=>{},checkOrderDate:()=>Promise.resolve(),showOrderStep:()=>{},orderPickupSlot:{value:''},orderPanel:{showModal:()=>shown++}});
  ctx.sessionStorage.setItem('draft',JSON.stringify({saved:Date.now(),items:[{id:3,quantita:1.5,prezzoCentesimi:1}],fields:{nome:'old',telefono:'12345678'},save:true}));
  vm.runInContext(code,ctx);ctx.restoreOrderDraft();
  assert.equal(ctx.orderCart.get(3).quantita,1.5);assert.equal(ctx.orderCart.get(3).prezzoCentesimi,1250);
  assert.equal(field.value,'12345678');assert.equal(ctx.orderForm.elements.nome.value,'Google name');assert.equal(shown,1);
  ctx.orderCart.clear();ctx.sessionStorage.setItem('draft',JSON.stringify({saved:0,items:[{id:3,quantita:1}],fields:{}}));ctx.restoreOrderDraft();assert.equal(ctx.orderCart.size,0);
});
test('manual pickup choices react to quantities and block a full slot',async()=>{
  let amount=2;
  const submit={disabled:false},listeners=new Map(),date={value:'2026-10-01'},slot={value:'',options:[],replaceChildren(...values){this.options=[...values]},add(value){this.options.push(value)}};
  const form={querySelector:()=>submit,addEventListener:(name,fn)=>listeners.set(name,fn)};
  const field={hidden:true},message={textContent:''};
  function Option(label,value){this.label=label;this.value=value}
  const ctx=context({Option,fetch:async url=>({ok:true,json:async()=>({disponibile:amount===1,fasce_ritiro_attive:true,fasce:amount===1?['18:00']:[],minuti_fascia_ritiro:30})})});
  vm.runInContext(fs.readFileSync(path.join(root,'static/order-request.js'),'utf8'),ctx);
  const control=ctx.AlphaOrders.availability({form,date,slot,field,message,quantities:()=>[amount]});
  await control.refresh();assert.equal(submit.disabled,true);assert.equal(slot.options.length,1);
  amount=1;await control.refresh();assert.equal(submit.disabled,false);assert.equal(slot.options[1].value,'18:00');
  assert.equal(slot.options[1].label,'18:00');
  assert.equal(field.hidden,false);
});
