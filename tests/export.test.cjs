/* Regression: CSV export always contains every filtered row, not only visible page. */
const fs=require("fs");
const vm=require("vm");
const assert=require("node:assert/strict");
const html=fs.readFileSync("dashboard/index.html","utf8");
const source=html.match(/<script>([\s\S]*?)<\/script>/);
assert(source,"dashboard script missing");

const elements=new Map();
function el(id){
  if(!elements.has(id))elements.set(id,{id,value:"",textContent:"",innerHTML:"",style:{},addEventListener(){},onclick:null});
  return elements.get(id);
}
let lastBlob=null,lastDownload="";
class CaptureBlob {
  constructor(parts) { this.content=parts.join("");lastBlob=this; }
}
const document={
  getElementById:el,
  querySelectorAll:()=>[],
  body:{appendChild(){}},
  createElement:()=>({
    style:{},download:"",href:"",
    click(){lastDownload=this.download;},
    remove(){}
  })
};
const rows=Array.from({length:123},(_,i)=>({
  id:"STOCK:"+String(i).padStart(6,"0"),
  category:i<83?"已上市":"审核中",
  board:i%2?"北交所":"科创板",
  company_name:"测试公司"+i,
  stock_code:String(i).padStart(6,"0"),
  list_date:"2026-11-16",
  business_summary:'生产设备,芯片'
}));
const context=vm.createContext({
  document,Blob:CaptureBlob,URL:{createObjectURL:()=>("blob:mock"),revokeObjectURL(){}},
  setTimeout:fn=>fn(),
  fetch:async()=>({ok:true,json:async()=>({summary:{as_of:"2026-11-16",counts:{已上市:83,审核中:40}},rows,events:[]})})
});
vm.runInContext(source[1],context,{filename:"dashboard/index.html"});
async function run(){
  await new Promise(r=>setImmediate(r));
  assert.equal(el("download").textContent.includes("123"),true,"initial total on button");
  el("next").onclick();el("next").onclick();
  assert.equal(el("count").textContent.includes("第 3"),true,"test is on page 3");
  el("download").onclick();
  assert.equal(lastBlob.content.split("\r\n").length-1,123,"no filter must export all 123 records");
  assert.match(lastDownload,/123_records/);
  el("category").value="已上市";
  vm.runInContext("filter()",context);
  el("next").onclick();
  el("download").onclick();
  assert.equal(lastBlob.content.split("\r\n").length-1,83,"filtered export must include both pages");
  assert.match(lastDownload,/83_records/);
  assert.match(lastBlob.content,/"生产设备,芯片"/,"CSV correctly quotes commas");
  console.log("PASS: 123 unfiltered records and 83 filtered records exported across pages.");
}
run().catch(e=>{console.error(e);process.exit(1)});
