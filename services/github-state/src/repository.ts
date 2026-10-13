import { DurableObject } from 'cloudflare:workers';
import { Env, EventPointer, Query, Result, json, queryKey, repository, stub, validateQuery, parseGitHub } from './protocol';
import { comment, issue, pr, status } from './normalize';
import { batchLimit } from './catalog';
import { Lane, LANES, laneAt, settings } from './reconciliation';

export class RepositoryState extends DurableObject<Env> {
  private inflightQueries=new Map<string,Promise<Result>>();
  private completedQueries=new Map<string,{result:Result,expires:number}>();
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx,env);
    ctx.storage.sql.exec(`CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE IF NOT EXISTS resources(kind TEXT,id TEXT,parent TEXT,data TEXT,updated TEXT,observed INTEGER,revision INTEGER,tombstone INTEGER DEFAULT 0,PRIMARY KEY(kind,id));
      CREATE INDEX IF NOT EXISTS resource_parent ON resources(kind,parent,tombstone);
      CREATE TABLE IF NOT EXISTS collections(key TEXT PRIMARY KEY,operation TEXT,args TEXT,complete INTEGER,verified INTEGER,revision INTEGER,etag TEXT);
      CREATE TABLE IF NOT EXISTS members(collection TEXT,id TEXT,PRIMARY KEY(collection,id));
      CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,event TEXT,received INTEGER);
      CREATE TABLE IF NOT EXISTS changes(revision INTEGER PRIMARY KEY,at INTEGER,kind TEXT,id TEXT,tombstone INTEGER);
      CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY,query TEXT,priority INTEGER,due INTEGER,started INTEGER,cursor TEXT,page INTEGER DEFAULT 1,api TEXT,etag TEXT,attempt INTEGER DEFAULT 0,dirty INTEGER DEFAULT 0,generation INTEGER DEFAULT 0,start_generation INTEGER DEFAULT 0);
      CREATE INDEX IF NOT EXISTS jobs_due ON jobs(due,priority);
      CREATE TABLE IF NOT EXISTS demand(key TEXT PRIMARY KEY,at INTEGER);
      CREATE TABLE IF NOT EXISTS cursors(id TEXT PRIMARY KEY,expires INTEGER,payload TEXT);
      CREATE TABLE IF NOT EXISTS head_prs(sha TEXT,number TEXT,PRIMARY KEY(sha,number));`);
    this.ctx.storage.sql.exec(`CREATE TABLE IF NOT EXISTS repair_metrics(at INTEGER,event TEXT,lane TEXT,operation TEXT,value INTEGER DEFAULT 1);
      CREATE INDEX IF NOT EXISTS repair_metrics_at ON repair_metrics(at);
      CREATE TABLE IF NOT EXISTS query_metrics(operation TEXT,coverage TEXT,requests INTEGER,last_at INTEGER,PRIMARY KEY(operation,coverage));`);
    const columns=new Set(this.ctx.storage.sql.exec<any>('PRAGMA table_info(jobs)').toArray().map(c=>c.name));
    for (const name of ['dirty','generation','start_generation']) if (!columns.has(name)) {
      this.ctx.storage.sql.exec('ALTER TABLE jobs ADD COLUMN '+name+' INTEGER DEFAULT 0');
      if (name==='dirty') this.ctx.storage.sql.exec('UPDATE jobs SET dirty=1 WHERE priority<=5 AND due<=?',Date.now());
    }
    for (const [name,type] of Object.entries({lane:"TEXT DEFAULT 'verification'",lease_id:'TEXT',lease_until:'INTEGER DEFAULT 0',requested_at:'INTEGER',created_at:'INTEGER',wait_reason:'TEXT',nested:'TEXT'})) if (!columns.has(name)) this.ctx.storage.sql.exec('ALTER TABLE jobs ADD COLUMN '+name+' '+type);
    this.ctx.storage.sql.exec('CREATE INDEX IF NOT EXISTS jobs_lane_due ON jobs(lane,due,lease_until)');
    if (!this.meta('reconciliation_version')) {
      this.ctx.storage.sql.exec("UPDATE jobs SET created_at=?,lane=CASE WHEN EXISTS(SELECT 1 FROM demand d WHERE d.key=jobs.key AND d.at>?) AND NOT EXISTS(SELECT 1 FROM collections c WHERE c.key=jobs.key AND c.complete=1) THEN 'requested' WHEN dirty=1 OR priority<=5 THEN 'active' WHEN key LIKE 'recent_%' THEN 'archive' ELSE 'verification' END",Date.now(),Date.now()-300000);
      this.setMeta('reconciliation_version',2);
    }
    if (!this.meta('head_index_version')) {
      this.ctx.storage.sql.exec("INSERT OR IGNORE INTO head_prs SELECT json_extract(data,'$.headRefOid'),id FROM resources WHERE kind='pr' AND id NOT LIKE 'pr:%' AND json_extract(data,'$.headRefOid') IS NOT NULL");
      this.setMeta('head_index_version',1);
    }
    if (!this.meta('workflow_scope_version')) {
      this.setMeta('workflow_since',new Date(Date.now()-3600000).toISOString());
      this.setMeta('workflow_scope_version',1);
      this.ctx.storage.sql.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=NULL,etag=NULL,due=? WHERE key=?',Date.now(),queryKey({operation:'workflow_runs'}));
    }
    // Delta surveys must retain their place ahead of a large bootstrap backlog.
    this.ctx.storage.sql.exec("UPDATE jobs SET priority=0 WHERE key LIKE 'pr_delta:%' OR key LIKE 'issue_delta:%'");
  }
  meta(key: string): any { const row = this.ctx.storage.sql.exec<any>('SELECT value FROM meta WHERE key=?',key).toArray()[0]; return row ? JSON.parse(row.value) : undefined; }
  setMeta(key:string,value:unknown): void { this.ctx.storage.sql.exec('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',key,JSON.stringify(value)); }
  revision(): number { return this.meta('revision') || 0; }
  nextRevision(kind:string,id:string,tombstone=false): number {
    const revision = this.revision()+1; this.setMeta('revision',revision);
    this.ctx.storage.sql.exec('INSERT INTO changes VALUES(?,?,?,?,?)',revision,Date.now(),kind,id,Number(tombstone)); return revision;
  }
  async encode(data: unknown, expires?:number): Promise<string> {
    const text = JSON.stringify(data);
    if (new TextEncoder().encode(text).length < 1500000) return text;
    const key = `resources/${this.meta('repository').id}/${crypto.randomUUID()}`;
    // Active resources must remain readable for their entire SQL row lifetime.
    // Raw deliveries, staging pages, and snapshots have separate expiry rules.
    await this.env.ARCHIVE.put(key,text,{customMetadata:{kind:expires?'cursor':'resource',...(expires?{expires:String(expires)}:{})}});
    return JSON.stringify({$r2:key});
  }
  async decode(text: string): Promise<any> {
    const data = JSON.parse(text);
    if (data?.$r2) { const object = await this.env.ARCHIVE.get(data.$r2); if (!object) throw new Error('resource unavailable'); return object.json(); }
    return data;
  }
  async put(kind:string,id:string,data:any,started=Date.now(),deleted=false,parent='',fromEvent=false): Promise<boolean> {
    let old = this.ctx.storage.sql.exec<any>('SELECT * FROM resources WHERE kind=? AND id=?',kind,id).toArray()[0];
    const updated = data?.updatedAt || data?.updated_at || data?.created_at || data?.startedAt || '';
    // A response begun before a webhook cannot overwrite that event, even if HTTP finishes later.
    if (!fromEvent && old?.observed > started) return false;
    if (old?.updated && updated && old.updated > updated) return false;
    if (old?.updated && updated===old.updated && old.tombstone && !deleted) return false;
    const encoded = await this.encode(data);
    // R2 encoding yields: repeat the guard against events received during that await.
    old=this.ctx.storage.sql.exec<any>('SELECT * FROM resources WHERE kind=? AND id=?',kind,id).toArray()[0];
    if (!fromEvent && old?.observed>started || old?.updated && updated && old.updated>updated || old?.updated && updated===old.updated && old.tombstone && !deleted) return false;
    if (kind==='pr' && /^[1-9][0-9]*$/.test(id) && data?.headRefOid) this.ctx.storage.sql.exec('INSERT OR IGNORE INTO head_prs VALUES(?,?)',data.headRefOid,id);
    if (old?.data===encoded && Boolean(old.tombstone)===deleted) {
      this.ctx.storage.sql.exec('UPDATE resources SET observed=? WHERE kind=? AND id=?',Date.now(),kind,id);
      return true;
    }
    const rev = this.nextRevision(kind,id,deleted);
    this.ctx.storage.sql.exec('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET parent=excluded.parent,data=excluded.data,updated=excluded.updated,observed=excluded.observed,revision=excluded.revision,tombstone=excluded.tombstone',kind,id,parent,encoded,updated,Date.now(),rev,Number(deleted));
    return true;
  }
  membership(key:string,id:string,included:boolean): void {
    const exists=this.ctx.storage.sql.exec('SELECT id FROM members WHERE collection=? AND id=?',key,id).toArray().length>0;
    if (exists===included) return;
    if (included) this.ctx.storage.sql.exec('INSERT INTO members VALUES(?,?)',key,id);
    else this.ctx.storage.sql.exec('DELETE FROM members WHERE collection=? AND id=?',key,id);
    this.nextRevision('collection',key);
  }
  id(operation:string,row:any): string { return String(row.number || row.id || row.node_id || row.ref || row.context || row.sha || row.headRefOid || 'value'); }
  async enqueue(q:Query,priority=20,due=Date.now(),dirty=false): Promise<void> {
    if (q.operation==='progress') return;
    const key=queryKey(q);
    const prior=this.ctx.storage.sql.exec<any>('SELECT * FROM jobs WHERE key=?',key).toArray()[0];
    const lane=this.lane(q,dirty || Boolean(prior?.dirty),priority),requested=lane==='requested'?(prior?.requested_at || Date.now()):prior?.requested_at || null;
    this.ctx.storage.sql.exec('INSERT INTO jobs(key,query,priority,due,dirty,generation,lane,requested_at,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET priority=excluded.priority,lane=excluded.lane,requested_at=excluded.requested_at,dirty=MAX(dirty,excluded.dirty),generation=generation+excluded.generation' + (dirty ? ",due=CASE WHEN wait_reason IS NOT NULL OR attempt>0 THEN due ELSE MIN(due,excluded.due) END" : ''),key,JSON.stringify({...q,allow_observed:undefined}),priority,due,Number(dirty),Number(dirty),lane,requested,Date.now());
    if (!prior) this.metric('arrival',q,lane);
    const alarm=await this.ctx.storage.getAlarm();
    const scheduled=this.ctx.storage.sql.exec<any>('SELECT due FROM jobs WHERE key=?',key).one().due;
    if (!alarm || alarm>scheduled) await this.ctx.storage.setAlarm(Math.max(Date.now()+100,scheduled));
  }
  complete(q:Pick<Query,'operation'|'args'>): boolean { return this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0; }
  metric(event:string,q:Query,lane:Lane,value=1): void { this.ctx.storage.sql.exec('INSERT INTO repair_metrics VALUES(?,?,?,?,?)',Date.now(),event,lane,q.operation,value); }
  lane(q:Query,dirty=false,priority=20): Lane {
    const demanded=(this.ctx.storage.sql.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q)).toArray()[0]?.at || 0)>Date.now()-300000;
    if (demanded && !this.complete(q)) return 'requested';
    if (dirty || priority<=5 || q.operation.endsWith('_delta')) return 'active';
    return q.operation.startsWith('recent_') || this.dormant(q)?'archive':'verification';
  }
  importantWorkflow(id:string): boolean {
    const row=this.ctx.storage.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=?','workflow_runs',id).toArray()[0];
    return Boolean(row && ['staging','trying'].includes(JSON.parse(row.data).head_branch));
  }
  importantPending(id:string): boolean {
    return this.importantWorkflow(id) && (!this.meta('workflow_finished:'+id) || Boolean(this.meta('important_final_pending:'+id)));
  }
  noteWorkflow(id:string,row:any): void {
    this.setMeta('workflow_finished:'+id,row.status==='completed');
    this.setMeta('workflow_seen_at:'+id,Date.now());
    if(this.importantWorkflow(id)) {
      if(row.status!=='completed') this.setMeta('important_active:'+id,true);
      else if(this.meta('important_active:'+id)) this.setMeta('important_final_pending:'+id,true);
    }
  }
  runAttempt(id:string): number {
    const known=this.meta('workflow_attempt:'+id);
    if(known) return known;
    const cached=this.ctx.storage.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=?','workflow_runs',id).toArray()[0];
    if(!cached) return 1;
    const attempt=Number(JSON.parse(cached.data).run_attempt || 1);
    this.setMeta('workflow_attempt:'+id,attempt);
    if(attempt>1) {
      // Legacy mirrors did not scope membership by attempt. Revalidate once;
      // retain current webhook rows and all historical resource records.
      const key=queryKey({operation:'workflow_jobs',args:{id}});
      this.ctx.storage.sql.exec('UPDATE collections SET complete=0,etag=NULL WHERE key=?',key);
      this.ctx.storage.sql.exec("DELETE FROM members WHERE collection=? AND id IN (SELECT id FROM resources WHERE kind='workflow_jobs' AND COALESCE(json_extract(data,'$.run_attempt'),1)!=?)",key,attempt);
      this.ctx.storage.sql.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=NULL,etag=NULL,nested=NULL,lease_id=NULL,lease_until=0,dirty=1,generation=generation+1 WHERE key=?',key);
      this.nextRevision('collection',key);
    }
    return attempt;
  }
  workflowAttempt(id:string,attempt:number): boolean {
    const prior=this.runAttempt(id);
    if (attempt<prior) return false;
    if (attempt>prior) {
      const key=queryKey({operation:'workflow_jobs',args:{id}});
      this.ctx.storage.sql.exec('DELETE FROM members WHERE collection=?',key);
      this.ctx.storage.sql.exec('UPDATE collections SET complete=0,etag=NULL WHERE key=?',key);
      this.ctx.storage.sql.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=NULL,etag=NULL,nested=NULL,lease_id=NULL,lease_until=0,generation=generation+1 WHERE key=?',key);
      this.nextRevision('collection',key);
      this.setMeta('workflow_finished:'+id,false);
      this.setMeta('important_final_pending:'+id,false);
      this.setMeta('important_active:'+id,false);
    }
    this.setMeta('workflow_attempt:'+id,attempt);
    return true;
  }
  async configure(name:string): Promise<void> {
    const repo=repository(this.env,name); if (!repo) throw new Error('repository not allowed');
    this.setMeta('repository',repo);
    for (const operation of ['repository','refs','open_prs','open_issues','recent_prs','recent_issues','pr_delta','issue_delta']) await this.enqueue({repository:repo.name,operation},operation==='open_prs' || operation.endsWith('_delta')?0:10);
    await this.ctx.storage.setAlarm(Date.now()+100);
  }
  async ingest(pointer:EventPointer): Promise<void> {
    if (this.ctx.storage.sql.exec('SELECT id FROM deliveries WHERE id=?',pointer.delivery).toArray().length) return;
    const object=await this.env.ARCHIVE.get(pointer.key); if (!object) throw new Error('event body unavailable');
    const body:any=parseGitHub(await object.text()); const repo=repository(this.env,pointer.repository); if (!repo) throw new Error('repository not allowed');
    this.setMeta('repository',repo);
    const event=pointer.event, deleted=body.action==='deleted';
    // All updates are idempotent; receipt is committed only after their durable rows and jobs.
    if (event==='pull_request' && body.pull_request) {
      const row=pr(body.pull_request), number=String(row.number);
      const before=this.ctx.storage.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=?','pr',number).toArray()[0];
      const prior=before?await this.decode(before.data):null;
      const accepted=await this.put('pr',number,row,pointer.received,false,number,true);
      // A webhook describes one PR, not a complete inventory. Never create fake coverage.
      const key=queryKey({operation:'open_prs'});
      if (accepted) this.membership(key,number,body.pull_request.state==='open');
      const detail={repository:repo.name,operation:'pr',args:{number:row.number}};
      const detailKnown=this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(detail)).toArray().length>0;
      await this.enqueue(detail,0,Date.now(),!detailKnown || row.mergeable==='UNKNOWN');
      const inventoryKnown=this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',key).toArray().length>0;
      await this.enqueue({repository:repo.name,operation:'open_prs'},1,Date.now(),!inventoryKnown);
      if(accepted && row.headRefOid && prior?.headRefOid!==row.headRefOid) await this.enqueue({repository:repo.name,operation:'files',args:{number:row.number}},1,Date.now(),true);
      if (accepted && row.headRefOid) for (const operation of ['statuses','checks']) {
        const q={repository:repo.name,operation,args:{sha:row.headRefOid}};
        const known=this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0;
        await this.enqueue(q,1,Date.now(),!known || prior?.headRefOid!==row.headRefOid);
      }
    }
    if (event==='issues' && body.issue) {
      const row=issue(body.issue); const accepted=await this.put('issue',String(row.number),row,pointer.received,deleted,String(row.number),true);
      if (accepted) this.membership(queryKey({operation:'open_issues'}),String(row.number),!deleted && row.state==='OPEN');
      const q={repository:repo.name,operation:'open_issues'};
      const known=this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0;
      await this.enqueue(q,1,Date.now(),!known);
    }
    if (['issue_comment','pull_request_review_comment'].includes(event) && body.comment) {
      const operation=event==='issue_comment'?'issue_comments':'review_comments';
      const number=body.issue?.number || body.pull_request?.number;
      const row=comment(body.comment); const accepted=await this.put(operation,String(row.id),row,pointer.received,deleted,String(number),true);
      const key=queryKey({operation,args:{number}});
      if (accepted) this.membership(key,String(row.id),!deleted);
      const known=this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',key).toArray().length>0;
      await this.enqueue({repository:repo.name,operation,args:{number}},0,Date.now(),!known);
      if (operation==='review_comments') await this.enqueue({repository:repo.name,operation:'review_threads',args:{number}},2,Date.now(),true);
    }
    if (event==='pull_request_review' || event==='pull_request_review_thread') {
      const number=body.pull_request?.number;
      if (number) for (const operation of ['review_threads','reviews','review_comments']) await this.enqueue({repository:repo.name,operation,args:{number}},1,Date.now(),true);
    }
    if (event==='status' && body.sha && body.context) {
      await this.put('status_context',body.sha+':'+body.context,status(body),pointer.received,false,body.sha,true);
      const q={repository:repo.name,operation:'statuses',args:{sha:body.sha}};
      const known=this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0;
      await this.enqueue(q,0,Date.now(),!known);
    }
    if (event==='check_run' || event==='check_suite') {
      const sha=body.check_run?.head_sha || body.check_suite?.head_sha;
      if (sha) {
        const q={repository:repo.name,operation:'checks',args:{sha}},key=queryKey(q);
        if (body.check_run?.id) {
          const row={...body.check_run,updated_at:body.check_run.completed_at || body.check_run.started_at || new Date(pointer.received).toISOString()};
          if (await this.put('checks',String(row.id),row,pointer.received,false,sha,true)) this.membership(key,String(row.id),true);
        }
        const known=this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',key).toArray().length>0;
        await this.enqueue(q,0,Date.now(),!known);
      }
    }
    if (event==='workflow_run' || event==='workflow_job') {
      const id=body.workflow_run?.id || body.workflow_job?.run_id;
      const attempt=Number(body.workflow_run?.run_attempt || body.workflow_job?.run_attempt || this.meta('workflow_attempt:'+id) || 1);
      if (id && !this.workflowAttempt(String(id),attempt)) {
        this.ctx.storage.sql.exec('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',pointer.delivery,event,pointer.received);
        return;
      }
      if (body.workflow_run) {
        const row=body.workflow_run;
        if (await this.put('workflow_runs',String(row.id),row,pointer.received,false,'',true)) {
          this.membership(queryKey({operation:'workflow_runs'}),String(row.id),true);
          this.noteWorkflow(String(row.id),row);
        }
      }
      if (id) {
        const q={repository:repo.name,operation:'workflow_jobs',args:{id:String(id)}},key=queryKey(q);
        if (body.workflow_job) {
          const row={...body.workflow_job,updated_at:body.workflow_job.completed_at || body.workflow_job.started_at || new Date(pointer.received).toISOString()};
          if (await this.put('workflow_jobs',String(row.id),row,pointer.received,false,String(id),true)) {
            this.membership(key,String(row.id),true);
            this.setMeta('workflow_seen_at:'+id,Date.now());
            if (row.status!=='completed') this.setMeta('workflow_finished:'+id,false);
          }
        }
        const demanded=(this.ctx.storage.sql.exec<any>('SELECT at FROM demand WHERE key=?',key).toArray()[0]?.at || 0)>Date.now()-300000;
        // Observing a job does not establish exhaustive membership or create a baseline obligation.
        if (demanded || this.importantPending(String(id))) await this.enqueue(q,1,Date.now(),Boolean(body.workflow_run) || !this.complete(q));
      }
      const q={repository:repo.name,operation:'workflow_runs'};
      const known=this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0;
      await this.enqueue(q,1,Date.now(),!known);
    }
    if (['push','create','delete','repository','label','milestone','merge_group'].includes(event)) {
      if (event==='push' && String(body.ref).startsWith('refs/heads/')) {
        const row={ref:body.ref,object:{sha:body.after,type:'commit',url:'https://api.github.com/repos/'+repo.name+'/git/commits/'+body.after},updated_at:new Date(pointer.received).toISOString()};
        const removed=Boolean(body.deleted) || /^0+$/.test(body.after || '');
        if (await this.put('refs',body.ref,row,pointer.received,removed,'',true)) this.membership(queryKey({operation:'refs'}),body.ref,!removed);
      }
      // Header and CI webhooks already update the indexed records. A single
      // changed PR must not trigger a repository-wide survey at fleet scale.
      for (const operation of ['open_prs','open_issues','refs','repository']) {
        const q={repository:repo.name,operation};
        const known=this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0;
        await this.enqueue(q,2,Date.now(),!known || operation==='repository' && event==='repository' || operation==='refs' && ['create','delete'].includes(event));
      }
    }
    this.ctx.storage.sql.exec('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',pointer.delivery,event,pointer.received);
    this.setMeta('last_webhook',Math.max(pointer.received,this.meta('last_webhook') || 0));
    this.setMeta('webhook_apply_ms',Math.max(0,Date.now()-pointer.received));
  }
  kind(q:Query): string { return ['open_prs','recent_prs','pr','pr_delta'].includes(q.operation)?'pr':['open_issues','recent_issues','issue_delta'].includes(q.operation)?'issue':q.operation; }
  async rows(q:Query): Promise<any[] | any | null> {
    const key=queryKey(q), collection=this.ctx.storage.sql.exec<any>('SELECT * FROM collections WHERE key=? AND complete=1',key).toArray()[0];
    if (!collection && !(q.operation==='workflow_jobs' && q.allow_observed && this.meta('workflow_attempt:'+q.args?.id))) return null;
    if (q.operation==='pr') {
      const row=this.ctx.storage.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=? AND tombstone=0','pr',String(q.args?.number)).toArray()[0];
      if (row) return this.decode(row.data);
    }
    if (['pr','repository','statuses'].includes(q.operation)) {
      const row=this.ctx.storage.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=? AND tombstone=0',q.operation,key).toArray()[0];
      if (!row) return null;
      const data=await this.decode(row.data);
      if (q.operation!=='statuses' || !Array.isArray(data)) return data;
      const contexts=new Map(data.map(n=>{const value=status(n);return [value.context,value];}));
      for (const row of this.ctx.storage.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND parent=? AND tombstone=0','status_context',String(q.args?.sha)).toArray()) {
        const value=status(await this.decode(row.data)),prior=contexts.get(value.context);
        if (!prior || Date.parse(value.startedAt)>=Date.parse(prior.startedAt)) contexts.set(value.context,value);
      }
      return [...contexts.values()];
    }
    const members=this.ctx.storage.sql.exec<any>('SELECT r.data FROM members m JOIN resources r ON r.kind=? AND r.id=m.id WHERE m.collection=? AND r.tombstone=0 ORDER BY CAST(r.id AS INTEGER),r.id',this.kind(q),key).toArray();
    const rows=[];
    for (const row of members) {
      const data=await this.decode(row.data);
      if (q.operation==='workflow_jobs' && Number(data.run_attempt || 1)!==(this.meta('workflow_attempt:'+q.args?.id) || 1)) continue;
      if (q.operation==='open_prs') {
        if (data.state && data.state!=='OPEN') continue;
        const statuses=await this.rows({repository:q.repository,operation:'statuses',args:{sha:data.headRefOid}});
        data.statusCheckRollup=Array.isArray(statuses)?statuses:[];
      }
      rows.push(data);
    }
    return rows;
  }
  async query(q:Query): Promise<Result> {
    validateQuery(q);
    const repo=this.meta('repository'); if (!repo) await this.configure(q.repository);
    const key=queryKey(q);
    if(q.operation==='workflow_jobs') this.runAttempt(String(q.args?.id));
    if (!q.allow_observed) await this.noteDemand(q);
    if (q.cursor) {
      const row=this.ctx.storage.sql.exec<any>('SELECT payload FROM cursors WHERE (id=? OR id=?) AND expires>? ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END LIMIT 1',q.cursor,q.cursor.split(':')[0],Date.now(),q.cursor).toArray()[0];
      if (!row) return {version:1,repository:q.repository,operation:q.operation,status:'unavailable',revision:this.revision(),complete:false,reason:'cursor_expired'};
      const payload=await this.decode(row.payload);
      if (payload.key!==key) throw new Error('cursor query mismatch');
      if (Boolean(payload.allow_observed)!==Boolean(q.allow_observed)) throw new Error('cursor coverage mismatch');
      if (payload.result?.data!==undefined) return payload.result;
      // Existing ten-minute cursors survive a rolling deployment.
      const [id,offsetText]=q.cursor.split(':'); const offset=Number(offsetText);
      if (!Number.isInteger(offset) || offset<0) throw new Error('invalid cursor');
      return this.page(payload.result,payload.rows,id,offset);
    }
    let rows:any;
    if (q.operation==='progress') {
      const prs=await this.rows({repository:q.repository,operation:'open_prs'});
      const p=Array.isArray(prs)?prs.find(p=>p.number===Number(q.args?.number)):null;
      const cs=await this.rows({...q,operation:'issue_comments'}), rcs=await this.rows({...q,operation:'review_comments'});
      for (const operation of ['issue_comments','review_comments']) { const child={...q,operation};await this.noteDemand(child);if (!this.complete(child)) await this.enqueue(child,5); }
      rows=p && cs && rcs ? {head:p.headRefOid,ncomments:cs.length+rcs.length,revision:this.revision()}:null;
    } else {
      if (!q.allow_observed && !this.complete(q)) await this.enqueue(q,5);
      rows=await this.rows(q);
    }
    const collection=this.ctx.storage.sql.exec<any>('SELECT * FROM collections WHERE key=?',key).toArray()[0];
    const complete=rows!==null && (q.operation==='progress' || this.complete(q));
    const observed=this.ctx.storage.sql.exec<any>('SELECT MAX(r.observed) at FROM members m JOIN resources r ON r.kind=? AND r.id=m.id WHERE m.collection=?',this.kind(q),key).one().at;
    const result:Result={version:1,repository:q.repository,operation:q.operation,status:rows===null?'unavailable':'ok',revision:this.revision(),observed_at:observed || collection?.verified,verified_at:collection?.verified,complete,coverage:complete?'complete':q.allow_observed?'webhook_observed':undefined};
    const coverage=rows===null?'unavailable':result.coverage!;
    this.ctx.storage.sql.exec('INSERT INTO query_metrics VALUES(?,?,1,?) ON CONFLICT(operation,coverage) DO UPDATE SET requests=requests+1,last_at=excluded.last_at',q.operation,coverage,Date.now());
    if (rows===null) return {...result,reason:'coverage_unavailable'};
    if (Array.isArray(rows) && rows.length>100) {
      const id=crypto.randomUUID();
      const expires=Date.now()+600000;
      // Store separate pages so continuation never downloads or decodes the
      // whole inventory. Every page retains the initial snapshot revision.
      let offset=0, first:Result | undefined;
      while (offset<rows.length) {
        const page=this.page(result,rows,id,offset), cursor=id+':'+offset;
        this.ctx.storage.sql.exec('INSERT INTO cursors VALUES(?,?,?)',cursor,expires,await this.encode({key,allow_observed:q.allow_observed,result:page},expires+60000));
        first ||= page;
        offset+=Array.isArray(page.data)?page.data.length:0;
      }
      return first!;
    }
    return {...result,data:rows};
  }
  page(result:Result,rows:any[],id:string,offset:number): Result {
    // Keep the number of client round trips bounded as inventories grow.
    // A byte bound avoids making comment-heavy pages arbitrarily large.
    let end=offset, bytes=0;
    while (end<rows.length && end<offset+500) {
      const size=new TextEncoder().encode(JSON.stringify(rows[end])).length;
      if (end>offset && bytes+size>1490000) break;
      bytes+=size; end++;
    }
    return {...result,data:rows.slice(offset,end),...(end<rows.length?{cursor:id+':'+end}:{})};
  }
  interval(q:Query): number {
    if (q.operation==='pr_delta' || q.operation==='issue_delta') return 60000;
    if (q.operation==='open_prs' || q.operation==='open_issues') return 3600000;
    if (q.operation==='files') return 365*86400000; // head-change webhook dirties this immutable-by-head read
    if (q.operation==='reactions') {
      const active=this.ctx.storage.sql.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q)).toArray()[0];
      return active?.at>Date.now()-300000?300000:86400000;
    }
    if (q.operation==='reviews' || q.operation==='checks') return 6*3600000;
    const demand=this.ctx.storage.sql.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q)).toArray()[0];
    if (q.operation==='statuses') return demand?.at>Date.now()-300000?300000:3600000;
    if (q.operation==='workflow_jobs') return 60000;
    if (q.operation==='workflow_runs') return demand?.at>Date.now()-300000?60000:3600000;
    if (['issue_comments','review_comments','review_threads','reviews','reactions'].includes(q.operation)) return demand?.at>Date.now()-300000?300000:3600000;
    return 3600000;
  }
  priority(q:Query): number {
    if (q.operation.endsWith('_delta')) return 0;
    const demand=this.ctx.storage.sql.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q)).toArray()[0];
    if (demand?.at>Date.now()-300000) return 5;
    if (['open_prs','open_issues','repository','refs','workflow_runs'].includes(q.operation)) return 10;
    return 20;
  }
  async noteDemand(q:Query): Promise<void> {
    if (q.allow_observed) return;
    const key=queryKey(q),now=Date.now();
    const prior=this.ctx.storage.sql.exec<any>('SELECT at FROM demand WHERE key=?',key).toArray()[0];
    this.ctx.storage.sql.exec('INSERT INTO demand VALUES(?,?) ON CONFLICT(key) DO UPDATE SET at=excluded.at',key,now);
    const complete=this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',key).toArray().length>0;
    if (!complete && q.operation!=='progress') {
      this.ctx.storage.sql.exec("UPDATE jobs SET lane='requested',requested_at=COALESCE(requested_at,?),due=CASE WHEN wait_reason IS NULL AND attempt=0 THEN MIN(due,?) ELSE due END WHERE key=?",now,now,key);
    }
    if (prior?.at>now-300000) return;
    if (complete && q.operation!=='progress' && !(q.operation==='workflow_jobs' && this.meta('workflow_finished:'+q.args?.id)) && !this.ctx.storage.sql.exec('SELECT key FROM jobs WHERE key=?',key).toArray().length) await this.enqueue(q,20,now+this.interval(q));
    // Becoming active advances a quiet audit to its hot interval. A client
    // still receives existing data immediately; actual quota waits stay intact.
    this.ctx.storage.sql.exec("UPDATE jobs SET due=MIN(due,?),priority=5,lane=CASE WHEN ? THEN 'verification' ELSE 'requested' END WHERE key=? AND attempt=0 AND started IS NULL AND wait_reason IS NULL",complete?now+this.interval(q):now,Number(complete),key);
    const due=this.ctx.storage.sql.exec<any>('SELECT due FROM jobs WHERE key=?',key).toArray()[0]?.due;
    const alarm=await this.ctx.storage.getAlarm();
    if (due && (!alarm || alarm>due)) await this.ctx.storage.setAlarm(Math.max(now+100,due));
  }
  nextJob(): any {
    const now=Date.now(),turn=this.meta('repair_turn') || 0;
    this.setMeta('repair_turn',turn+1);
    const preferred=laneAt(turn,settings(this.env).weights);
    return this.ctx.storage.sql.exec<any>('SELECT * FROM jobs WHERE due<=? AND lease_until<=? ORDER BY CASE WHEN lane=? THEN 0 WHEN lane=\'requested\' THEN 1 WHEN lane=\'active\' THEN 2 WHEN lane=\'verification\' THEN 3 ELSE 4 END,due,key LIMIT 1',now,now,preferred).toArray()[0];
  }
  dormant(q:Query): boolean {
    if (q.operation==='workflow_jobs' && this.meta('workflow_finished:'+q.args?.id)) return true;
    const demand=this.ctx.storage.sql.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q)).toArray()[0];
    if (demand?.at>Date.now()-300000) return false;
    if (q.operation==='workflow_jobs') {
      const row=this.ctx.storage.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=?','workflow_runs',String(q.args?.id)).toArray()[0];
      return Boolean(this.meta('workflow_finished:'+q.args?.id) || row && JSON.parse(row.data).status==='completed');
    }
    let numbers:string[]=[];
    if (q.args?.number) numbers=[String(q.args.number)];
    else if (q.operation==='reactions') {
      const row=this.ctx.storage.sql.exec<any>('SELECT parent,tombstone FROM resources WHERE kind=? AND id=?','review_comments',String(q.args?.id)).toArray()[0];
      if (row?.tombstone) return true;
      if (row?.parent) numbers=[row.parent];
    } else if (['statuses','checks'].includes(q.operation)) numbers=this.ctx.storage.sql.exec<any>('SELECT number FROM head_prs WHERE sha=?',String(q.args?.sha)).toArray().map(r=>r.number);
    if (!numbers.length) return false;
    return numbers.every(number=>{
      const row=this.ctx.storage.sql.exec<any>("SELECT data FROM resources WHERE kind IN ('pr','issue') AND id=? ORDER BY kind DESC LIMIT 1",number).toArray()[0];
      const data=row?JSON.parse(row.data):null;
      return data?.state && data.state!=='OPEN';
    });
  }
  finishJob(job:any,q:Query,etag?:string): void {
    const current=this.ctx.storage.sql.exec<any>('SELECT generation,priority,requested_at,lane FROM jobs WHERE key=?',job.key).toArray()[0];
    const again=current?.generation>(job.start_generation || 0);
    // A newer webhook can arrive while pages are being fetched. Completing the
    // old collection must not erase that event's pending repair.
    this.metric('completion',q,current?.lane || job.lane || 'verification');
    const requested=job.requested_at || current?.requested_at;
    if (requested) {
      this.metric('requested_latency_ms',q,'requested',Date.now()-requested);
      if(job.page===1 && !job.nested) this.metric('requested_single_latency_ms',q,'requested',Date.now()-requested);
    }
    this.ctx.storage.sql.exec('UPDATE jobs SET due=?,priority=?,lane=?,dirty=?,started=NULL,cursor=NULL,page=1,api=NULL,etag=?,attempt=0,wait_reason=NULL,nested=NULL,requested_at=NULL WHERE key=?',again?Date.now():Date.now()+this.interval(q),again?current.priority:this.priority(q),this.lane(q,again),Number(again),etag || null,job.key);
    if(!again && q.operation==='workflow_jobs' && this.meta('workflow_finished:'+q.args?.id)) {
      this.setMeta('important_final_pending:'+q.args?.id,false);this.setMeta('important_active:'+q.args?.id,false);
    }
    if (!again && this.dormant(q)) this.ctx.storage.sql.exec('DELETE FROM jobs WHERE key=?',job.key);
  }
  async refresh(job:any,supplied?:any): Promise<void> {
    const repo=this.meta('repository'), q:Query=JSON.parse(job.query);
    const started=job.started || Date.now();
    if (!job.started) {
      job.start_generation=job.generation || 0;
      this.ctx.storage.sql.exec('UPDATE jobs SET started=?,start_generation=? WHERE key=?',started,job.start_generation,job.key);
    }
    const runAttempt=q.operation==='workflow_jobs'?this.runAttempt(String(q.args?.id)):undefined;
    const upstream=q.operation==='workflow_runs'?{...q,args:{after:new Date((this.meta('workflow_watermark') || Date.parse(this.meta('workflow_since')))-300000).toISOString()}}:q.operation==='workflow_jobs'?{...q,args:{...q.args,attempt:runAttempt}}:q;
    const response=supplied?null:await stub(this.env.INSTALLATIONS,repo.installation).fetch('https://internal/read',{method:'POST',body:JSON.stringify({repository:repo,query:upstream,cursor:job.cursor,page:job.page,api:job.api,nested:job.nested?await this.decode(job.nested):undefined,etag:job.page===1 && !job.nested && q.operation!=='workflow_runs'?job.etag:undefined})});
    const result:any=supplied || await response!.json();
    if (job.lease_id && this.ctx.storage.sql.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key).toArray()[0]?.lease_id!==job.lease_id) return;
    if (result.continuation) {
      const nested=await this.encode(result.continuation,Date.now()+86400000);
      if (job.lease_id && this.ctx.storage.sql.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key).toArray()[0]?.lease_id!==job.lease_id) return;
      this.ctx.storage.sql.exec('UPDATE jobs SET nested=?,api=?,due=?,wait_reason=NULL WHERE key=?',nested,result.api,Date.now(),job.key);
      this.metric('page',q,job.lane || 'verification');return;
    }
    if (result.restart) {
      this.ctx.storage.sql.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=?,etag=NULL,due=? WHERE key=?',result.api,Date.now(),job.key);
      return;
    }
    if (result.overflow) {
      this.ctx.storage.sql.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=?,due=? WHERE key=?','graphql',Date.now(),job.key);
      return;
    }
    if (response && !response.ok || result.deferred || result.unavailable) {
      const capacity=result.category==='capacity';
      if(q.operation==='review_comments' && result.deferred && String(result.category).startsWith('primary')) {
        // The existing thread reconciler supplies the same flat history. Preserve
        // the REST cooldown while promoting that dependency in the other pool.
        const source={...q,operation:'review_threads'};
        const existing=this.ctx.storage.sql.exec<any>('SELECT started,dirty FROM jobs WHERE key=?',queryKey(source)).toArray()[0];
        await this.noteDemand(source);await this.enqueue(source,1,Date.now(),!existing || !existing.started && !existing.dirty);
        if(!this.complete(q)) this.ctx.storage.sql.exec("UPDATE jobs SET lane='requested',requested_at=COALESCE(requested_at,?) WHERE key=?",job.requested_at || Date.now(),queryKey(source));
      }
      this.ctx.storage.sql.exec('UPDATE jobs SET due=?,attempt=attempt+?,wait_reason=? WHERE key=?',result.retry_at || Date.now()+Math.min(3600000,60000*2**Math.min(6,job.attempt)),capacity?0:1,result.category || 'upstream',job.key);
      this.metric(capacity?'capacity_wait':'retry',q,job.lane || 'verification');
      if(!capacity) this.setMeta('last_upstream_failure',{at:Date.now(),operation:q.operation,category:result.category || 'upstream'}); return;
    }
    if (result.unchanged) {
      this.ctx.storage.sql.exec('UPDATE collections SET verified=? WHERE key=?',Date.now(),job.key);
      this.finishJob(job,q,job.etag); return;
    }
    if (result.api!==job.api && job.page>1 && job.api) {
      // REST pages and GraphQL cursors cannot be mixed; restart this staged collection.
      this.ctx.storage.sql.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=NULL WHERE key=?',job.key); return;
    }
    const key=`staging/${repo.id}/${encodeURIComponent(job.key)}/${started}/${job.page}`;
    await this.env.ARCHIVE.put(key,JSON.stringify(result.data),{customMetadata:{expires:String(Date.now()+86400000)}});
    if (job.lease_id && this.ctx.storage.sql.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key).toArray()[0]?.lease_id!==job.lease_id) return;
    this.ctx.storage.sql.exec('UPDATE jobs SET nested=NULL,wait_reason=NULL WHERE key=?',job.key);
    this.metric('page',q,job.lane || 'verification');
    let next=result.cursor;
    if (['pr_delta','issue_delta'].includes(q.operation) && Array.isArray(result.data) && result.data.some((r:any)=>Date.parse(r.updatedAt)<(this.meta(q.operation+'_watermark') || started)-300000)) next=undefined;
    if (q.operation.startsWith('recent_') && Array.isArray(result.data) && result.data.some((r:any)=>Date.parse(r.updatedAt)<Date.now()-30*86400000)) next=undefined;
    if (next) {
      if (next===job.cursor) throw new Error('repeated upstream cursor');
      this.ctx.storage.sql.exec('UPDATE jobs SET cursor=?,page=page+1,api=?,due=?,etag=? WHERE key=?',next,result.api,Date.now(),result.etag || job.etag,job.key); return;
    }
    const rows:any[]=[]; let scalar:any;
    for (let page=1;page<=job.page;page++) {
      const object=await this.env.ARCHIVE.get(`staging/${repo.id}/${encodeURIComponent(job.key)}/${started}/${page}`);
      if (!object) throw new Error('staging page missing');
      const data:any=await object.json(); if (Array.isArray(data)) rows.push(...data); else scalar=data;
    }
    const kind=this.kind(q), memberIDs=new Set<string>(), acceptedIDs=new Set<string>();
    const changedNumbers=new Set<number>(), changedHeads=new Set<number>();
    if (scalar!==undefined || ['pr','repository','statuses'].includes(q.operation)) {
      await this.put(q.operation,job.key,scalar===undefined?rows:scalar,started);
      if (q.operation==='pr' && scalar?.number) await this.put('pr',String(scalar.number),scalar,started,false,String(scalar.number));
    } else {
      for (const row of rows) {
        if (q.operation.startsWith('recent_') && Date.parse(row.updatedAt)<Date.now()-30*86400000) continue;
        const id=q.operation==='files'?String(q.args?.number)+':'+String(row.filename):this.id(q.operation,row); memberIDs.add(id);
        if (kind==='pr') {
          const prior=this.ctx.storage.sql.exec<any>('SELECT data,updated,observed FROM resources WHERE kind=? AND id=?','pr',id).toArray()[0];
          const before=prior?await this.decode(prior.data):null;
          if ((!prior || prior.observed<=started) && (!before || before.updatedAt!==row.updatedAt)) changedNumbers.add(row.number);
          if ((!prior || prior.observed<=started) && (!before || before.headRefOid!==row.headRefOid)) changedHeads.add(row.number);
        }
        if (q.operation==='workflow_jobs') row.run_attempt ||= runAttempt;
        if (q.operation==='workflow_runs' && !this.workflowAttempt(id,Number(row.run_attempt || 1))) continue;
        if (await this.put(kind,id,row,started,false,String(q.args?.number || q.args?.id || q.args?.sha || ''))) acceptedIDs.add(id);
        if (q.operation==='workflow_runs' && acceptedIDs.has(id)) {
          this.noteWorkflow(id,row);
          const key=queryKey({operation:'workflow_jobs',args:{id}});
          if (this.importantPending(id)) await this.enqueue({repository:repo.name,operation:'workflow_jobs',args:{id}},1);
        }
      }
      // Keep newer webhook membership when the reconciliation began before that event.
      const prior=this.ctx.storage.sql.exec<any>('SELECT m.id,r.observed FROM members m LEFT JOIN resources r ON r.kind=? AND r.id=m.id WHERE m.collection=?',kind,job.key).toArray();
      for (const row of prior) if (q.operation!=='workflow_runs' && !memberIDs.has(row.id) && row.observed<=started && this.ctx.storage.sql.exec<any>('SELECT observed FROM resources WHERE kind=? AND id=?',kind,row.id).toArray()[0]?.observed<=started) this.membership(job.key,row.id,false);
      for (const id of memberIDs) this.membership(job.key,id,true);
    }
    if (job.lease_id && this.ctx.storage.sql.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key).toArray()[0]?.lease_id!==job.lease_id) return;
    if (!this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',job.key).toArray().length) this.nextRevision('collection',job.key);
    this.ctx.storage.sql.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET complete=1,verified=excluded.verified,revision=excluded.revision,etag=excluded.etag',job.key,q.operation,JSON.stringify(q.args || {}),1,Date.now(),this.revision(),job.page===1?result.etag || null:null);
    this.finishJob(job,q,job.page===1?result.etag || null:null);
    if (q.operation==='workflow_runs') this.setMeta('workflow_watermark',started);
    if (['pr_delta','issue_delta'].includes(q.operation)) {
      const operation=q.operation==='pr_delta'?'open_prs':'open_issues', inventory=queryKey({operation});
      for (const row of rows) {
        const id=String(row.number);
        if (acceptedIDs.has(id)) this.membership(inventory,id,row.state==='OPEN');
        if (changedNumbers.has(row.number)) {
          await this.enqueue({repository:repo.name,operation:'issue_comments',args:{number:row.number}},2,Date.now(),true);
          if (operation==='open_prs') for (const op of ['review_threads','pr']) await this.enqueue({repository:repo.name,operation:op,args:{number:row.number}},3,Date.now(),true);
          if (changedHeads.has(row.number)) await this.enqueue({repository:repo.name,operation:'files',args:{number:row.number}},3,Date.now(),true);
        }
      }
      this.setMeta(q.operation+'_watermark',started);
    }
    if (q.operation==='open_prs' || q.operation==='recent_prs') {
      for (const row of rows) {
        if (q.operation==='recent_prs' && Date.parse(row.updatedAt)<Date.now()-30*86400000) continue;
        for (const operation of ['issue_comments','review_threads','reviews','files']) await this.enqueue({repository:repo.name,operation,args:{number:row.number}},20);
        if (q.operation==='open_prs') {
          const itemKey=queryKey({operation:'pr',args:{number:row.number}});
          await this.put('pr',itemKey,row,started);
          this.ctx.storage.sql.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET complete=1,verified=excluded.verified,revision=excluded.revision',itemKey,'pr',JSON.stringify({number:row.number}),1,Date.now(),this.revision(),null);
        }
        if (q.operation==='open_prs' && row.headRefOid) for (const operation of ['statuses','checks']) await this.enqueue({repository:repo.name,operation,args:{sha:row.headRefOid}},2);
      }
      if(q.operation==='open_prs') this.setMeta('bootstrap_inventory',Date.now());
    }
    if (q.operation==='open_issues') for (const row of rows) await this.enqueue({repository:repo.name,operation:'issue_comments',args:{number:row.number}},20);
    if (q.operation==='review_comments') for (const row of rows) await this.enqueue({repository:repo.name,operation:'reactions',args:{id:String(row.id)}},25);
    if (q.operation==='review_threads') {
      // This complete thread reconciliation also supplies the flat REST-shaped history.
      const comments=rows.flatMap(t=>t.comments.nodes.map(comment));
      const flatKey=queryKey({operation:'review_comments',args:q.args}), ids=new Set<string>();
      for (const c of comments) {
        ids.add(String(c.id)); await this.put('review_comments',String(c.id),c,started,false,String(q.args?.number));
        await this.enqueue({repository:repo.name,operation:'reactions',args:{id:String(c.id)}},25);
      }
      const prior=this.ctx.storage.sql.exec<any>('SELECT m.id,r.observed FROM members m LEFT JOIN resources r ON r.kind=? AND r.id=m.id WHERE m.collection=?','review_comments',flatKey).toArray();
      for (const row of prior) if (!ids.has(row.id) && row.observed<=started) this.membership(flatKey,row.id,false);
      for (const id of ids) this.membership(flatKey,id,true);
      if (!this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',flatKey).toArray().length) this.nextRevision('collection',flatKey);
      this.ctx.storage.sql.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET complete=1,verified=excluded.verified,revision=excluded.revision',flatKey,'review_comments',JSON.stringify(q.args),1,Date.now(),this.revision(),null);
      const dependent=this.ctx.storage.sql.exec<any>('SELECT * FROM jobs WHERE key=? AND wait_reason LIKE ?',flatKey,'primary%').toArray()[0];
      if(dependent) {
        this.finishJob({...dependent,start_generation:dependent.generation},{...q,operation:'review_comments'});
        this.ctx.storage.sql.exec('UPDATE jobs SET lease_id=NULL,lease_until=0 WHERE key=?',flatKey);
      }
    }
    this.setMeta('last_repair',Date.now());
  }
  async maintenance(): Promise<void> {
    const now=Date.now();
    // Retire queued historical CI even if an old retry placed it far in the future.
    const obsolete="key LIKE 'workflow_jobs:%' AND lease_until<=? AND NOT EXISTS(SELECT 1 FROM demand d WHERE d.key=jobs.key AND d.at>?) AND NOT EXISTS(SELECT 1 FROM resources r WHERE r.kind='workflow_runs' AND r.id=CAST(json_extract(jobs.query,'$.args.id') AS TEXT) AND json_extract(r.data,'$.head_branch') IN ('staging','trying') AND (json_extract(r.data,'$.status')!='completed' OR EXISTS(SELECT 1 FROM meta m WHERE m.key='important_final_pending:' || r.id AND m.value='true')))";
    const retired=this.ctx.storage.sql.exec<any>('SELECT lane,COUNT(*) n FROM jobs WHERE '+obsolete+' GROUP BY lane',now,now-300000).toArray();
    this.ctx.storage.sql.exec('DELETE FROM jobs WHERE '+obsolete,now,now-300000);
    for(const row of retired) this.metric('cancellation',{repository:this.meta('repository').name,operation:'workflow_jobs'},row.lane,row.n);
    this.ctx.storage.sql.exec('DELETE FROM deliveries WHERE received<?',now-14*86400000);
    this.ctx.storage.sql.exec('DELETE FROM changes WHERE at<?',now-7*86400000);
    this.ctx.storage.sql.exec('DELETE FROM cursors WHERE expires<?',now);
    this.ctx.storage.sql.exec('DELETE FROM repair_metrics WHERE at<?',now-7*86400000);
    if (now-(this.meta('last_snapshot') || 0)>86400000) {
      const key=`snapshots/${this.meta('repository').id}/${new Date(now).toISOString().slice(0,10)}.json`;
      const snapshot={version:1,at:now,meta:this.ctx.storage.sql.exec('SELECT * FROM meta').toArray(),resources:this.ctx.storage.sql.exec('SELECT * FROM resources').toArray(),collections:this.ctx.storage.sql.exec('SELECT * FROM collections').toArray(),members:this.ctx.storage.sql.exec('SELECT * FROM members').toArray()};
      await this.env.ARCHIVE.put(key,JSON.stringify(snapshot),{customMetadata:{expires:String(now+7*86400000)}}); this.setMeta('last_snapshot',now);
    }
  }
  retireUnneeded(job:any): boolean {
    const q:Query=JSON.parse(job.query),demand=(this.ctx.storage.sql.exec<any>('SELECT at FROM demand WHERE key=?',job.key).toArray()[0]?.at || 0)>Date.now()-300000;
    const unneeded=q.operation==='workflow_jobs' && !demand && !this.importantPending(String(q.args?.id));
    if (!unneeded && (job.dirty || !this.dormant(q) || !this.complete(q))) return false;
    this.ctx.storage.sql.exec('DELETE FROM jobs WHERE key=?',job.key);
    this.metric('cancellation',q,job.lane);return true;
  }
  claim(job:any): any {
    const now=Date.now(),lease=crypto.randomUUID(),started=job.started || now,generation=job.started?job.start_generation:job.generation;
    this.ctx.storage.sql.exec('UPDATE jobs SET lease_id=?,lease_until=?,started=?,start_generation=? WHERE key=? AND lease_until<=?',lease,now+settings(this.env).lease_ms,started,generation,job.key,now);
    return this.ctx.storage.sql.exec<any>('SELECT * FROM jobs WHERE key=? AND lease_id=?',job.key,lease).toArray()[0];
  }
  async dispatch(first:any): Promise<void> {
    const q:Query=JSON.parse(first.query),operation=q.operation,repo=this.meta('repository');
    let jobs=[first];
    try {
      if (['issue_comments','review_threads','statuses','reactions'].includes(operation) && first.page===1 && !first.api && !first.nested) {
        const candidates=this.ctx.storage.sql.exec<any>('SELECT * FROM jobs WHERE due<=? AND lease_until<=? AND lane=? AND page=1 AND api IS NULL AND nested IS NULL AND key LIKE ? ORDER BY due,key LIMIT ?',Date.now(),Date.now(),first.lane,operation+':%',Math.max(0,batchLimit(operation)-1)).toArray();
        for (const item of candidates) {
          if (this.retireUnneeded(item)) continue;
          const claimed=this.claim(item);if (claimed) jobs.push(claimed);
        }
        if (operation==='reactions') {
          const supported=[];
          for (const item of jobs) {
            const query=JSON.parse(item.query),row=this.ctx.storage.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=? AND tombstone=0','review_comments',String(query.args.id)).toArray()[0];
            const data=row?await this.decode(row.data):null;
            if (data?.node_id) supported.push({...item,node_id:data.node_id});
          }
          if (supported.length!==jobs.length) { await this.refresh(first);return; }
          jobs=supported;
        }
        const response=await stub(this.env.INSTALLATIONS,repo.installation).fetch('https://internal/batch',{method:'POST',body:JSON.stringify({repository:repo,jobs:jobs.map(j=>({key:j.key,query:JSON.parse(j.query),node_id:j.node_id}))})});
        const result:any=await response.json();
        if (result.results) {
          for(let i=0;i<jobs.length;i++) await this.refresh(jobs[i],result.results[i] || {unavailable:true});
        } else if (result.deferred && operation!=='review_threads' && result.category!=='capacity' && !String(result.category).startsWith('secondary')) {
          // Retry REST-capable operations in their other pool on the next quantum.
          for(const item of jobs) this.ctx.storage.sql.exec('UPDATE jobs SET api=?,due=? WHERE key=? AND lease_id=?','core',Date.now(),item.key,item.lease_id);
        } else for(const item of jobs) await this.refresh(item,result.deferred?result:{unavailable:true,retry_at:Date.now()+60000});
      } else await this.refresh(first);
    } catch {
      for(const item of jobs) {
        this.ctx.storage.sql.exec('UPDATE jobs SET due=?,attempt=attempt+1,wait_reason=? WHERE key=? AND lease_id=?',Date.now()+60000,'transport_or_parser',item.key,item.lease_id);
        this.metric('retry',JSON.parse(item.query),item.lane);
      }
      this.setMeta('last_upstream_failure',{at:Date.now(),operation,category:'transport_or_parser'});
    } finally {
      for(const item of jobs) this.ctx.storage.sql.exec('UPDATE jobs SET lease_id=NULL,lease_until=0 WHERE key=? AND lease_id=?',item.key,item.lease_id);
    }
  }
  async alarm(): Promise<void> {
    const options=settings(this.env),began=Date.now();let dispatched=0,retired=0;
    try {
      while(dispatched<options.dispatches && Date.now()-began<options.launch_ms) {
        const wave:Promise<void>[]=[];
        while(wave.length<options.concurrency && dispatched<options.dispatches) {
          const next=this.nextJob();if (!next) break;
          if (this.retireUnneeded(next)) {if (++retired>=200) break;continue;}
          const job=this.claim(next);if (!job) continue;
          dispatched++;wave.push(this.dispatch(job));
        }
        if (!wave.length) break;
        await Promise.allSettled(wave);
      }
      await this.maintenance();
    } finally {
      const row=this.ctx.storage.sql.exec<any>('SELECT MIN(MAX(due,lease_until)) AS due FROM jobs').one();
      if (row.due) await this.ctx.storage.setAlarm(Math.max(Date.now()+100,row.due));
    }
  }
  async fetch(req:Request): Promise<Response> {
    try {
      const path=new URL(req.url).pathname;
      if (path==='/configure') { await this.configure((await req.json() as any).repository); return json({ok:true}); }
      if (path==='/event') { await this.ingest(await req.json() as EventPointer); return json({ok:true}); }
      if (path==='/query') {
        const q=await req.json() as Query;
        validateQuery(q);
        if(q.operation==='workflow_jobs') this.runAttempt(String(q.args?.id));
        const etag='"'+this.revision()+'"';
        if (!q.cursor && req.headers.get('if-none-match')===etag && q.operation!=='progress' && this.ctx.storage.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length) {
          await this.noteDemand(q);
          return new Response(null,{status:304,headers:{ETag:etag}});
        }
        const key=queryKey(q)+'|observed:'+Boolean(q.allow_observed)+'|'+(q.cursor || '');
        const cached=this.completedQueries.get(key);
        if (!q.cursor && cached?.result.revision===this.revision() && cached.expires>Date.now()) {
          await this.noteDemand(q);
          return json(cached.result,200,{ETag:'"'+cached.result.revision+'"'});
        }
        let pending=this.inflightQueries.get(key);
        if (!pending) {
          pending=this.query(q);
          this.inflightQueries.set(key,pending);
        }
        let result:Result;
        try { result=await pending; }
        finally { if (this.inflightQueries.get(key)===pending) this.inflightQueries.delete(key); }
        if (!q.cursor && result.status==='ok') {
          this.completedQueries.delete(key);
          this.completedQueries.set(key,{result,expires:Date.now()+540000});
          while (this.completedQueries.size>16) this.completedQueries.delete(this.completedQueries.keys().next().value!);
        }
        return json(result,200,{ETag:'"'+result.revision+'"'});
      }
      if (path==='/health') return json({reconciliation_version:2,deployment_id:this.env.CF_VERSION?.id,settings:settings(this.env),repository:this.meta('repository'),revision:this.revision(),bootstrap_inventory:this.meta('bootstrap_inventory'),last_webhook:this.meta('last_webhook'),webhook_apply_ms:this.meta('webhook_apply_ms'),last_repair:this.meta('last_repair'),last_upstream_failure:this.meta('last_upstream_failure'),coverage:this.ctx.storage.sql.exec('SELECT operation,COUNT(*) resources,SUM(complete) complete,MIN(verified) oldest_verified FROM collections GROUP BY operation').toArray(),jobs:this.ctx.storage.sql.exec('SELECT COUNT(*) total,SUM(due<=?) due,MIN(started) oldest_started FROM jobs',Date.now()).one(),lanes:this.ctx.storage.sql.exec('SELECT lane,COUNT(*) jobs,SUM(due<=? AND lease_until<=?) eligible,SUM(lease_until>?) inflight,MIN(CASE WHEN due<=? AND lease_until<=? THEN due END) oldest_eligible,MIN(requested_at) oldest_requested FROM jobs GROUP BY lane',...Array(5).fill(Date.now())).toArray(),waits:this.ctx.storage.sql.exec('SELECT lane,wait_reason,COUNT(*) jobs FROM jobs WHERE wait_reason IS NOT NULL GROUP BY lane,wait_reason').toArray(),repair_hour:this.ctx.storage.sql.exec('SELECT lane,operation,event,COUNT(*) n,SUM(value) value,MAX(value) maximum FROM repair_metrics WHERE at>? GROUP BY lane,operation,event',Date.now()-3600000).toArray(),requested_latency:this.ctx.storage.sql.exec("SELECT operation,MIN(value) p95_ms,MAX(samples) samples FROM (SELECT operation,value,ROW_NUMBER() OVER(PARTITION BY operation ORDER BY value) rank,COUNT(*) OVER(PARTITION BY operation) samples FROM repair_metrics WHERE event='requested_single_latency_ms' AND at>?) WHERE rank>=MAX(1,CAST((samples*95+99)/100 AS INTEGER)) GROUP BY operation",Date.now()-3600000).toArray(),queries:this.ctx.storage.sql.exec('SELECT * FROM query_metrics').toArray(),repair_operations:this.ctx.storage.sql.exec("SELECT json_extract(query,'$.operation') operation,COUNT(*) jobs,SUM(due<=?) due,MIN(due) next_due,MAX(attempt) attempts FROM jobs GROUP BY operation",Date.now()).toArray()});
      if (path==='/changes') { const after=Number(new URL(req.url).searchParams.get('after') || 0); const first=this.ctx.storage.sql.exec<any>('SELECT MIN(revision) AS revision FROM changes').one(); return json({revision:this.revision(),reset:after>0 && first.revision && after<first.revision-1,changes:this.ctx.storage.sql.exec('SELECT * FROM changes WHERE revision>? ORDER BY revision LIMIT 1000',after).toArray()}); }
      if (path==='/tick') { if (!this.meta('repository')) await this.configure((await req.json() as any).repository); await this.ctx.storage.setAlarm(Date.now()+100); return json({ok:true}); }
      if (path==='/repair') { this.ctx.storage.sql.exec('UPDATE jobs SET due=CASE WHEN wait_reason IS NULL AND attempt=0 THEN ? ELSE due END,dirty=1,generation=generation+1',Date.now()); await this.ctx.storage.setAlarm(Date.now()+100); return json({ok:true}); }
      return json({error:'not_found'},404);
    } catch(error) { return json({error:error instanceof Error?error.message:'operation failed'},500); }
  }
}
