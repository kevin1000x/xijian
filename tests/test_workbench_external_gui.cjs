"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../workbench/app.js"),"utf8");
function node(tag,text){return {tag,textContent:text,children:[],listeners:{},hidden:false,disabled:false,
  append(...values){this.children.push(...values);},replaceChildren(){this.children=[];},
  setAttribute(){},addEventListener(kind,fn){this.listeners[kind]=fn;}};}
const nodes=new Map(),calls=[];
const context={state:{},$:(id)=>{if(!nodes.has(id))nodes.set(id,node(id));return nodes.get(id);},el:node,
  api:async(url,body)=>{calls.push({url,body});return data;},task:async fn=>fn(),refreshAgent:async()=>{}};
vm.createContext(context);
for(const name of ["externalReason","externalStatus","renderExternalAgents","renderExternalPriority","showExternalEntry"]){
  const one=source.match(new RegExp("function "+name+"[^\\n]+"))[0];
  const fn=one.endsWith("}")?one:source.match(new RegExp("function "+name+"\\([^\\n]*\\)[\\s\\S]*?\\n}"))[0];
  vm.runInContext(fn,context);
}
const data={entries:[{id:"codex-a",kind:"codex",generation_supported:true,name:"Codex <img>",installed:true,
  executable:"C:\\A&B\\codex.exe",interface_status:"compatible",auth_status:"unauthenticated",authorized:false,
  probe:{login_home:"C:\\external's home"}},{id:"custom-b",kind:"custom_acp",name:"Custom",installed:true,
  interface_status:"entry_unsupported",auth_status:"unknown",authorized:false}],
  preferences:{priority:["codex-a","custom-b"],fixed_id:null,allow_fallback:false},selection:{selected:null,reason:"no_approved_agents"}};
context.renderExternalAgents(data);
assert.equal(calls.length,0,"rendering does not probe or generate");
const first=nodes.get("external-agent-list").children[0];
assert.equal(first.children[0].textContent,"Codex <img>","untrusted names use text");
assert.match(first.children[1].textContent,/接口兼容.*未登录.*任务未验证.*未授权/);
context.showExternalEntry("codex-a");
assert.match(nodes.get("external-login-help").textContent,/external''s home/);
assert.equal(nodes.get("local-agent-approved").checked,false);
assert.equal(nodes.get("enable-local-agent").disabled,false);
data.entries.push({id:"other-ready",kind:"opencode",name:"OpenCode",installed:true,
  generation_supported:true,interface_status:"compatible",auth_status:"not_required",authorized:false});
context.showExternalEntry("other-ready");
assert.equal(nodes.get("enable-local-agent").disabled,false,"ready adapters are configurable regardless of brand");
data.entries.push({id:"claude-ready",kind:"claude",name:"Claude Code",installed:true,
  executable:"C:\\Tools\\claude.exe",generation_supported:true,interface_status:"compatible",
  probe:{login_environment:{HOME:"C:\\external-home",CLAUDE_CONFIG_DIR:"C:\\external-config","INJECT;evil":"ignored"}}});
context.showExternalEntry("claude-ready");
assert.match(nodes.get("external-login-help").textContent,/CLAUDE_CONFIG_DIR.*[\s\S]*'auth' 'login'/);
assert.doesNotMatch(nodes.get("external-login-help").textContent,/INJECT/);
context.showExternalEntry("custom-b");
assert.equal(nodes.get("enable-local-agent").disabled,true);
const priority=nodes.get("external-priority").children[0];priority.children[2].listeners.click();
assert.equal(context.state.externalPriority[0],"custom-b");
assert.equal(calls.length,0,"reordering is local until save");
console.log("External GUI: distinct states, text-only names, login scope, unsupported gate and explicit preferences passed");
