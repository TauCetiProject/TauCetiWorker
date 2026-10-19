// Read existing Bors receipts, never GitHub or the merge-control API.
export const BORS_RECEIPTS = 'https://bors.taucetiproject.org/repositories/1/candidates?base=main';
const sha = (value:any) => typeof value==='string' && /^[a-f0-9]{40}$/.test(value);

export function promotionReceipts(value:any) {
  if(value?.schema!=='tauceti-bors.candidates/v1' || value.repo!=='TauCetiProject/TauCeti' || value.base!=='main' || value.timestamp_unit!=='unix_microseconds' || !Array.isArray(value.candidates)) throw new Error('Invalid Bors receipt envelope');
  const receipts=[];
  for(const candidate of value.candidates) {
    const snapshot=candidate.snapshot, events=candidate.events;
    if(snapshot?.contract!=='tauceti-bors.candidate/v1' || snapshot.base!=='main' || !sha(snapshot.base_sha) || !Array.isArray(snapshot.members) || !Array.isArray(events)) continue;
    for(const event of events) {
      if(event.kind!=='promoted' || !['github.base_push_response','github.base_recovery_read'].includes(event.source) || !Number.isSafeInteger(event.observed_at) || event.observed_at<=0 || event.details?.base!=='main' || event.details.base_sha!==snapshot.base_sha || !sha(event.details.commit)) continue;
      // A green CI result or a staging push alone is never landing evidence.
      if(!events.some((e:any)=>e.kind==='constructed' && e.details?.commit===event.details.commit && e.details.base_sha===snapshot.base_sha)) continue;
      for(const member of snapshot.members) {
        if(Number.isSafeInteger(member.pr) && member.pr>0 && sha(member.head_sha)) receipts.push({number:member.pr,head:member.head_sha,at:event.observed_at,source:event.source==='github.base_push_response'?'bors-main-push-receipt':'bors-main-recovery-observation'});
      }
    }
  }
  return receipts;
}

export async function readPromotionReceipts(fetcher:typeof fetch=fetch) {
  const response=await fetcher(BORS_RECEIPTS,{method:'GET',redirect:'error',signal:AbortSignal.timeout(5000),headers:{'User-Agent':'TauCetiGitHubState/1.0','Accept':'application/json'}});
  if(!response.ok || !response.body) throw new Error('Bors receipts HTTP '+response.status);
  const reader=response.body.getReader(),chunks=[];
  let size=0;
  try {
    for(;;) {
      const {done,value}=await reader.read(); if(done) break;
      size+=value.byteLength;
      if(size>8*1024*1024) throw new Error('Bors receipts exceed size limit');
      chunks.push(value);
    }
  } finally {await reader.cancel();}
  const raw=new Uint8Array(size);let offset=0;
  for(const chunk of chunks) {raw.set(chunk,offset);offset+=chunk.byteLength;}
  return promotionReceipts(JSON.parse(new TextDecoder().decode(raw)));
}
