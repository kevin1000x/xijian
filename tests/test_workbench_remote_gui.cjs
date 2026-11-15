"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('workbench/app.js','utf8');
const nodes=new Map(),calls=[];
const $=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:''});return nodes.get(id);};
const context={$,api:async(url,body)=>{calls.push({url,body});return {probe:{source:'codex <img>',model:'synthetic',node_id:'node'},task_id:'task',status:'complete'};},refreshAgent:async()=>{}};
const entry={record:{status:'GENERATION_UNKNOWN_BLOCKED',proposal_id:'local-id',
  generation_receipt:{source:'remote_node',task_id:'remote-id'}},snapshot:{historical:true,revision:2}};
const renders=[];
Object.assign(context,{state:{agentTask:entry},renderAgent:(record,snapshot)=>renders.push({record,snapshot}),
  agentStatus:()=>{},refreshAgentTasks:async()=>{}});
context.lifecycleActive=record=>record?.lifecycle?.state==='RUNNING';
vm.createContext(context);
vm.runInContext(source.match(/function canRecoverRemoteCandidate[\s\S]*?\n}/)[0],context);
for(const name of ['configureRemoteNode','queryRemoteTask','recoverRemoteCandidate']){
  vm.runInContext(source.match(new RegExp('async function '+name+'\\([^\\n]*\\)[\\s\\S]*?\\n}'))[0],context);
}
(async()=>{
  $('remote-node-url').value='http://127.0.0.1:8766';$('remote-node-token').value='synthetic';
  await context.configureRemoteNode();
  assert.equal(calls[0].body.mode,'remote_node');assert.equal(calls[0].body.credential,'synthetic');
  assert.equal($('remote-node-token').value,'');assert.match($('remote-node-status').textContent,/codex <img>/);
  $('remote-task-id').value='task';await context.queryRemoteTask();
  assert.equal(calls[1].url,'/api/external/remote-status');assert.equal(calls[1].body.task_id,'task');
  await context.recoverRemoteCandidate();
  assert.equal(calls[2].url,'/api/agent/remote-recover');assert.equal(calls[2].body.proposal_id,'local-id');
  assert.equal(renders[0].snapshot.historical,false);assert.equal(renders[0].snapshot.revision,2);
  const partial={...entry.record,status:'CANDIDATE_READY',remote_recovery:{status:'complete'},
    lifecycle:{state:'UNKNOWN_BLOCKED',operation:{action:'generate',status:'unknown'}}};
  assert.equal(context.canRecoverRemoteCandidate(partial),true);
  assert.equal(context.canRecoverRemoteCandidate({...partial,lifecycle:{...partial.lifecycle,cancelled:true}}),false);
  assert.equal(context.canRecoverRemoteCandidate({...partial,lifecycle:{...partial.lifecycle,state:'IDLE'}}),false);
  assert.equal(context.canRecoverRemoteCandidate({...partial,lifecycle:{...partial.lifecycle,operation:{action:'public_test',status:'unknown'}}}),false);
  context.state.agentTask={...entry,record:partial};
  await context.recoverRemoteCandidate();
  assert.equal(calls.length,4);
  assert.equal(calls[3].url,calls[2].url);
  console.log('Remote GUI connection, status query and explicit original candidate recovery passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
