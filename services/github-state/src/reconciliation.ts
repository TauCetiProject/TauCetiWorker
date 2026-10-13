import { Env } from './protocol';

export const LANES = ['requested', 'active', 'verification', 'archive'] as const;
export type Lane = typeof LANES[number];
export interface Settings { concurrency:number; dispatches:number; launch_ms:number; lease_ms:number; weights:number[] }
export function settings(env:Env): Settings {
  const raw=JSON.parse(env.RECONCILIATION_SETTINGS || '{}');
  const integer=(key:string,fallback:number,max:number)=>Number.isInteger(raw[key]) && raw[key]>0 && raw[key]<=max?raw[key]:fallback;
  return {concurrency:integer('concurrency',4,4),dispatches:integer('dispatches',16,100),launch_ms:integer('launch_ms',10000,30000),lease_ms:60000,
    weights:Array.isArray(raw.weights) && raw.weights.length===4 && raw.weights.every((n:unknown)=>Number.isInteger(n) && Number(n)>0 && Number(n)<=32)?raw.weights:[8,4,2,1]};
}

// Interleave shares rather than serving a long uninterrupted archival or urgent burst.
export function laneAt(turn:number,weights:number[]): Lane {
  const cycle:Lane[]=[];
  for(let i=0;i<Math.max(...weights);i++) for(let j=0;j<LANES.length;j++) if(weights[j]>i) cycle.push(LANES[j]);
  return cycle[turn%cycle.length];
}
