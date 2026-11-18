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
  const input=setup();input.install("internalInputScope","internalApproval","updateInternalCells");
  let schema=[],values=[];const recipient={source:"enterprise_mcp",profile_sha256:"fixture"};
  input.c.invalidate=()=>{};input.c.state.agentReadiness={input_recipient:recipient};
  input.c.state.dataset={preview:[{amount:123,region:"region-a"},{amount:456,region:"region-b"}]};
  input.node("internal-fields").querySelectorAll=q=>(q.includes("schema")?schema:values).map(value=>({value}));
  input.node("internal-result-rows").checked=false;
  assert.throws(()=>input.c.internalApproval(),/选择/);
  schema=["region","amount"];let approval=input.c.internalApproval();
  assert.deepEqual(JSON.parse(JSON.stringify(approval.input_scope)),{columns:schema,value_columns:[],rows:[],allow_result_rows:false});
  assert.equal(approval.input_recipient,recipient);
  values=["region"];input.node("internal-rows").value="2";input.c.updateInternalCells();
  assert.match(input.node("internal-cells").textContent,/region-b/);
  assert.doesNotMatch(input.node("internal-cells").textContent,/region-a|amount|456/);
  input.node("internal-rows").value="3";assert.throws(()=>input.c.internalApproval(),/行号/);
  const x=setup();x.install("advanceAgent");x.c.taskBlocked=r=>r.lifecycle?.cancelled||r.status.includes("UNKNOWN");
  const snapshot={revision:1,dataset_id:"input-a"};
  x.c.state.agentTask={record:{proposal_id:"task-a",proposal_sha256:"exact-visible-digest",status:"AWAITING_PUBLICATION_APPROVAL"},snapshot};
  await assert.rejects(()=>x.c.advanceAgent("publish"));
  assert.equal(x.calls.length,0);
  x.c.state.agentTask.reviewedDigest="exact-visible-digest";
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

  const fields=setup();
  fields.install("loadFieldDisclosure");
  fields.c.el=(tag,text)=>({tag,textContent:text,children:[],append(item){this.children.push(item);}});
  fields.c.taskBlocked=()=>false;fields.c.lifecycleActive=()=>false;
  const current={record:{proposal_id:"scope",proposal_sha256:"pin"},snapshot:{revision:1}};
  fields.c.state.agentTask=current;
  fields.c.api=async(url,body)=>{
    assert.equal(url,"/api/agent/disclosure");assert.equal(body.proposal_sha256,"pin");
    return {proposal_sha256:"pin",fields:[{local_name:"amount",symbol:"COL_0001",dtype:"int64",meaning:"measure",restores:[]}],source:"external_http",destination:"http://localhost:8000"};
  };
  await fields.c.loadFieldDisclosure(current);
  assert.equal(current.reviewedDigest,"pin");assert.equal(fields.node("agent-publish").hidden,false);
  const stale={...current,reviewedDigest:null};await fields.c.loadFieldDisclosure(stale);
  assert.equal(stale.reviewedDigest,null);

  const w=setup();
  vm.runInContext(source.match(/function taskBlocked[^\n]+/)[0]+"\n"+source.match(/function lifecycleActive[^\n]+/)[0],w.c);
  w.install("canRecoverRemoteCandidate","updateAgentControls");
  w.c.state.agentTask={record:{status:"GENERATION_UNKNOWN_BLOCKED",lifecycle:{state:"RUNNING"}}};
  w.c.updateAgentControls();
  assert.equal(w.node("agent-cancel-task").disabled,false,"active dispatch remains cancellable despite provisional unknown record");
  w.c.state.agentTask.record.lifecycle={state:"UNKNOWN_BLOCKED"};
  w.c.updateAgentControls();
  assert.equal(w.node("agent-cancel-task").disabled,true,"finished unknown state has no replay/cancel dispatch");

  const failedIsolation=setup(), messages=[];
  failedIsolation.install("checkPublicCandidate");
  failedIsolation.c.agentStatus=message=>messages.push(message);
  await failedIsolation.c.checkPublicCandidate({public_test:{status:"ISOLATION_BLOCKED"}},{revision:1});
  assert.equal(failedIsolation.calls.length,0);
  assert.match(messages[0],/隔离.*已停止.*修复名额已保留/);

  console.log("GUI lifecycle: exact one-command approval, stale/cancel gates, read-only race-safe polling and host liveness passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
