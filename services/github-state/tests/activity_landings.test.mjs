import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const bundle=await build({entryPoints:['src/activity_landings.ts'],bundle:true,platform:'node',format:'esm',write:false});
const {promotionReceipts,readPromotionReceipts,BORS_RECEIPTS}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const base='a'.repeat(40),commit='b'.repeat(40),head='c'.repeat(40);
const fixture=()=>({schema:'tauceti-bors.candidates/v1',repo:'TauCetiProject/TauCeti',base:'main',timestamp_unit:'unix_microseconds',candidates:[{
 snapshot:{contract:'tauceti-bors.candidate/v1',base:'main',base_sha:base,members:[{pr:7,head_sha:head}]},
 events:[{kind:'constructed',details:{commit,base_sha:base}},{kind:'promoted',source:'github.base_push_response',observed_at:1792380591874607,details:{base:'main',base_sha:base,commit}}]
}]});
test('promotion receipts retain exact approved PR heads and the actual recorded promotion time',()=>{
 assert.deepEqual(promotionReceipts(fixture()),[{number:7,head,at:1792380591874607,source:'bors-main-push-receipt'}]);
});
test('staging, CI success, other bases and mismatched construction never prove a landing',()=>{
 for(const change of [
  c=>c.events.pop(),c=>c.events[1].kind='status_observed',c=>c.events[1].source='github.staging_push_response',
  c=>c.events[1].details.base='other',c=>c.events[1].details.commit='d'.repeat(40),
  c=>c.events[0].details.base_sha='d'.repeat(40),c=>c.snapshot.members[0].head_sha='',
 ]){const v=fixture();change(v.candidates[0]);assert.deepEqual(promotionReceipts(v),[]);}
 const v=fixture();v.repo='other/repo';assert.throws(()=>promotionReceipts(v));
});
test('receipt fetch is restricted to the existing stored Bors journal and refuses redirects',async()=>{
 const original=globalThis.fetch;
 try{
  globalThis.fetch=async(url,options)=>{assert.equal(url,BORS_RECEIPTS);assert.equal(options.redirect,'error');return Response.json(fixture());};
  assert.equal((await readPromotionReceipts()).length,1);
  globalThis.fetch=async()=>new Response(new Uint8Array(8*1024*1024+1));
  await assert.rejects(readPromotionReceipts(),/size limit/);
 }finally{globalThis.fetch=original;}
});
