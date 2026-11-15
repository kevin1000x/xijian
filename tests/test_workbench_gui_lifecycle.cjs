/* Run with node; no DOM package or provider required. Exercises production GUI flow. */
"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../workbench/app.js"),"utf8");
function production(name){
  const result=source.match(new RegExp("(?:async )?function "+name+"\\([^\\n]*\\)[\\s\\S]*?\\n}"));
  assert.ok(result,"production function exists: "+name);return result[0];
}
function setup(){
  const nodes=new Map(),scheduled=[],calls=[];
  const node=id=>{if(!nodes.has(id))nodes.set(id,{textContent:"",hidden:false,disabled:false,value:"",children:[],classList:{toggle(){}},replaceChildren(){this.children=[];},append(n){this.children.push(n);},querySelectorAll(){return [];}});return nodes.get(id);};
  const c={state:{revision:1,busy:false,stopped:false,pollFailures:0,agentTask:null},$:node,el:(tag,text)=>({tag,textContent:text}),document:{hidden:false},
    window:{setTimeout(fn,delay){scheduled.push({fn,delay});return scheduled.length;},clearTimeout(){}},AbortController,Date,Number,Array,JSON,Promise,Math,
    api:async(url,body)=>{calls.push({url,body});return {};},agentStatus(){},updateBudget(){},renderAgentTasks(){},renderAgent(record,snapshot){c.state.agentTask={record,snapshot};},renderResult(){},renderReadiness(){},renderLifecycle(){}};
  vm.createContext(c);return {c,nodes,node,calls,scheduled,install(...names){names.forEach(n=>vm.runInContext(production(n),c));}};
}
(async()=>{
  const x=setup();x.install("advanceAgent");x.c.taskBlocked=r=>r.lifecycle?.cancelled||r.status.includes("UNKNOWN");
  const snapshot={revision:1,dataset_id:"input-a"};
  x.c.state.agentTask={record:{proposal_id:"task-a",proposal_sha256:"exact-visible-digest",status:"AWAITING_PUBLICATION_APPROVAL"},snapshot};
  x.c.api=async(url,body)=>{x.calls.push({url,body});return {proposal_id:"task-a",lifecycle:{mode:"hosted_api"}};};
  await x.c.advanceAgent("publish");
  assert.equal(x.calls.length,1);assert.equal(x.calls[0].url,"/api/agent/advance");
  assert.equal(x.calls[0].body.proposal_sha256,"exact-visible-digest");
  assert.deepEqual(Object.keys(x.calls[0].body).sort(),["proposal_id","proposal_sha256"]);
  for(const entry of [
    {record:{status:"AWAITING_PUBLICATION_APPROVAL"},snapshot,stale:true},
    {record:{status:"GENERATION_UNKNOWN_BLOCKED"},snapshot},
    {record:{status:"PUBLISHED",lifecycle:{cancelled:true}},snapshot},
    {record:{status:"PUBLISHED"},snapshot:{revision:0}}
  ]){
    x.c.state.agentTask=entry;await assert.rejects(()=>x.c.advanceAgent("publish"));
  }
  assert.equal(x.calls.length,1,"stale, unknown and cancelled tasks cannot dispatch");

  const y=setup();y.install("pollAgent");const old={record:{proposal_id:"old-task"},snapshot:{revision:1}};
  y.c.state.agentTask=old;let release;const pending=new Promise(resolve=>release=resolve);
  y.c.api=async(url,body)=>{y.calls.push({url,body});if(url.endsWith("/old-task"))return pending;return {};};
  let renders=0;y.c.renderAgent=()=>renders++;const polling=y.c.pollAgent();
  y.c.state.revision=2;y.c.state.agentTask={record:{proposal_id:"new-task"},snapshot:{revision:2}};
  release({proposal_id:"old-task",result:{run_id:"old-run"}});await polling;
  assert.equal(renders,0,"late polling response must not replace a newly selected task or edit");
  assert.equal(y.c.state.agentTask.record.proposal_id,"new-task");
  assert.ok(y.calls.every(call=>call.body===undefined),"polling must only read");
  assert.ok(y.scheduled.every(item=>item.delay<=15000),"reads/timeouts stay bounded");

  const z=setup();z.install("renderReadiness");z.c.updateAgentControls=()=>{};
  z.c.state.configurationLoaded=true;z.node("internal-url").value="typed-unsaved-endpoint";
  z.c.renderReadiness({internal:{source:"enterprise_mcp",ready:true},external:{ready:true},host:{configured:true,connected:false,claimed_tasks:[],registrations:[]},configuration:{mode:"http",url:"persisted-old-endpoint"}});
  assert.match(z.node("agent-connections").children[0].textContent,/等待宿主连接/);
  assert.doesNotMatch(z.node("agent-connections").children[0].textContent,/宿主已连接/);
  assert.equal(z.node("internal-url").value,"typed-unsaved-endpoint","poll must preserve unsaved connection edits");
  z.c.renderReadiness({internal:{source:"enterprise_mcp",ready:true},host:{connected:true,claimed_tasks:["task-a"],registrations:[]},external:{}});
  assert.match(z.node("agent-connections").children[0].textContent,/已领取 1 项任务/);

  const w=setup();
  vm.runInContext(source.match(/function taskBlocked[^\n]+/)[0]+"\n"+source.match(/function lifecycleActive[^\n]+/)[0],w.c);
  w.install("canRecoverRemoteCandidate","updateAgentControls");
  w.c.state.agentTask={record:{status:"GENERATION_UNKNOWN_BLOCKED",lifecycle:{state:"RUNNING"}}};
  w.c.updateAgentControls();
  assert.equal(w.node("agent-cancel-task").disabled,false,"active dispatch remains cancellable despite provisional unknown record");
  w.c.state.agentTask.record.lifecycle={state:"UNKNOWN_BLOCKED"};
  w.c.updateAgentControls();
  assert.equal(w.node("agent-cancel-task").disabled,true,"finished unknown state has no replay/cancel dispatch");

  console.log("GUI lifecycle: exact one-command approval, stale/cancel gates, read-only race-safe polling and host liveness passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
