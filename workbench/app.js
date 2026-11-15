/* Local-only workbench. Imported strings are always rendered as text nodes. */
"use strict";
const $ = (id) => document.getElementById(id);
const state = {token:null,dataset:null,spec:null,plans:[],runs:[],preview:null,result:null,busy:false,revision:0,nextId:1,resultContext:null,agentTask:null,agentReadiness:null,registration:null,polling:false,pollFailures:0,stopped:false};
const labels = {count:"行数",sum:"合计",mean:"平均值",min:"最小值",max:"最大值",eq:"等于",ne:"不等于",gt:"大于",ge:"大于等于",lt:"小于",le:"小于等于"};
function el(tag, text, cls) { const node=document.createElement(tag); if(text!==undefined)node.textContent=String(text); if(cls)node.className=cls; return node; }
function clone(value){return JSON.parse(JSON.stringify(value));}
function uid(prefix){return prefix+(state.nextId++);}
const customizedLabels=new WeakSet();
function defaultLabel(item){return item.op==="count"?"行数":item.op==="difference"?"净额":item.op==="ratio"?"比率":(item.column||"")+labels[item.op];}
function automaticLabel(item){const base=defaultLabel(item);return !customizedLabels.has(item)&&(item.label===base||item.label.startsWith(base+" · "));}
function uniqueLabel(base,id){const used=new Set([...(state.spec?.metrics||[]),...(state.spec?.derived||[])].filter(item=>item.id!==id).map(item=>item.label.trim()));let label=base,index=2;while(used.has(label))label=base+" · "+index++;return label;}
function changeMetric(item,key,value){const automatic=automaticLabel(item);item[key]=value;if(key==="op")item.column=value==="count"?null:(item.column||fields(true)[0]||"");if(automatic)item.label=uniqueLabel(defaultLabel(item),item.id);invalidate();renderMetrics();renderDerived();renderSort();}
function message(text,error=false){$("message").textContent=text;$("message").classList.toggle("error",error);$("message").hidden=!text;}
async function api(path,body,signal){
  const options={credentials:"same-origin",headers:{},signal};
  if(body!==undefined){options.method="POST";options.headers={"Content-Type":"application/json","X-Workbench-Token":state.token};options.body=JSON.stringify(body);}
  const response=await fetch(path,options);let data;
  try{data=await response.json();}catch(_){throw new Error("本地服务未返回有效结果。请检查服务是否仍在运行。");}
  if(!response.ok||(data.error&&!['EXECUTION_FAILED','UNKNOWN_BLOCKED'].includes(data.status)))throw new Error(data.error?.message||"操作未完成，请检查输入后重试。");
  return data;
}
async function task(action){
  if(state.busy)return;
  state.busy=true;document.body.setAttribute("aria-busy","true");
  const controls=Array.from(document.querySelectorAll("button,input,select,textarea"));
  const original=controls.map(n=>[n,n.disabled]);controls.forEach(n=>{if(!n.id.startsWith("agent-cancel-"))n.disabled=true;});
  try{await action();}catch(error){message(error.message,true);}
  finally{state.busy=false;document.body.removeAttribute("aria-busy");original.forEach(([n,disabled])=>{if(n.isConnected)n.disabled=disabled;});if($("agent-draft"))$("agent-draft").disabled=!(state.agentReadiness?.internal?.ready&&state.agentReadiness?.external?.ready);updateAgentControls();}
}
function invalidate(){
  state.revision++;state.preview=null;$("confirmation").hidden=true;
  if(state.result){
    message("草稿已修改，尚未重新运行。下方保留的是先前结果。");
    if(!state.resultContext?.historical){$("result-status").textContent="上一版结果 · 当前草稿尚未运行";$("result-context").textContent="以下是上一版结果，来自「"+(state.resultContext?.name||"先前数据")+"」。当前数据或任务已修改，尚未重新计算。";}
    $("save-plan-section").hidden=true;$("export-link").textContent="导出所示旧结果";
  }
  if(state.agentTask){state.agentTask.stale=true;agentStatus("任务草稿已修改；下面保留的是上一版协作记录，尚未再次提交。");}
  ["agent-publish","agent-continue-generation","agent-continue-execution","agent-resume-note"].forEach(id=>{if($(id))$(id).hidden=true;});
  if($("agent-interpret"))$("agent-interpret").hidden=true;
  checkCompatibility();
}
function fields(numericOnly=false){return (state.dataset?.columns||[]).filter(c=>!numericOnly||c.type!=="string").map(c=>c.name);}
function fieldType(name){return state.dataset?.columns.find(c=>c.name===name)?.type;}
function select(options,value,onchange,aria){
  const control=el("select");if(aria)control.setAttribute("aria-label",aria);
  const values=options.map(option=>typeof option==="string"?[option,option]:option);
  if(value!==undefined&&value!==null&&!values.some(([key])=>String(key)===String(value)))values.push([value,"字段不可用 · "+value]);
  values.forEach(([key,label])=>{const item=el("option",label);item.value=key;control.append(item);});
  if(value!==undefined&&value!==null)control.value=value;
  control.addEventListener("change",()=>onchange(control.value));return control;
}
function input(value,oninput,label){const control=el("input");control.value=value??"";control.maxLength=200;control.setAttribute("aria-label",label);control.addEventListener("input",()=>oninput(control.value));return control;}
function labeled(text,control){const label=el("label",text);label.append(control);return label;}
function removeButton(label,remove){const button=el("button","×","remove-button");button.type="button";button.setAttribute("aria-label",label);button.addEventListener("click",remove);return button;}
function emptySpec(){return {version:1,group_by:[],filters:[],metrics:[],derived:[],sort:[],limit:null};}
function savedSpec(value){
  const object=item=>item!==null&&typeof item==="object"&&!Array.isArray(item),text=item=>typeof item==="string"&&item.trim().length>0;
  if(!object(value))throw new Error("保存的分析方案格式无效，当前编辑内容已保留。");
  const spec={...emptySpec(),...clone(value)};
  const arrays=["group_by","filters","metrics","derived","sort"];
  const valid=spec.version===1&&arrays.every(key=>Array.isArray(spec[key]))
    &&spec.group_by.every(text)
    &&spec.filters.every(item=>object(item)&&text(item.column)&&['eq','ne','gt','ge','lt','le'].includes(item.op)&&(typeof item.value==="string"||(typeof item.value==="number"&&Number.isFinite(item.value))))
    &&spec.metrics.every(item=>object(item)&&text(item.id)&&text(item.label)&&['count','sum','mean','min','max'].includes(item.op)&&(item.op==="count"?(item.column===null||item.column===undefined):text(item.column)))
    &&spec.derived.every(item=>object(item)&&text(item.id)&&text(item.label)&&['difference','ratio'].includes(item.op)&&text(item.left)&&text(item.right))
    &&spec.sort.every(item=>object(item)&&text(item.key)&&['asc','desc'].includes(item.direction))
    &&(spec.limit===null||(Number.isInteger(spec.limit)&&spec.limit>0));
  if(!valid)throw new Error("保存的分析方案包含无效字段或指标，当前编辑内容已保留。");
  spec.metrics.forEach(item=>{if(item.op==="count")item.column=null;});return spec;
}
function preset(kind){
  if(!state.dataset){message("先选择表格，或点击「渠道表现」试用合成样例。");return;}
  const numeric=fields(true),strings=state.dataset.columns.filter(c=>c.type==="string").map(c=>c.name);
  if(!numeric.length){message("这张表没有可计算的数值列，可以添加「行数」指标。",true);return;}
  const spec=emptySpec();spec.group_by=strings.length?[strings[0]]:[];
  spec.metrics=[{id:uid("m"),op:"sum",column:numeric[0],label:numeric[0]+"合计"}];
  if(kind==="net"){
    if(numeric.length<2){message("分类净额需要两列数值。导入包含金额与扣减额的表后重试。",true);return;}
    spec.metrics.push({id:uid("m"),op:"sum",column:numeric[1],label:numeric[1]+"合计"});
    spec.derived=[{id:uid("d"),op:"difference",left:spec.metrics[0].id,right:spec.metrics[1].id,label:"净额"}];
    document.querySelector(".derived-block").open=true;
  }
  state.spec=spec;invalidate();renderBuilder();message(kind==="net"?"已填入净额示例，可修改字段与计算口径。":"已填入求和示例，可继续添加指标和筛选。");
}
function renderGroups(){
  const container=$("group-fields");container.replaceChildren();
  const all=[...fields(),...state.spec.group_by.filter(name=>!fields().includes(name))];
  all.forEach(name=>{
    const check=el("input");check.type="checkbox";check.checked=state.spec.group_by.includes(name);
    check.addEventListener("change",()=>{if(check.checked&&state.spec.group_by.length>=3){check.checked=false;message("最多同时按 3 个字段分组。",true);return;}state.spec.group_by=check.checked?[...state.spec.group_by,name]:state.spec.group_by.filter(item=>item!==name);invalidate();renderSort();});
    const label=el("label",undefined,"field-checkbox");label.append(check,el("span",name+(fields().includes(name)?"":" · 不可用")));container.append(label);
  });
}
function renderMetrics(){
  const container=$("metric-rows");container.replaceChildren();
  state.spec.metrics.forEach((metric,index)=>{
    const row=el("div",undefined,"metric-row");
    const operation=select(["sum","count","mean","min","max"].map(op=>[op,labels[op]]),metric.op,value=>changeMetric(metric,"op",value),"指标 "+(index+1)+" 计算方式");
    const column=select(metric.op==="count"?[["","全部行"]]:fields(true),metric.column??"",value=>changeMetric(metric,"column",value),"指标 "+(index+1)+" 数据列");column.disabled=metric.op==="count";
    row.append(labeled("计算",operation),labeled("数据列",column),labeled("结果名称",input(metric.label,value=>{customizedLabels.add(metric);metric.label=value;invalidate();renderSort();},"指标 "+(index+1)+" 结果名称")),removeButton("删除指标 "+(index+1),()=>{state.spec.metrics.splice(index,1);invalidate();renderMetrics();renderDerived();renderSort();}));
    container.append(row);
  });
}
function renderFilters(){
  const container=$("filter-rows");container.replaceChildren();
  state.spec.filters.forEach((filter,index)=>{
    const row=el("div",undefined,"filter-row");
    const ops=fieldType(filter.column)==="string"?["eq","ne"]:["eq","ne","gt","ge","lt","le"];
    row.append(labeled("字段",select(fields(),filter.column,value=>{filter.column=value;if(fieldType(value)==="string"&&!['eq','ne'].includes(filter.op))filter.op="eq";invalidate();renderFilters();},"筛选 "+(index+1)+" 字段")),labeled("条件",select(ops.map(op=>[op,labels[op]]),filter.op,value=>{filter.op=value;invalidate();},"筛选 "+(index+1)+" 条件")),labeled("值",input(filter.value,value=>{filter.value=value;invalidate();},"筛选 "+(index+1)+" 值")),removeButton("删除筛选 "+(index+1),()=>{state.spec.filters.splice(index,1);invalidate();renderFilters();}));container.append(row);
  });
  $("filter-count").textContent=state.spec.filters.length?state.spec.filters.length+" 个条件，全部满足":"未添加条件";
}
function renderDerived(){
  const container=$("derived-rows");container.replaceChildren();
  const options=[["","请选择指标"],...state.spec.metrics.map(metric=>[metric.id,metric.label||metric.id])];
  state.spec.derived.forEach((derived,index)=>{
    const row=el("div",undefined,"derived-row");
    row.append(labeled("左侧指标",select(options,derived.left,value=>{derived.left=value;invalidate();},"计算 "+(index+1)+" 左侧指标")),labeled("计算",select([["difference","减去"],["ratio","除以"]],derived.op,value=>{const automatic=automaticLabel(derived);derived.op=value;if(automatic)derived.label=uniqueLabel(defaultLabel(derived),derived.id);invalidate();renderDerived();renderSort();},"计算 "+(index+1)+" 方式")),labeled("右侧指标",select(options,derived.right,value=>{derived.right=value;invalidate();},"计算 "+(index+1)+" 右侧指标")),labeled("结果名称",input(derived.label,value=>{customizedLabels.add(derived);derived.label=value;invalidate();renderSort();},"计算 "+(index+1)+" 结果名称")),removeButton("删除计算 "+(index+1),()=>{state.spec.derived.splice(index,1);invalidate();renderDerived();renderSort();}));container.append(row);
  });
}
function renderSort(){
  const key=$("sort-key"),value=state.spec.sort[0]?.key||"";key.replaceChildren();
  const options=[["","保留默认顺序"],...state.spec.group_by.map((name,index)=>["g"+index,name]),...state.spec.metrics.map(metric=>[metric.id,metric.label||metric.id]),...state.spec.derived.map(derived=>[derived.id,derived.label||derived.id])];
  if(value&&!options.some(([id])=>id===value)){state.spec.sort=[];}
  options.forEach(([id,label])=>{const option=el("option",label);option.value=id;key.append(option);});
  key.value=state.spec.sort[0]?.key||"";$("sort-direction").value=state.spec.sort[0]?.direction||"desc";$("row-limit").value=state.spec.limit??"";
}
function renderBuilder(){
  $("builder-empty").hidden=!!state.dataset;$("builder").hidden=!state.dataset;if(!state.dataset)return;
  renderGroups();renderMetrics();renderFilters();renderDerived();renderSort();checkCompatibility();
}
function checkCompatibility(){
  if(!state.dataset||!state.spec)return;
  const required=[...state.spec.group_by,...state.spec.filters.map(f=>f.column),...state.spec.metrics.filter(m=>m.op!=="count").map(m=>m.column)];
  const missing=[...new Set(required.filter(name=>!fields().includes(name)))];
  const wrongType=state.spec.metrics.filter(m=>m.op!=="count"&&fieldType(m.column)==="string").map(m=>m.column);
  const ids=state.spec.metrics.map(m=>m.id),orphan=state.spec.derived.some(d=>!ids.includes(d.left)||!ids.includes(d.right));
  const notes=[];if(missing.length)notes.push("新表缺少字段："+missing.join("、")+"。请重新选择对应字段。");if(wrongType.length)notes.push("以下字段不是数值列："+[...new Set(wrongType)].join("、")+"。");if(orphan)notes.push("有计算尚未选择完整，或引用了已删除的指标。请明确选择两侧指标。");
  const names=[...state.spec.metrics,...state.spec.derived].map(item=>item.label.trim()),duplicates=[...new Set(names.filter((name,index)=>name&&names.indexOf(name)!==index))];if(duplicates.length)notes.push("结果名称重复："+duplicates.join("、")+"。请为每个指标取不同名称。");
  $("compatibility").textContent=notes.join(" ");$("compatibility").hidden=!notes.length;
  return notes.length===0;
}
function normalizedSpec(){
  if(!checkCompatibility())throw new Error("请先修正标出的字段或指标，再查看分析口径。");
  const spec=clone(state.spec);if(!spec.metrics.length)throw new Error("至少添加一个计算指标，例如销售额合计或行数。");
  spec.filters.forEach(filter=>{if(fieldType(filter.column)!=="string"){if(String(filter.value).trim()===""||!Number.isFinite(Number(filter.value)))throw new Error("数值字段「"+filter.column+"」需要有效的筛选数字。");const number=Number(filter.value);if(fieldType(filter.column)==="integer"&&!Number.isSafeInteger(number))throw new Error("整数筛选值需在浏览器可精确表达的整数范围内。");filter.value=number;}else filter.value=String(filter.value);});
  return spec;
}
function display(value){return value===null||value===undefined?"—":String(value);}
function renderTable(container,columns,rows){
  container.replaceChildren();if(!rows.length){container.append(el("p","没有符合当前筛选的记录。可以调整筛选条件后重新运行。","empty-table"));return;}
  const table=el("table"),head=el("thead"),header=el("tr"),body=el("tbody");
  columns.forEach(column=>{const cell=el("th",column.label??column.name);cell.scope="col";if(column.type!=="string")cell.className="numeric";header.append(cell);});head.append(header);
  rows.forEach(row=>{const tr=el("tr");columns.forEach(column=>{const value=row[column.key??column.name],cell=el("td",display(value));cell.title=display(value);if(column.type!=="string")cell.className="numeric";tr.append(cell);});body.append(tr);});table.append(head,body);container.append(table);
}
async function importData(payload){
  const dataset=await api("/api/import",payload);state.dataset=dataset;invalidate();
  if(!state.spec){state.spec=emptySpec();const numeric=fields(true);const group=dataset.columns.find(c=>c.type==="string");state.spec.group_by=group?[group.name]:[];state.spec.metrics=[{id:uid("m"),op:numeric.length?"sum":"count",column:numeric[0]??null,label:numeric.length?numeric[0]+"合计":"行数"}];}
  $("dataset-info").hidden=false;$("dataset-name").textContent=dataset.name;$("dataset-size").textContent=dataset.row_count+" 行 · "+dataset.columns.length+" 列";
  $("column-list").replaceChildren();dataset.columns.forEach(column=>{const row=el("div",undefined,"column-item");row.append(el("span",column.name),el("span",{string:"文本",integer:"整数",number:"数值"}[column.type]||column.type,"column-type"));$("column-list").append(row);});
  $("data-preview-section").hidden=false;$("preview-count").textContent="预览前 "+dataset.preview.length+" 行";renderTable($("data-preview"),dataset.columns,dataset.preview);renderBuilder();updateAgentControls();
  const warnings=(dataset.warnings||[]).map(w=>typeof w==="string"?w:w.message||w.code).join(" ");message("已读取「"+dataset.name+"」。"+(checkCompatibility()?"已有分析口径已保留，可继续修改或复用。":"请匹配新表字段后继续。")+(warnings?" "+warnings:""));
}
async function readFile(file){
  if(!file)return;if(file.size>2*1024*1024)throw new Error("文件超过 2 MB，请用较小的合成数据表试用。");
  const ext=file.name.split(".").pop().toLowerCase();if(!["csv","tsv","xlsx"].includes(ext))throw new Error("请选择 CSV、TSV 或 XLSX 文件。");
  if(ext==="xlsx"){const bytes=new Uint8Array(await file.arrayBuffer());let binary="";for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));await importData({name:file.name,base64:btoa(binary),format:"xlsx"});}
  else await importData({name:file.name,text:await file.text(),format:ext});
}
function renderPlans(){
  const container=$("plan-list");container.replaceChildren();
  if(!state.plans.length){container.append(el("p","保存计算口径，下次换一张表继续用。","muted small"));return;}
  state.plans.forEach(plan=>{const button=el("button",undefined,"plan-item");button.type="button";button.append(el("span",plan.name),el("span","↗"));button.addEventListener("click",()=>task(()=>{const spec=savedSpec(plan.spec);const ids=[...spec.metrics,...spec.derived].map(m=>Number(m.id.replace(/^\D+/,""))).filter(Number.isFinite);state.spec=spec;state.nextId=Math.max(state.nextId,...ids.map(id=>id+1));invalidate();renderBuilder();$("plan-name").value=plan.name;document.querySelector(".derived-block").open=state.spec.derived.length>0;document.querySelector(".filter-block").open=state.spec.filters.length>0;message(state.dataset?"已载入「"+plan.name+"」，确认口径后即可运行。":"已载入「"+plan.name+"」，请导入字段相同的新数据。 ");}));container.append(button);});
}
function renderRuns(){
  const container=$("run-list");container.replaceChildren();
  if(!state.runs.length){container.append(el("p","运行完成后，可在这里重新查看结果。","muted small"));return;}
  state.runs.slice(0,10).forEach(run=>{const button=el("button",undefined,"plan-item");button.type="button";button.append(el("span",run.name||"已保存的分析"),el("span","↗"));button.title=(run.name||"已保存的分析")+" · "+(run.created_at||"");button.addEventListener("click",()=>task(async()=>{const result=await api("/api/runs/"+encodeURIComponent(run.id));const successful=renderResult(result,run);message(successful?"已读取保存的运行结果，没有重新计算。当前编辑中的分析口径已保留。":"已读取历史运行记录；该运行没有得到可用结果，请查看状态与原因。",!successful);}));container.append(button);});
}
function renderResult(result,historical=null,submitted=null){
  const successful=result.status==="EXECUTED_UNVERIFIED"&&result.correctness==="NOT_EVALUATED";
  const unknown=result.status==="UNKNOWN_BLOCKED";
  const rows=Array.isArray(result.rows)?result.rows:[],columns=Array.isArray(result.columns)?result.columns:[];
  let reason=result.error?.message||result.reason||result.failure_reason||result.message||result.error||"运行未完成，未生成可用的分析结果。";
  if(typeof reason!=="string")reason=JSON.stringify(reason);
  state.result=result;state.resultContext=historical?{historical:true,name:historical.name}:submitted||{dataset_id:state.dataset?.dataset_id,name:state.dataset?.name,spec:clone(state.spec),revision:state.revision};$("export-link").textContent=historical?"导出历史结果":"导出 CSV";$("results").hidden=false;$("confirmation").hidden=true;
  $("results-heading").textContent=historical?"历史分析结果":"分析结果";
  $("result-context").textContent=historical?"历史数据："+(historical.name||"已保存的分析")+(historical.created_at?" · "+historical.created_at:"")+"。此结果来自保存的运行，不代表当前编辑中的数据与口径。":"数据："+(state.resultContext?.name||"当前表格")+" · 提交版本 "+state.resultContext?.revision;
  $("save-plan-section").hidden=!!historical||!successful||!state.resultContext?.spec;
  $("result-status").textContent=successful?"已执行 · 数值未经独立认证":unknown?"运行状态未知 · 已阻断":"执行未成功 · 没有可用结果";
  $("result-count").textContent=successful?rows.length+" 条结果":"本次未得到可用结果";$("export-link").href="/api/export/"+encodeURIComponent(result.run_id);$("export-link").hidden=!successful;$("chart-tab").hidden=!successful;
  $("result-warnings").replaceChildren();(result.warnings||[]).forEach(w=>$("result-warnings").append(el("p",typeof w==="string"?w:w.message||w.code)));
  if(successful)renderTable($("result-table"),columns,rows);else{$("result-table").replaceChildren(el("p",reason+(unknown?" 为避免重复执行，该次运行已阻断。请保留运行编号并联系维护者检查。":" 请检查数值范围与计算口径；调整后重新查看口径，再发起新的运行。"),"inline-warning"));}
  const chartMetrics=columns.filter(column=>column.type!=="string"&&(!(result.source==="local_deterministic"||!result.source)||!column.key.startsWith("g")));$("chart-metric").replaceChildren();chartMetrics.forEach(column=>{const option=el("option",column.label);option.value=column.key;$("chart-metric").append(option);});
  $("run-details").replaceChildren();[["计算来源",result.source==="local_deterministic"||!result.source?"本地规则组合":result.source==="local_test_double"?"离线测试程序 · 无模型调用":result.source],["运行状态",result.status],["正确性状态",result.correctness],["运行编号",result.run_id]].forEach(([key,value])=>$("run-details").append(el("dt",key),el("dd",display(value))));
  switchTab("table");if(successful)renderChart();else $("chart").replaceChildren();$("results").scrollIntoView({behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth",block:"start"});return successful;
}
function switchTab(kind){["table","chart"].forEach(name=>{$(name+"-tab").setAttribute("aria-selected",String(name===kind));$(name+"-tab").tabIndex=name===kind?0:-1;$(name+"-panel").hidden=name!==kind;});}
function renderChart(){
  const container=$("chart");container.replaceChildren();if(!state.result)return;
  const key=$("chart-metric").value,groups=state.result.columns.filter(column=>(state.result.source==="local_deterministic"||!state.result.source)?column.key.startsWith("g"):column.type==="string");
  const safeRows=state.result.rows.filter(row=>typeof row[key]==="number"&&Number.isFinite(row[key])&&(!Number.isInteger(row[key])||Number.isSafeInteger(row[key])));
  const omitted=state.result.rows.length-safeRows.length,rows=safeRows.slice(0,20);
  if(!rows.length){container.append(el("p","没有可安全绘图的数值。完整数值仍可在表格中查看或导出。","muted small"));$("chart-note").textContent="空值与超出浏览器精确整数范围的值不绘图。";return;}
  const minimum=Math.min(0,...rows.map(row=>row[key])),maximum=Math.max(0,...rows.map(row=>row[key]));
  const scale=Math.max(Math.abs(minimum),Math.abs(maximum))||1,min=minimum/scale,max=maximum/scale,span=max-min||1,zero=-min/span*100;
  rows.forEach((row,index)=>{const line=el("div",undefined,"bar-row"),name=groups.length?groups.map(column=>display(row[column.key])).join(" / "):"全表汇总";const label=el("span",name,"bar-label");label.title=name;
    const track=el("div",undefined,"bar-track"),baseline=el("span",undefined,"bar-zero"),bar=el("span",undefined,"bar-fill"+(row[key]<0?" negative":""));baseline.style.left=zero+"%";bar.style.left=(row[key]<0?zero+row[key]/scale/span*100:zero)+"%";bar.style.width=Math.abs(row[key]/scale/span*100)+"%";track.append(baseline,bar);track.setAttribute("aria-hidden","true");line.append(label,track,el("span",display(row[key]),"bar-value"));container.append(line);
  });
  const axis=el("div",undefined,"chart-axis");axis.append(el("span",display(minimum)),el("span",display(maximum)));container.append(axis);
  $("chart-note").textContent="共同零基线；负值向左，正值向右。"+(safeRows.length>20?"仅绘制当前结果前 20 个有效数值。":"")+(omitted?" "+omitted+" 条空值或超出浏览器精确整数范围的值未绘图，请查看表格。":"");
}
$("choose-file").addEventListener("click",()=>$("file-input").click());
$("file-input").addEventListener("change",()=>task(async()=>{await readFile($("file-input").files[0]);$("file-input").value="";}));
$("dropzone").addEventListener("dragover",event=>{event.preventDefault();if(!state.busy)$("dropzone").classList.add("dragover");});
$("dropzone").addEventListener("dragleave",()=>$("dropzone").classList.remove("dragover"));
$("dropzone").addEventListener("drop",event=>{event.preventDefault();$("dropzone").classList.remove("dragover");if(event.dataTransfer.files.length!==1){message("请一次导入一个表格文件。",true);return;}task(()=>readFile(event.dataTransfer.files[0]));});
$("paste-button").addEventListener("click",()=>task(async()=>{const text=$("paste-data").value;if(!text.trim())throw new Error("先粘贴包含表头的数据，再点击读取。");if(new TextEncoder().encode(text).length>2*1024*1024)throw new Error("粘贴内容超过 2 MB，请减少数据量。");await importData({name:"粘贴的表格",text,format:text.includes("\t")?"tsv":"csv"});}));
$("example-button").addEventListener("click",()=>task(async()=>{
  const text="渠道,区域,销售额,退款额,订单数\n直营,华东,18600,1200,62\n电商,华东,25400,3100,94\n合作伙伴,华南,12900,600,43\n直营,华南,14200,900,48\n电商,华南,21800,2200,81\n合作伙伴,华东,16400,1100,55\n直营,华北,11300,500,38\n电商,华北,19200,1800,72\n合作伙伴,华北,9700,400,32\n";
  await importData({name:"渠道表现 · 合成样例.csv",text,format:"csv"});
}));
$("preset-sum").addEventListener("click",()=>preset("sum"));$("preset-net").addEventListener("click",()=>preset("net"));
$("add-metric").addEventListener("click",()=>{if(state.spec.metrics.length>=16){message("最多添加 16 个基础指标。",true);return;}const column=fields(true)[0];state.spec.metrics.push({id:uid("m"),op:column?"sum":"count",column:column??null,label:uniqueLabel(column?column+"合计":"行数")});invalidate();renderMetrics();renderDerived();renderSort();});
$("add-filter").addEventListener("click",()=>{if(state.spec.filters.length>=16){message("最多添加 16 个筛选条件。",true);return;}state.spec.filters.push({column:fields()[0],op:"eq",value:""});invalidate();renderFilters();});
$("add-derived").addEventListener("click",()=>{if(state.spec.derived.length>=16){message("最多添加 16 个指标计算。",true);return;}if(!state.spec.metrics.length){message("先添加基础指标，再计算净额或比率。",true);return;}state.spec.derived.push({id:uid("d"),op:"difference",left:"",right:"",label:uniqueLabel("净额")});invalidate();renderDerived();renderSort();});
$("sort-key").addEventListener("change",()=>{state.spec.sort=$("sort-key").value?[{key:$("sort-key").value,direction:$("sort-direction").value}]:[];invalidate();});
$("sort-direction").addEventListener("change",()=>{if(state.spec.sort.length)state.spec.sort[0].direction=$("sort-direction").value;invalidate();});
$("row-limit").addEventListener("change",()=>{state.spec.limit=$("row-limit").value?Number($("row-limit").value):null;invalidate();});
$("preview-button").addEventListener("click",()=>task(async()=>{
  const revision=state.revision,spec=normalizedSpec(),preview=await api("/api/preview",{dataset_id:state.dataset.dataset_id,spec});if(revision!==state.revision)return;
  state.preview={...preview,spec,revision};$("summary-list").replaceChildren();(preview.summary||[]).forEach(line=>$("summary-list").append(el("li",line)));$("confirmation").hidden=false;message("计算口径已整理，请确认后运行。");$("confirmation").scrollIntoView({behavior:"auto",block:"nearest"});
}));
$("run-button").addEventListener("click",()=>task(async()=>{
  if(!state.preview||state.preview.revision!==state.revision)throw new Error("数据或口径已变化，请重新查看分析口径。");
  const revision=state.revision,preview=state.preview,submitted={dataset_id:state.dataset.dataset_id,name:state.dataset.name,spec:clone(preview.spec),revision};state.preview=null;$("confirmation").hidden=true;
  const result=await api("/api/run",{dataset_id:state.dataset.dataset_id,spec:preview.spec,confirmation:preview.confirmation});
  if(revision!==state.revision)return;const successful=renderResult(result,null,submitted);state.runs=[{id:result.run_id,name:state.dataset.name,created_at:new Date().toISOString(),status:result.status},...state.runs.filter(run=>run.id!==result.run_id)];renderRuns();message(successful?"分析完成。可以导出结果，或保存口径供下次复用。":result.status==="UNKNOWN_BLOCKED"?"运行状态未知，已阻断重复执行。请保留运行编号并联系维护者检查。":"执行未成功，没有可用结果。请查看原因，调整数据或口径后重新确认。",!successful);
}));
$("save-form").addEventListener("submit",event=>{event.preventDefault();task(async()=>{const name=$("plan-name").value.trim();if(!name)throw new Error("请填写分析方案名称。");if(!state.resultContext?.spec||state.resultContext.historical||state.resultContext.revision!==state.revision)throw new Error("当前草稿尚未运行，不能作为所示结果的方案保存。");const plan=await api("/api/plans",{name,dataset_id:state.resultContext.dataset_id,spec:clone(state.resultContext.spec)});state.plans=[plan,...state.plans.filter(item=>item.id!==plan.id)];renderPlans();message("已保存「"+name+"」。换一张字段相同的表即可再次使用。");});});
$("table-tab").addEventListener("click",()=>switchTab("table"));$("chart-tab").addEventListener("click",()=>switchTab("chart"));
document.querySelector(".result-tabs").addEventListener("keydown",event=>{if(["ArrowLeft","ArrowRight","Home","End"].includes(event.key)){event.preventDefault();const kind=event.key==="Home"?"table":event.key==="End"?"chart":$("table-tab").getAttribute("aria-selected")==="true"?"chart":"table";switchTab(kind);$(kind+"-tab").focus();}});
$("chart-metric").addEventListener("change",renderChart);
$("shutdown-button").addEventListener("click",()=>task(async()=>{await api("/api/shutdown",{});$("service-status").textContent="服务已停止";state.token=null;state.stopped=true;message("本地服务已停止。下次从桌面入口重新打开工作台。");window.setTimeout(()=>document.querySelectorAll("button,input,select,textarea").forEach(control=>control.disabled=true),0);}));
task(async()=>{const bootstrap=await api("/api/bootstrap");state.token=bootstrap.csrf_token;$("app-version").textContent=bootstrap.version||bootstrap.app_version||"本地开发版";state.plans=bootstrap.plans||[];state.runs=bootstrap.runs||[];renderPlans();renderRuns();message("");});

// Tasks cross domains only through explicit backend publication and connector gates.
function agentStatus(text,error=false){$("agent-state").textContent=text;$("agent-state").classList.toggle("error",error);}
function renderReadiness(info){
  state.agentReadiness=info;
  if(info.external?.source==="remote_node"&&info.external.operation_id){
    $("remote-task-id").value=info.external.operation_id;
  }
  if(!state.configurationLoaded&&["http","enterprise_mcp"].includes(info.configuration?.mode)){
    const c=info.configuration;state.configurationLoaded=true;$("internal-mode").value=c.mode;
    $("api-settings").hidden=c.mode!=="http";$("enterprise-settings").hidden=c.mode!=="enterprise_mcp";
    if(c.mode==="enterprise_mcp"){$("enterprise-actor").value=c.actor_id;$("enterprise-allocation").value=c.host_token_reservation;$("enterprise-budget-ack").checked=c.host_budget_enforced===true;}
    else{$("internal-protocol").value=c.protocol;$("internal-url").value=c.url;$("internal-model").value=c.model;$("internal-aliases").value=(c.model_aliases||[]).join(",");}
  }
  const host=info.host||{},registrations=(host.registrations||[]).filter(item=>!item.revoked);
  if(state.registration)state.registration=registrations.find(item=>item.registration_id===state.registration.registration_id)||null;
  if(!state.registration&&registrations.length===1)state.registration=registrations[0];
  const registrationSignature=JSON.stringify(registrations.map(item=>[item.registration_id,item.connected]));
  if(registrationSignature!==state.registrationSignature){state.registrationSignature=registrationSignature;$("mcp-registration-select").replaceChildren();
  if(!state.registration&&registrations.length>1){const placeholder=el("option","选择要管理的连接");placeholder.value="";$("mcp-registration-select").append(placeholder);}
  if(registrations.length){registrations.forEach(item=>{const option=el("option","连接 "+item.registration_id.slice(0,8)+(item.connected?" · 已连接":" · 离线"));option.value=item.registration_id;$("mcp-registration-select").append(option);});}}
  $("mcp-registration-select").hidden=registrations.length<2;
  if(state.registration)$("mcp-registration-select").value=state.registration.registration_id;
  $("agent-connections").replaceChildren();
  [["内部",info.internal],["外部生成",info.external]].forEach(([name,route])=>{
    const mcp=route?.source==="enterprise_mcp",claimed=Array.isArray(host.claimed_tasks)?host.claimed_tasks.length:Number(host.claimed_tasks||0);
    const budgetExhausted=route?.missing?.includes("token_budget"),budgetBlocked=route?.missing?.includes("live_budget_bound_unverified");
    const status=budgetExhausted?"剩余预算不足 · 新生成暂停":budgetBlocked?"预算上界待验证 · 生成暂停":mcp?(host.connected?"宿主已连接"+(claimed?" · 已领取 "+claimed+" 项任务":" · 尚未领取任务"):"已配置 · 等待宿主连接"):(route?.ready?(route.live===false?"离线测试 · 无模型调用":"连接已就绪"):"尚未就绪");
    const node=el("span",name+" · "+status);node.title=(route?.missing||[]).map(code=>code==="live_budget_bound_unverified"?"尚未验证真实调用的预算上界，生成暂停":code).join("、")+(host.last_seen?" · 上次联系 "+host.last_seen:"");$("agent-connections").append(node);
  });
  $("agent-draft").disabled=state.busy||!(info.internal?.ready&&info.external?.ready);
  $("agent-mcp-enroll").hidden=info.internal?.source!=="enterprise_mcp";
  updateBudget(info.token_budget);updateAgentControls();
}
async function refreshAgent(){
  try{renderReadiness(await api("/api/agent/readiness"));await refreshAgentTasks();if(!state.agentTask)agentStatus(state.agentReadiness.internal?.source==="enterprise_mcp"?"先连接内部 Agent，并明确允许其使用本次输入；公开材料仍需逐次批准。":"准备任务后，检查并批准外部可见材料。后台将推进已批准的任务。");}
  catch(_){state.agentReadiness=null;$("agent-draft").disabled=true;$("agent-connections").textContent="协作服务暂不可达 · 已保存记录不会重复提交";}
}
function taskBlocked(record){return !!record?.lifecycle?.cancelled||/CANCEL/.test(record?.status||"")||(!lifecycleActive(record)&&(/UNKNOWN/.test(record?.status||"")||/UNKNOWN|CANCEL/.test(record?.lifecycle?.state||"")));}
function lifecycleActive(record){return record?.lifecycle?.state==="RUNNING";}
function canRecoverRemoteCandidate(record){
  const receipt=record?.generation_receipt,life=record?.lifecycle;
  if(receipt?.source!=="remote_node"||!receipt.task_id||life?.cancelled||lifecycleActive(record))return false;
  return record.status==="GENERATION_UNKNOWN_BLOCKED"||
    (record.status==="CANDIDATE_READY"&&!!record.remote_recovery&&life?.state==="UNKNOWN_BLOCKED"&&
      life.operation?.action==="generate"&&life.operation?.status==="unknown");
}
function updateAgentControls(){
  const record=state.agentTask?.record,blocked=taskBlocked(record),active=lifecycleActive(record);
  $("agent-cancel-task").hidden=!record;$("agent-cancel-task").disabled=!record||blocked||["INTERPRETED_UNVERIFIED","DRAFT_FAILED","EXECUTION_FAILED"].includes(record.status);
  ["agent-publish","agent-continue-generation","agent-continue-execution","agent-interpret","agent-public-test","agent-repair","agent-reuse"].forEach(id=>{if(blocked||active)$(id).hidden=true;});
  $("agent-remote-recover").hidden=!canRecoverRemoteCandidate(record);
  $("agent-remote-recover").disabled=state.busy||state.agentReadiness?.external?.node?.node_id!==record?.generation_receipt?.node_id;
  $("agent-mcp-admit").hidden=!state.registration;$("agent-mcp-admit").disabled=state.busy||!state.dataset;
  $("agent-mcp-revoke").hidden=!state.registration;
  const admitted=state.registration?.dataset_ids?.includes(state.dataset?.dataset_id);
  $("mcp-input-scope").textContent=state.dataset?(admitted?"所选连接已获准使用「":"待授权输入：「")+state.dataset.name+"」。只授权这份输入，不包含其他历史数据。":"可先创建连接，导入数据后再单独授权。";
  if(typeof updateExternalControls==="function")updateExternalControls();
}
function renderLifecycle(record){
  const life=record.lifecycle||{},node=$("agent-lifecycle");node.hidden=false;
  const names={IDLE:"尚未推进",RUNNING:"处理中",WAITING_FOR_HOST:"等待内部 Agent",WAITING_APPROVAL:"等待批准公开材料",WAITING_PUBLIC_REPAIR_APPROVAL:"等待批准修复材料",COMPLETE:"处理完成",CANCELLED:"取消意图已保存",UNKNOWN_BLOCKED:"状态未知 · 不自动重试",BLOCKED:"当前步骤受阻"};
  node.textContent=(life.mode==="hosted_api"?"后台托管":life.mode==="host_mcp"?"宿主 Agent 协作":"已有协作记录")+" · "+(names[life.state]||life.state||$("agent-bundle-state").textContent)+(life.owner?" · 执行连接："+life.owner.slice(0,8):"")+(life.owner_expired?" · 领取已过期":"");
  if(life.cancelled)node.textContent+="。后续步骤停止推进；远端是否停止及实际计费仍需核对。";
  else if(life.operation?.status==="STARTED")node.textContent+=" · 当前操作："+life.operation.action;
  node.classList.toggle("attention",taskBlocked(record));updateAgentControls();
}
async function pollAgent(){
  if(state.stopped)return;
  if(state.polling||state.busy||document.hidden){window.setTimeout(pollAgent,2000);return;}
  state.polling=true;const controller=new AbortController(),timeout=window.setTimeout(()=>controller.abort(),10000);
  const entry=state.agentTask,revision=state.revision;
  try{
    const requests=[api("/api/agent/readiness",undefined,controller.signal),api("/api/agent/tasks",undefined,controller.signal)];
    if(entry)requests.push(api("/api/agent/tasks/"+encodeURIComponent(entry.record.proposal_id),undefined,controller.signal));
    const [info,tasks,record]=await Promise.all(requests);if(state.busy)return;
    renderReadiness(info);renderAgentTasks(tasks);
    if(record&&state.agentTask===entry&&revision===state.revision){
      if(JSON.stringify(record)!==JSON.stringify(entry.record)){
        const expanded=Array.from($("agent-public-files").querySelectorAll("details")).map(n=>n.open);
        renderAgent(record,entry.snapshot);
        $("agent-public-files").querySelectorAll("details").forEach((n,i)=>{if(expanded[i]!==undefined)n.open=expanded[i];});
        if(record.result&&!entry.stale&&record.result.run_id!==state.result?.run_id)renderResult(record.result,null,entry.snapshot);
      }else renderLifecycle(record);
    }
    state.pollFailures=0;$("agent-sync").textContent="状态自动更新 · "+new Date().toLocaleTimeString();
  }catch(_){state.pollFailures++;$("agent-sync").textContent="暂时无法同步，将自动重连；不会重新发送或执行任务。";}
  finally{window.clearTimeout(timeout);state.polling=false;window.setTimeout(pollAgent,Math.min(15000,2000*Math.max(1,state.pollFailures)));}
}
window.setTimeout(pollAgent,2000);
function renderAgent(record,snapshot){
  const remoteReceipt=record.generation_receipt;
  if(remoteReceipt?.source==="remote_node"){
    const delivery=record.remote_recovery?.status||remoteReceipt.status;
    $("remote-task-id").value=remoteReceipt.task_id;
    $("remote-node-status").textContent=`节点任务 ${remoteReceipt.task_id}：${delivery}`;
  }
  const selection=record.external_selection;
  $("agent-selected-route").textContent=selection?"本任务外侧："+selection.source+(selection.version?" / "+selection.version:"")+(selection.model?" / "+selection.model:"")+" · "+(record.reused_from?"复用原程序，本次未调用外侧":externalReason(selection.reason)):"";
  state.agentTask={record,snapshot,stale:snapshot.revision!==state.revision};if(snapshot.dataset_id&&!snapshot.historical){try{sessionStorage.setItem("agent-context-"+record.proposal_id,JSON.stringify({dataset_id:snapshot.dataset_id,name:snapshot.name}));}catch(_){}}$("agent-proposal").hidden=false;
  if(state.result&&!record.result){
    $("result-status").textContent="上一任务结果 · 当前任务尚无结果";
    $("result-context").textContent="当前所选协作任务尚无运行结果。下面保留的是先前运行 "+state.result.run_id+" 的结果，与当前任务无关。";
    $("export-link").textContent="导出所示旧结果";$("save-plan-section").hidden=true;
  }
  renderAgentVersions(record);
  $("agent-public-files").replaceChildren();
  if(record.public_preview){const details=el("details"),summary=el("summary","查看公开包内容"),pre=el("pre",record.public_preview);details.open=record.status==="AWAITING_PUBLICATION_APPROVAL";details.append(summary,pre);$("agent-public-files").append(details);}
  if(record.candidate_preview){const details=el("details"),summary=el("summary","查看收到的原始程序"),pre=el("pre",record.candidate_preview);details.append(summary,pre);$("agent-public-files").append(details);}
  $("agent-public-digest").textContent="提案摘要："+(record.proposal_sha256||"尚未形成")+(record.candidate_sha256?" · 程序摘要："+record.candidate_sha256:"");
  const statusLabels={AWAITING_INTERNAL_PROPOSAL:"等待企业 Agent 提案",AWAITING_PUBLICATION_APPROVAL:"待批准公开材料",PUBLISHED:"已发布",CANDIDATE_READY:"已收到程序",EXECUTED_UNVERIFIED:"已内部执行",INTERPRETED_UNVERIFIED:"已返回内部解读",DRAFT_FAILED:"任务草拟失败",DRAFT_UNKNOWN_BLOCKED:"内部请求待核对",GENERATION_UNKNOWN_BLOCKED:"外部交付未知 · 不重复发送",EXECUTION_UNKNOWN_BLOCKED:"内部执行待核对",INTERPRETATION_UNKNOWN_BLOCKED:"解释请求未知 · 保留结果",EXECUTION_FAILED:"执行失败"};$("agent-bundle-state").textContent=statusLabels[record.status]||record.status||"待发布";
  $("agent-publish").hidden=state.agentTask.stale||snapshot.historical||!record.proposal_sha256||!!record.candidate_sha256||!['AWAITING_PUBLICATION_APPROVAL'].includes(record.status);
  const mayContinue=!state.agentTask.stale&&!snapshot.historical&&!taskBlocked(record)&&!lifecycleActive(record);
  $("agent-continue-generation").hidden=!mayContinue||record.status!=="PUBLISHED";
  $("agent-continue-execution").hidden=!mayContinue||record.status!=="CANDIDATE_READY"||!record.candidate_sha256||(record.public_test&&record.public_test.status!=="PASSED");
  const resumeMessage=record.public_test&&record.public_test.status!=="PASSED"?"此候选未通过公开检查，内部执行入口已暂停。查看公开失败后准备修复版本；未知测试不重跑。":record.status==="PUBLISHED"?"公开材料已批准，尚未登记外部请求。继续时使用已保存的同一份材料。":record.status==="CANDIDATE_READY"?"原程序已保存，尚未提交执行。继续时使用此任务原有的输入快照，不使用当前新导入的表。":["GENERATION_UNKNOWN_BLOCKED","EXECUTION_UNKNOWN_BLOCKED"].includes(record.status)?"这一步已提交，结果尚未确认。可刷新查看最新记录，不能从这里重复发送或执行。":"";
  $("agent-resume-note").textContent=resumeMessage;$("agent-resume-note").hidden=!resumeMessage;
  $("agent-reuse").hidden=!record.candidate_sha256||!record.result||record.result.status!=="EXECUTED_UNVERIFIED"||(record.public_test&&record.public_test.status!=="PASSED");
  $("agent-events").replaceChildren();const eventLabels={public_repair_prepared:"新公开修复版本已准备，等待批准",task_revision_prepared:"已保留上一版本并准备新任务",enterprise_context_prepared:"已准备企业 Agent 上下文",enterprise_interpretation_received:"已接收企业 Agent 提交的结果说明",internal_request_started:"已请求内部模型，等待返回",proposal_validated:"公开任务已形成，等待批准",exact_publication_approved:"已批准并发布本次公开材料",external_request_started:"已开始外部生成请求",original_candidate_received:"已接收原始候选程序",remote_candidate_recovered:"已从原节点取回原程序",original_candidate_reused:"已复用原始程序，本次未调用模型",external_response_unresolved:"外部交付状态未知，已阻止重复发送",execution_started:"已提交内部执行",execution_observed:"内部执行状态已记录",internal_interpretation_started:"已请求内部模型解读实际结果",internal_interpretation_received:"已收到内部结果解读",internal_interpretation_unresolved:"内部解释请求状态未知，已保留结果",internal_request_failed_or_invalid:"内部请求失败或提案不符合契约"};(record.events||[]).forEach(event=>{const item=el("li",event.sequence+" · "+(record.source==="local_test_double"?"[离线测试] ":"")+(eventLabels[event.kind]||event.kind));item.title=event.status||"";$("agent-events").append(item);});
  const explanation=record.interpretation;$("agent-interpretation").hidden=!explanation;$("agent-interpretation").textContent=explanation?(explanation.source==="local_test_double"?"离线测试解释（无模型调用）\n":explanation.source==="enterprise_mcp"?"企业 Agent 提交的解读（未经独立认证）\n":"内部模型解读（未经独立认证）\n")+(explanation.text||""):"";
  $("agent-mcp").hidden=true;
  $("agent-interpret").hidden=state.agentReadiness?.internal?.source==="enterprise_mcp"||state.agentTask.stale||snapshot.historical||!record.result||record.result.status!=="EXECUTED_UNVERIFIED"||record.status!=="EXECUTED_UNVERIFIED"||!!explanation;
  renderLifecycle(record);
}
function renderAgentVersions(record){
  const kind={original:"原始任务",revision:"任务修订",repair:"程序修复"}[record.version_kind]||"原始任务";
  $("agent-lineage").textContent=kind+" · "+record.proposal_id.slice(0,8)+(record.parent_id?" · 来自 "+record.parent_id.slice(0,8):"")+(record.version_kind==="repair"?" · 第 "+(record.repair_index||record.repair_depth)+" 次修复（最多 2 次）":"");
  $("agent-child").hidden=!record.repair_child_id;$("agent-parent").hidden=!record.parent_id;$("agent-revise").hidden=false;
  const unknown=taskBlocked(record),test=record.public_test;
  $("agent-public-test").hidden=!record.candidate_sha256||!!test||unknown;
  $("agent-repair").hidden=test?.status!=="FAILED"||unknown||(record.repair_slots_used||0)>=2||!!record.repair_child_id;
  $("agent-public-test-status").textContent=!test?"公开样例尚未检查。":test.status==="PASSED"?"公开样例运行及输出约定通过；数值正确性尚未独立评估。":test.status==="FAILED"&&record.repair_child_id?"首次公开失败已保留；修复版本 "+record.repair_child_id.slice(0,8)+" 已另存，可从上方查看。":test.status==="FAILED"&&(record.repair_slots_used||0)>=2?"公开样例未通过，本任务家族的两次修复名额已使用；失败记录保留。":test.status==="FAILED"?"公开样例未通过（"+(test.reason_code||"contract_failure")+"）。可准备修复版本，上一程序和首次失败将保留。":"公开样例执行结果未知；保留现场，不重复执行或请求修复。";
}
async function checkPublicCandidate(record,snapshot){
  if(!record.public_test){
    agentStatus("正在本机检查已公开的独立样例；不会发送内部输入或结果。");
    record=await api("/api/agent/public-test",{proposal_id:record.proposal_id,candidate_sha256:record.candidate_sha256});
    record=await api("/api/agent/tasks/"+encodeURIComponent(record.proposal_id));
    if(snapshot.revision===state.revision)renderAgent(record,snapshot);
  }
  if(record.public_test?.status!=="PASSED")agentStatus("公开样例未通过，当前程序保留。检查失败状态后可准备新的公开修复版本。",true);
  return record;
}
$("agent-public-test").addEventListener("click",()=>task(async()=>{
  const entry=state.agentTask;if(!entry)throw new Error("请先选择任务。");
  await checkPublicCandidate(entry.record,entry.snapshot);await refreshAgentTasks();
}));
$("agent-repair").addEventListener("click",()=>task(async()=>{
  const entry=state.agentTask;if(!entry)throw new Error("请先选择任务。");
  const snapshot={...entry.snapshot,revision:state.revision,historical:false};
  const record=await api("/api/agent/repair",{proposal_id:entry.record.proposal_id,candidate_sha256:entry.record.candidate_sha256});
  renderAgent(record,snapshot);await refreshAgentTasks();
  agentStatus("修复版本已准备，尚未发送。请查看包括原程序和公开失败类型的新材料，再批准协作。");
}));
$("agent-revise").addEventListener("click",()=>task(async()=>{
  const entry=state.agentTask,text=$("natural-language").value.trim();
  if(!entry||!text)throw new Error("选择原任务，并在上方填写修订后的完整任务。未导入新表时沿用原任务输入。");
  const snapshot={dataset_id:state.dataset?.dataset_id,name:state.dataset?.name||"原任务输入",revision:state.revision,spec:null};
  const record=await api("/api/agent/revise",{proposal_id:entry.record.proposal_id,dataset_id:snapshot.dataset_id,task:text});
  if(snapshot.revision!==state.revision)return;renderAgent(record,snapshot);await refreshAgentTasks();
  agentStatus(record.status==="AWAITING_INTERNAL_PROPOSAL"?"修订已另存为新任务。内部 Agent 可通过此任务 MCP 提交新提案。":"修订已另存为新任务，旧材料、程序与结果仍可从上一版本查看。");
}));
$("agent-child").addEventListener("click",()=>task(async()=>{
  const id=state.agentTask?.record.repair_child_id;if(!id)return;
  const record=await api("/api/agent/tasks/"+encodeURIComponent(id)),snapshot={revision:state.revision,name:"修复版本",historical:!["AWAITING_PUBLICATION_APPROVAL","PUBLISHED","CANDIDATE_READY"].includes(record.status)};
  renderAgent(record,snapshot);if(record.result)renderResult(record.result,{name:"修复版本"});
  agentStatus("已读取保存的修复版本，没有再次提交。");
}));
$("agent-parent").addEventListener("click",()=>task(async()=>{
  const id=state.agentTask?.record.parent_id;if(!id)return;
  const record=await api("/api/agent/tasks/"+encodeURIComponent(id)),snapshot={revision:state.revision,name:"上一版本",historical:true};
  renderAgent(record,snapshot);if(record.result)renderResult(record.result,{name:"上一版本"});
  agentStatus("已读取上一版本；没有重新生成或执行。");
}));
async function currentAgentRecord(snapshot,id){const record=await api("/api/agent/tasks/"+encodeURIComponent(id));if(snapshot.revision===state.revision)renderAgent(record,snapshot);updateBudget((await api("/api/agent/readiness")).token_budget);return record;}
$("agent-refresh").addEventListener("click",()=>task(refreshAgent));
$("natural-language").addEventListener("input",()=>invalidate());
$("agent-draft").addEventListener("click",()=>task(async()=>{
  if(!state.dataset)throw new Error("请先导入本次使用的合成数据。");const text=$("natural-language").value.trim();if(!text)throw new Error("请描述本次任务。");
  const snapshot={dataset_id:state.dataset.dataset_id,name:state.dataset.name,revision:state.revision,spec:null};agentStatus(state.agentReadiness?.internal?.source==="enterprise_mcp"?"正在准备企业 Agent 的内部上下文。":state.agentReadiness?.internal?.live===false?"正在使用离线测试传输准备任务，无模型调用。":"已请求内部模型准备任务，尚未向外部发送材料。");
  try{const record=await api("/api/agent/draft",{dataset_id:snapshot.dataset_id,task:text});if(snapshot.revision!==state.revision)return;renderAgent(record,snapshot);agentStatus(record.status==="AWAITING_INTERNAL_PROPOSAL"?"任务已准备，等待已连接的内部 Agent 领取并提交提案；状态会自动更新。":"内部提案已返回。检查外部可见材料后，再批准本次公开内容。");}
  catch(error){agentStatus(error.message,true);throw error;}
}));
async function advanceAgent(start){
  const entry=state.agentTask;if(!entry||entry.stale||entry.snapshot.revision!==state.revision||taskBlocked(entry.record))throw new Error("任务已修改或已停止，请查看保存的记录。");
  const expected={publish:"AWAITING_PUBLICATION_APPROVAL",generate:"PUBLISHED",run:"CANDIDATE_READY"};
  if(entry.record.status!==expected[start])throw new Error("当前阶段已变化，请等待状态同步。");
  const payload={proposal_id:entry.record.proposal_id};
  if(start==="publish")payload.proposal_sha256=entry.record.proposal_sha256;
  const record=await api("/api/agent/advance",payload);
  if(state.agentTask===entry&&entry.snapshot.revision===state.revision)renderAgent(record,entry.snapshot);
  agentStatus(record.lifecycle?.mode==="hosted_api"?"后台已接收。关闭页面不会中断后续推进；状态将在这里自动更新。":"操作已保存，等待内部 Agent 继续处理。MCP 配置本身不会唤醒宿主。");
}
$("agent-publish").addEventListener("click",()=>task(()=>advanceAgent("publish")));
$("agent-continue-generation").addEventListener("click",()=>task(()=>advanceAgent("generate")));
$("agent-continue-execution").addEventListener("click",()=>task(()=>advanceAgent("run")));
async function recoverRemoteCandidate(){
  const entry=state.agentTask;
  if(!canRecoverRemoteCandidate(entry?.record))throw new Error("请先选择待取回的节点任务。");
  const record=await api("/api/agent/remote-recover",{proposal_id:entry.record.proposal_id});
  if(state.agentTask===entry){
    const snapshot={...entry.snapshot,historical:false};
    renderAgent(record,snapshot);
    agentStatus("原程序已保存。确认后可继续处理原任务。");
  }
  await refreshAgentTasks();
}
$("agent-remote-recover").addEventListener("click",()=>task(recoverRemoteCandidate));
$("agent-interpret").addEventListener("click",()=>task(async()=>{
  const entry=state.agentTask;if(!entry||entry.stale)throw new Error("任务已修改，请查看原任务记录。");const result=entry.record.result;
  const record=await api("/api/agent/interpret",{proposal_id:entry.record.proposal_id,run_id:result.run_id});if(entry.snapshot.revision===state.revision)renderAgent(record,entry.snapshot);
}));
refreshAgent();
async function refreshAgentTasks(){
  renderAgentTasks(await api("/api/agent/tasks"));
}
function renderAgentTasks(data){
  const records=Array.isArray(data)?data:(data.tasks||[]),container=$("agent-task-list"),signature=JSON.stringify(records);if(signature===state.taskListSignature)return;state.taskListSignature=signature;container.replaceChildren();
  if(!records.length){container.append(el("p","尚无协作任务。","muted small"));return;}
  records.slice(0,20).forEach(item=>{const button=el("button",(item.task||item.title||"任务 "+(item.proposal_id||item.id).slice(0,8)).slice(0,60),"plan-item");button.type="button";button.title=item.status||"";button.addEventListener("click",()=>task(async()=>{
    const record=await api("/api/agent/tasks/"+encodeURIComponent(item.proposal_id||item.id)),snapshot={revision:state.revision,name:"历史协作任务",spec:null,historical:true};
    if(["AWAITING_INTERNAL_PROPOSAL","AWAITING_PUBLICATION_APPROVAL","PUBLISHED","CANDIDATE_READY"].includes(record.status)){let saved=null;try{saved=JSON.parse(sessionStorage.getItem("agent-context-"+record.proposal_id));}catch(_){}Object.assign(snapshot,saved||{},{historical:false});}
    renderAgent(record,snapshot);
    if(record.result)renderResult(record.result,{name:"历史协作任务"});agentStatus(["AWAITING_INTERNAL_PROPOSAL","AWAITING_PUBLICATION_APPROVAL","PUBLISHED","CANDIDATE_READY"].includes(record.status)?"已恢复待处理任务，继续使用此任务原有的输入快照。":"已读取保存的协作任务；没有再次生成或执行。新任务请从上方重新提出。");
  }));container.append(button);});
}

$("agent-reuse").addEventListener("click",()=>task(async()=>{
  const entry=state.agentTask;if(!entry?.record?.candidate_sha256||!state.dataset)throw new Error("先读取已成功执行的协作任务，并导入本次合成数据。");
  const snapshot={dataset_id:state.dataset.dataset_id,name:state.dataset.name,revision:state.revision,spec:null};
  agentStatus("正在检查新输入是否兼容，并复用所示原始程序；不会请求外部重新生成。");
  try{const record=await api("/api/agent/reuse",{proposal_id:entry.record.proposal_id,dataset_id:snapshot.dataset_id});
    if(snapshot.revision!==state.revision)return;renderAgent(record,snapshot);if(record.result)renderResult(record.result,null,snapshot);await refreshAgentTasks();updateBudget((await api("/api/agent/readiness")).token_budget);
    agentStatus(record.result?.status==="EXECUTED_UNVERIFIED"?"原始程序已在新数据上执行。未重新生成程序，也未请求模型解释。":"复用执行未成功；保留本次状态及原始任务。",record.result?.status!=="EXECUTED_UNVERIFIED");
  }catch(error){agentStatus(error.message,true);throw error;}
}));

function updateBudget(value){
  if(!value)return;const n=v=>Number.isFinite(v)?v.toLocaleString():"未知";
  $("token-budget").textContent="往来上限 "+n(value.ceiling)+" · 保守扣账 "+n(value.conservative_charged??value.charged)+" · 在途预留 "+n(value.reserved)+" · 账本可分配余额 "+n(value.ledger_allocatable??value.remaining)+" Token。实测 Token："+(value.observed_operations?n(value.observed_tokens):"未知")+(value.unknown_operations?" · 尚有 "+n(value.unknown_operations)+" 项用量未知":"")+"。未知部分不代表零用量；账本余额不是供应商实际剩余额度。";
}
$("internal-mode").addEventListener("change",()=>{$("api-settings").hidden=$("internal-mode").value!=="http";$("enterprise-settings").hidden=$("internal-mode").value!=="enterprise_mcp";});
$("qoder-auth").addEventListener("change",()=>{$("qoder-token-label").hidden=$("qoder-auth").value!=="pat";});
$("configure-internal").addEventListener("click",()=>task(async()=>{
  const mode=$("internal-mode").value,payload={mode,authorized:true};
  if(mode==="http")Object.assign(payload,{protocol:$("internal-protocol").value,url:$("internal-url").value.trim(),model:$("internal-model").value.trim(),credential:$("internal-credential").value,model_aliases:$("internal-aliases").value.split(",").map(x=>x.trim()).filter(Boolean)});
  else Object.assign(payload,{actor_id:$("enterprise-actor").value.trim(),host_budget_enforced:$("enterprise-budget-ack").checked,host_token_reservation:Number($("enterprise-allocation").value)});
  await api("/api/agent/configure",payload);$("internal-credential").value="";$("mcp-configuration").hidden=true;await refreshAgent();message("内部连接已应用。先前任务仍保留原有路线身份。");
}));
$("configure-qoder").addEventListener("click",()=>task(async()=>{
  agentStatus("正在核对 Qoder 登录与模型目录，此步骤不提交生成任务。");
  const value=await api("/api/agent/configure",{mode:"qoder_sdk",authorized:true,auth_mode:$("qoder-auth").value,credential:$("qoder-credential").value,model:$("qoder-model").value});
  $("qoder-credential").value="";
  const selected=$("qoder-model").value;if(value.probe?.models){$("qoder-model").replaceChildren();value.probe.models.forEach(model=>{const option=el("option",model.name);option.value=model.value;$("qoder-model").append(option);});$("qoder-model").value=selected;}
  await refreshAgent();message(value.readiness.ready?"Qoder 已连接，模型为 "+value.readiness.model+"。":"Qoder 尚未就绪："+(value.readiness.missing||[]).join("、"),!value.readiness.ready);
}));
$("agent-resume").addEventListener("click",()=>task(async()=>{
  const entry=state.agentTask;if(!entry)throw new Error("请先准备或打开一个协作任务。");
  const record=await currentAgentRecord(entry.snapshot,entry.record.proposal_id);if(record.result)renderResult(record.result,null,entry.snapshot);await refreshAgentTasks();
  agentStatus("已读取当前任务状态："+$("agent-bundle-state").textContent+"。");
}));
$("agent-mcp").addEventListener("click",()=>task(async()=>{
  const entry=state.agentTask;if(!entry?.snapshot.dataset_id)throw new Error("请先准备本次任务。");
  const value=await api("/api/agent/mcp-session",{proposal_id:entry.record.proposal_id,dataset_id:entry.snapshot.dataset_id});
  $("mcp-config-text").textContent=JSON.stringify(value.client_config,null,2);$("mcp-configuration").hidden=false;$("mcp-configuration").open=true;
}));
$("agent-cancel-task").addEventListener("click",async()=>{
  const entry=state.agentTask;if(!entry||$("agent-cancel-task").disabled)return;
  $("agent-cancel-task").disabled=true;
  try{const value=await api("/api/agent/cancel-task",{proposal_id:entry.record.proposal_id});
    if(state.agentTask===entry){if(value.proposal_id)renderAgent(value,entry.snapshot);else if(value.task_id===entry.record.proposal_id)renderAgent({...entry.record,lifecycle:value},entry.snapshot);}
    agentStatus("取消意图已保存，后续步骤不再推进。本地停止状态以任务记录为准；远端是否停止及计费情况未知。");
  }catch(error){message(error.message,true);updateAgentControls();}
});
$("mcp-registration-select").addEventListener("change",()=>{state.registration=(state.agentReadiness?.host?.registrations||[]).find(item=>item.registration_id===$("mcp-registration-select").value)||null;updateAgentControls();});
$("agent-mcp-enroll").addEventListener("click",()=>task(async()=>{
  const value=await api("/api/agent/mcp-register",{dataset_ids:[]});
  state.registration=value;$("mcp-config-text").textContent=JSON.stringify(value.client_config,null,2);$("mcp-configuration").hidden=false;$("mcp-configuration").open=true;
  agentStatus("连接已创建，尚未授权任何输入。将配置添加到内部 Agent，再明确授权本次导入的数据。");await refreshAgent();
}));
$("agent-mcp-admit").addEventListener("click",()=>task(async()=>{
  if(!state.registration?.registration_id||!state.dataset)throw new Error("请先创建连接并导入本次数据。");
  const dataset=state.dataset;await api("/api/agent/mcp-admit",{registration_id:state.registration.registration_id,dataset_ids:[dataset.dataset_id]});
  agentStatus("已明确授权内部 Agent 使用「"+dataset.name+"」。其他历史输入没有新增授权。");await refreshAgent();
}));
$("agent-mcp-revoke").addEventListener("click",()=>task(async()=>{
  if(!state.registration?.registration_id)return;
  await api("/api/agent/mcp-revoke",{registration_id:state.registration.registration_id});state.registration=null;$("mcp-configuration").hidden=true;
  agentStatus("连接授权已撤销。历史记录保留；已运行任务请另用取消整项任务停止后续操作。");await refreshAgent();
}));

async function configureRemoteNode(){
  const payload={mode:"remote_node",authorized:true,url:$("remote-node-url").value.trim(),credential:$("remote-node-token").value};
  $("remote-node-token").value="";
  const result=await api("/api/agent/configure",payload);
  $("remote-node-status").textContent=`已连接 ${result.probe.source} · ${result.probe.model} · 节点 ${result.probe.node_id}${result.readiness?.external?.ready===false?" · 新生成暂停":""}`;
  await refreshAgent();
}
async function queryRemoteTask(){
  const result=await api("/api/external/remote-status",{task_id:$("remote-task-id").value.trim()});
  $("remote-node-status").textContent=`任务 ${result.task_id}：${result.status}`;
}
$("configure-remote-node").addEventListener("click",()=>task(configureRemoteNode));
$("query-remote-task").addEventListener("click",()=>task(queryRemoteTask));

$("configure-external-http").addEventListener("click",()=>task(async()=>{
  const payload={mode:"external_http",authorized:true,protocol:$("external-protocol").value,url:$("external-url").value.trim(),model:$("external-model").value.trim(),credential:$("external-credential").value,model_aliases:$("external-aliases").value.split(",").map(x=>x.trim()).filter(Boolean)};
  try{await api("/api/agent/configure",payload);await refreshAgent();agentStatus("外部 API 配置已应用。尚未发送公开包；新任务将绑定此路线。");}finally{$("external-credential").value="";}
}));

// Discovery reads installation metadata. Only the explicit probe action starts
// bounded CLI status queries; neither action sends a model prompt.
function externalReason(reason){return ({fixed_selected:"使用固定入口",preferred_selected:"按已保存优先级选择",approved_fallback_selected:"首选不可用，使用已批准后备",configured_route:"使用已配置路线",no_approved_agents:"尚无已授权可用入口",fixed_unavailable:"固定入口不可用，已暂停",no_eligible_approved_agent:"没有满足已保存选择规则的可用入口",entry_needs_check:"已保存入口需要重新检查",submission_already_started:"已提交，不重新选择"})[reason]||reason||"尚未选择";}
function externalStatus(row){
  const protocol={unchecked:"接口未检查",compatible:"接口兼容",incompatible:"当前版本暂不可接入",desktop_only:"已发现桌面应用",entry_unsupported:"入口已登记，暂不可调用",adapter_unsupported:"尚未适配",unavailable:"入口不可用",version_unknown:"版本未知",probe_failed:"检查失败",timeout:"检查超时",output_limit:"检查输出超限"};
  const auth={unknown:"登录状态未知",authenticated:"外侧目录已登录",unauthenticated:"外侧目录未登录",not_required:"此接口无需登录"};
  return [row.installed?"已发现":"未发现",protocol[row.interface_status]||row.interface_status,
    auth[row.auth_status]||"登录状态未知","任务未验证",row.authorized?"已授权":"未授权"].join(" · ");
}
function updateExternalControls(){
  const row=state.externalAgents?.entries?.find(item=>item.id===state.externalEntry);
  $("enable-local-agent").disabled=state.busy||!row||row.interface_status!=="compatible"||row.generation_supported!==true;
}
function renderExternalAgents(data){
  state.externalAgents=data;
  if(data.catalog){
    const manual=$("agent-manual-kind"),chosen=manual.value;manual.replaceChildren();
    for(const item of data.catalog){const option=el("option",item.name);option.value=item.kind;manual.append(option);}
    if(data.catalog.some(item=>item.kind===chosen))manual.value=chosen;
  }
  const list=$("external-agent-list");list.replaceChildren();
  for(const row of data.entries||[]){
    const card=el("article",undefined,"external-agent-card");
    card.append(el("h4",row.name+(row.version?" · "+row.version:"")),el("p",externalStatus(row),"small"));
    if(row.executable)card.append(el("p",row.executable,"external-entry-path small"));
    if(row.probe?.status==="boundary_unreviewed")card.append(el("p","ACP 接口声明已找到；此运行时的配置与工具边界尚未核实，暂不可用于外侧生成。","small"));
    if(row.installed){
      const actions=el("div",undefined,"external-entry-actions"),probe=el("button","检查接口","text-button"),choose=el("button","设置用途","text-button");
      probe.type=choose.type="button";
      probe.addEventListener("click",()=>task(async()=>{renderExternalAgents(await api("/api/external/probe",{entry_id:row.id}));showExternalEntry(row.id);await refreshAgent();}));
      choose.addEventListener("click",()=>showExternalEntry(row.id));actions.append(probe,choose);card.append(actions);
    }
    list.append(card);
  }
  const chosen=data.selection?.selected;
  $("external-agent-message").textContent=(chosen?"下一个新任务："+chosen.name+" · ":"")+externalReason(data.selection?.reason)+(data.active?"":"。当前仍使用原有连接配置。");
  const fixed=$("external-fixed");fixed.replaceChildren();const auto=el("option","按优先级选择");auto.value="";fixed.append(auto);
  for(const row of data.entries||[]){const option=el("option","固定："+row.name+(row.version?" · "+row.version:"")+" · "+row.id.slice(-6));option.value=row.id;fixed.append(option);}
  fixed.value=data.preferences?.fixed_id||"";$("external-fallback").checked=data.preferences?.allow_fallback===true;
  state.externalPriority=[...(data.preferences?.priority||[])];renderExternalPriority();
  if(state.externalEntry&&!data.entries.some(row=>row.id===state.externalEntry)){$("external-entry-settings").hidden=true;state.externalEntry=null;}
}
function renderExternalPriority(){
  const list=$("external-priority");list.replaceChildren();
  state.externalPriority.forEach((id,index)=>{
    const row=state.externalAgents.entries.find(item=>item.id===id),item=el("li");
    item.append(el("span",(row?.name||id)+" · "+id.slice(-6)+(row?.authorized?"":" · 未授权")));
    for(const [text,delta] of [["上移",-1],["下移",1]]){
      const button=el("button",text,"text-button");button.type="button";button.disabled=index+delta<0||index+delta>=state.externalPriority.length;
      button.setAttribute("aria-label",(row?.name||id)+text);
      button.addEventListener("click",()=>{const next=index+delta;[state.externalPriority[index],state.externalPriority[next]]=[state.externalPriority[next],state.externalPriority[index]];renderExternalPriority();});item.append(button);
    }
    list.append(item);
  });
}
function showExternalEntry(id){
  const row=state.externalAgents.entries.find(item=>item.id===id);if(!row)return;
  state.externalEntry=id;$("external-entry-settings").hidden=false;
  $("external-entry-title").textContent=row.name+" · 用途与使用范围";
  $("external-entry-detail").textContent=externalStatus(row);
  $("local-agent-model").value=row.approved_model||"";$("local-agent-allocation").value=row.allocation||100000;
  $("local-agent-invocations").value=row.max_invocations||1;$("local-agent-timeout").value=row.timeout_seconds||120;$("local-agent-approved").checked=false;
  $("enable-local-agent").disabled=row.interface_status!=="compatible"||row.generation_supported!==true;
  $("revoke-local-agent").hidden=!row.authorized;
  const help=$("external-login-help"),home=row.probe?.login_home;
  const environment=row.probe?.login_environment||(home&&row.kind==="codex"?{CODEX_HOME:home}:null);
  help.hidden=!environment;
  if(environment){
    const quote=text=>"'"+String(text).replaceAll("'","''")+"'";
    const allowed=new Set(["CODEX_HOME","CLAUDE_CONFIG_DIR","HOME","USERPROFILE","APPDATA","LOCALAPPDATA","XDG_CONFIG_HOME","XDG_DATA_HOME","XDG_CACHE_HOME","XDG_STATE_HOME","OPENCODE_TEST_HOME","OPENCODE_TEST_MANAGED_CONFIG_DIR","OPENCODE_DISABLE_PROJECT_CONFIG","OPENCODE_PURE","OPENCODE_DISABLE_DEFAULT_PLUGINS"]);
    const lines=Object.entries(environment).filter(([key])=>allowed.has(key)).map(([key,value])=>"$env:"+key+" = "+quote(value));
    const args=row.kind==="codex"?["login"]:["auth","login"];
    lines.push("& "+quote(row.executable)+" "+args.map(quote).join(" "));
    help.textContent="在新的 PowerShell 窗口登录此连接的独立目录，再返回检查接口：\n"+lines.join("\n");
  }
}
$("discover-agents").addEventListener("click",()=>task(async()=>{renderExternalAgents(await api("/api/external/discover",{}));}));
$("agent-add-entry").addEventListener("click",()=>task(async()=>{
  const entries=[...(state.externalAgents?.manual_entries||[]),{kind:$("agent-manual-kind").value,executable:$("agent-manual-path").value.trim()}];
  renderExternalAgents(await api("/api/external/discover",{manual_entries:entries}));
}));
$("enable-local-agent").addEventListener("click",()=>task(async()=>{
  renderExternalAgents(await api("/api/external/enable",{entry_id:state.externalEntry,authorized:$("local-agent-approved").checked,
    data_scope:"public_bundle_only",account_scope:"dedicated_external_home",model:$("local-agent-model").value.trim(),
    allocation:Number($("local-agent-allocation").value),max_invocations:Number($("local-agent-invocations").value),timeout_seconds:Number($("local-agent-timeout").value)}));
  showExternalEntry(state.externalEntry);await refreshAgent();
}));
$("revoke-local-agent").addEventListener("click",()=>task(async()=>{renderExternalAgents(await api("/api/external/revoke",{entry_id:state.externalEntry}));showExternalEntry(state.externalEntry);await refreshAgent();}));
$("save-external-preferences").addEventListener("click",()=>task(async()=>{
  renderExternalAgents(await api("/api/external/preferences",{fixed_id:$("external-fixed").value||null,priority:state.externalPriority||[],allow_fallback:$("external-fallback").checked}));await refreshAgent();
}));
api("/api/external/agents").then(renderExternalAgents).catch(()=>{$("external-agent-message").textContent="暂时无法读取连接设置，可稍后检测。";});
