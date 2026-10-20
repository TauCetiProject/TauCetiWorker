import {readFileSync} from 'node:fs';
import {generateKeyPairSync,createHash} from 'node:crypto';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
const key=generateKeyPairSync('rsa',{modulusLength:2048}).privateKey.export({format:'pem',type:'pkcs8'});
const hash=s=>createHash('sha256').update(s).digest('hex');
export const repository={id:'100',name:'org/repo',installation:'200'};
export async function runtime(handler,bindings={},options={}){
 return new Miniflare(convertV4MiniflareOptions({...options,modules:true,script:readFileSync('dist/index.js','utf8'),compatibilityDate:'2026-10-11',durableObjects:{REPOSITORIES:{className:'RepositoryState',useSQLite:true},INSTALLATIONS:{className:'InstallationScheduler',useSQLite:true}},r2Buckets:['ARCHIVE'],queueProducers:{EVENTS:'events'},bindings:{ALLOWED_REPOSITORIES:JSON.stringify([repository]),GITHUB_APP_ID:'42',GITHUB_INSTALLATION_ID:'200',GITHUB_PRIVATE_KEY:String(key),WEBHOOK_SECRET:'secret',CLIENT_TOKENS:JSON.stringify([{hash:hash('client'),repositories:['org/repo']}]),ADMIN_TOKEN_HASH:hash('admin'),...bindings},outboundService:async req=>{
  const u=new URL(req.url);
  if(u.pathname.endsWith('/access_tokens'))return Response.json({token:'installation-read-token',expires_at:new Date(Date.now()+3600000).toISOString()},{status:201});
  return handler(req,u);
 }}));
}
export const response=(body,api='graphql',remaining=4999,status=200)=>Response.json(body,{status,headers:{'x-ratelimit-resource':api,'x-ratelimit-remaining':String(remaining),'x-ratelimit-limit':'5000','x-ratelimit-reset':String(Math.floor(Date.now()/1000)+300)}});
export const send=(stub,path,body)=>stub.fetch('https://internal'+path,{method:'POST',body:JSON.stringify(body)});
