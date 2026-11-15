'use strict';
// Uses the user's already-installed Qoder SDK. No package downloads at runtime.
const fs=require('fs'), path=require('path');
const SYSTEM='You are an external coding agent. The only task material is public-bundle.json. Read that file and write analysis.py implementing its contract. Do not access any other path, execute code, use network tools, spawn agents, or inspect settings/history. Preserve the source you write; no markdown output is accepted as a program.';
function toolAllowed(name,input,workspace){
 if(!input||typeof input.file_path!=='string')return false;
 const target=path.resolve(workspace,input.file_path),base=path.resolve(workspace);
 if(name==='Read')return target===path.join(base,'public-bundle.json');
 if(name!=='Write'||target!==path.join(base,'analysis.py')||typeof input.content!=='string'||Buffer.byteLength(input.content)>65536)return false;
 try{if(fs.lstatSync(target).isSymbolicLink())return false;}catch(e){if(e.code!=='ENOENT')return false;}
 return true;
}
function optionsFor(sdk,request,events){
 let calls=0;
 const permit=async(name,input,opts)=>({behavior:toolAllowed(name,input,request.workspace)?'allow':'deny',message:'Only the exact public input and candidate output are allowed.',toolUseID:opts.toolUseID});
 return {
  auth:request.auth_mode==='pat'?sdk.accessToken(request.token):sdk.qodercliAuth(),
  transport:new sdk.WorkerTransport({pathToQoderWorkerRuntime:request.runtime}),
  cwd:request.workspace,settingSources:[],settings:{general:{enableAutoUpdate:false}},
  systemPrompt:SYSTEM,tools:request.mode==='catalog'?[]:['Read','Write'],skills:[],plugins:[],mcpServers:{},strictMcpConfig:true,
  memory:{mode:'custom',projectScope:false,userScope:false,generation:{enabled:false},consumption:{enabled:false}},
  evolution:{skill:{mode:'custom',generation:{enabled:false}}},promptSuggestions:false,
  persistSession:false,maxTurns:4,permissionMode:'default',canUseTool:permit,
  extraArgs:{'max-model-request-retries':'0','max-output-tokens':'8192','context-window':'200000'},
  hooks:{PreToolUse:[{hooks:[async(input)=>{
   const allowed=toolAllowed(input.tool_name,input.tool_input,request.workspace);
   events.push({kind:'tool_boundary',tool:input.tool_name,allowed});
   return {hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:allowed?'allow':'deny',permissionDecisionReason:'Exact public workspace boundary'}};
  }]}]},
  resolveModel:(context)=>{
   if(request.mode==='catalog')throw new Error('CATALOG_NO_GENERATION');
   if(context.purpose!=='main'||++calls>4)throw new Error('MODEL_REQUEST_LIMIT');
   const model=context.availableModels.find(m=>m.value===request.model&&m.isEnabled!==false);
   if(!model||!Object.values(model.context_config||{}).some(c=>c.token_count===200000))throw new Error('MODEL_UNAVAILABLE');
   events.push({kind:'model_request_admitted',ordinal:calls,model:model.value,purpose:context.purpose,context_window:200000});
   return {model:model.value,parameters:{contextWindow:200000,reasoningEffort:'low'}};
  },stderr:()=>{}
 };
}
async function main(request){
 if(!['catalog','generate'].includes(request.mode))throw new Error('MODE_INVALID');
 const sdk=require(request.sdk),events=[];let q,release;let submit=false;let result=null;let modelList=[];
 const gate=new Promise(r=>release=r);
 async function* input(){await gate;if(submit)yield {type:'user',message:{role:'user',content:'Read public-bundle.json and write the complete analysis.py now. Do not run it.'},parent_tool_use_id:null};}
 try{
  q=sdk.query({prompt:input(),options:optionsFor(sdk,request,events)});
  const consume=(async()=>{for await(const msg of q){if(msg.type==='result')result=msg;}})();
  await q.initializationResult();
  modelList=await q.getAvailableModels();
  if(request.mode==='catalog')return {status:'catalog',models:modelList.map(m=>({value:m.value,displayName:m.displayName,isEnabled:m.isEnabled,context_config:m.context_config})),events};
  if(!modelList.some(m=>m.value===request.model&&m.isEnabled!==false))throw new Error('MODEL_UNAVAILABLE');
  submit=true;release();await consume;
  const errors=(result?.errors||[]).map(String);
  if(result?.subtype!=='success')return {status:'unresolved',result_subtype:result?.subtype,code:errors.some(x=>/login|auth/i.test(x))?'AUTH_REQUIRED':'QODER_UNRESOLVED',events};
  const usage=result.usage||null;
  return {status:'complete',result_subtype:result.subtype,events,usage,model_usage:result.modelUsage||null,total_credits:result.total_credits??null,configured_model:request.model};
 }catch(e){return {status:'unresolved',code:/login|auth/i.test(String(e.message))?'AUTH_REQUIRED':'QODER_UNRESOLVED',events};}
 finally{release?.();if(q)await q.close();}
}
module.exports={toolAllowed,optionsFor,main};
if(require.main===module){
 const chunks=[];let count=0;
 process.stdin.on('data',chunk=>{count+=chunk.length;if(count>131072){process.exitCode=2;process.stdin.destroy();return;}chunks.push(chunk);});
 process.stdin.on('end',async()=>{try{const result=await main(JSON.parse(Buffer.concat(chunks).toString('utf8')));process.stdout.write(JSON.stringify(result)+'\n');}catch(e){process.stdout.write('{"status":"unresolved","code":"BRIDGE_INVALID"}\n');process.exitCode=1;}});
}
