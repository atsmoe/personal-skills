const assert = require('node:assert/strict');
const {test} = require('node:test');
const {verifyCells,applyOperations,exportWorkbook,loadImageParts} = require('../skills/photo-purchase-ledger/scripts/tencent_bridge.js');

test('STRING cells ignore Tencent default number_value=0',()=>{
  verifyCells({cells:[{row:1,col:0,value_type:'STRING',string_value:'subtotal',number_value:0}]},{'1,0':'subtotal'},{start_row:1,end_row:1,start_col:0,end_col:0});
});
test('unknown insertion is not retried without verified reconciliation',async()=>{
  let writes=0;
  await assert.rejects(applyOperations({plan:{target:{file_id:'test',sheet_id:'sheet'},plan_hash:'a',operations:[]},
    checkpoint:{plan_hash:'a',cursor:0,in_flight:0},refresh:async()=>({}),call:async()=>{writes++;}}),/uncertain/);
  assert.equal(writes,0);
});
test('full-export reconciliation skips an insertion that already completed',async()=>{
  let writes=0;
  const result=await applyOperations({plan:{target:{file_id:'test',sheet_id:'sheet'},plan_hash:'a',operations:[{}]},
    checkpoint:{plan_hash:'a',cursor:0,in_flight:0},refresh:async()=>({checkpoint:{cursor:1,in_flight:null}}),call:async()=>{writes++;}});
  assert.equal(writes,0);assert.equal(result.acceptance_verified,false);
});
test('failed export cannot fall back to a stale workbook',async()=>{
  let downloads=0;
  await assert.rejects(exportWorkbook({fileId:'test',sleep:async()=>{},
    call:async name=>({structuredContent:name==='manage_export_file'?{task_id:'test'}:{error:'export failed'}}),
    download:async()=>{downloads++;}}),/error/);
  assert.equal(downloads,0);
});
test('image payload is assembled without returning it to the model',async()=>{
  const content=await loadImageParts(async r=>({content:r.offset===0?'abcd':'efgh',total_chars:8}),{});
  assert.equal(content,'abcdefgh');
});
