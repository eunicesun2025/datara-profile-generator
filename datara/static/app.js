'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const json = v => JSON.stringify(v, null, 2);
const state = {profile:null, profiles:[], meta:null, settings:null, view:'fields', tableId:null,
  dirty:false, sample:null, page:1, artifact:'mapping', preview:null, filter:'all', job:null,
  history:[], importData:null, importNotes:[], suggestions:[], busy:false,
  references:[], analysis:null, samplesCollapsed:false, collapsedPanels:new Set()};
let previewTimer, toastTimer, pollTimer, dragId, editFieldId;

async function api(path, body, options = {}) {
  const response = await fetch('/api' + path, {method:body === undefined ? 'GET':'POST',
    headers:body instanceof FormData ? {} : {'Content-Type':'application/json'},
    body:body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body), ...options});
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try { const e = await response.json(); message = typeof e.detail === 'string' ? e.detail : json(e.detail); } catch {}
    throw Error(message);
  }
  return options.blob ? response.blob() : response.json();
}
function toast(message) { $('#toast').textContent = message; $('#toast').classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 5500); }
function currentTable() { return state.profile?.tables.find(t => t.id === state.tableId) || state.profile?.tables[0]; }
function isSystem(f) { return Object.hasOwn(state.meta.system, f.name); }
function dirty() { state.dirty = true; state.preview = null; updateSaveState(); clearTimeout(previewTimer); previewTimer = setTimeout(refreshPreview, 600); }
function updateSaveState() { const el = $('#save-state'); if(el) el.textContent = state.dirty ? '有未保存的修改' : state.profile?.revision ? `已保存 · v${state.profile.revision}` : '新草稿'; }
function confirmLeave() { return !state.dirty || window.confirm('当前修改尚未保存。继续将离开这份草稿，是否继续？'); }
function fieldCount(source) { return state.profile.tables.reduce((n,t) => n + t.fields.filter(f => !source || f.source === source).length,0); }
function issuesHTML(issues) {
  if(!issues) return '';
  return ['errors','warnings'].filter(k => issues[k]?.length).map(k => `<div class="notice ${k === 'errors' ? 'error':'warning'}" style="margin-bottom:12px"><strong>${k === 'errors' ? '请修正后生成':'请留意'}</strong><ul>${issues[k].slice(0,15).map(e=>`<li>${esc(e)}</li>`).join('')}</ul>${issues[k].length>15?`<p class="no-margin">另有 ${issues[k].length-15} 项，请继续审核字段。</p>`:''}</div>`).join('');
}
function routeUrl(id,view='fields') {return id ? '#profile/'+id+'/'+view : '#home';}
function pushRoute() {const url=routeUrl(state.profile?.id,state.view);if(location.hash!==url)window.history.pushState(null,'',url);}
async function loadProfile(p, view='fields', navigate=true) {
  p.reference_ids ||= [];
  if(!p.revision)p=await api('/profiles/save',p);
  clearTimeout(pollTimer); state.job=null; state.profile=p; state.tableId=p.tables[0]?.id; state.view=view; state.filter='all';
  state.dirty=false; state.preview=null; state.sample=null; state.importNotes=[]; state.history=[];
  state.analysis=null; state.references=[];
  for(const id of p.reference_ids) {try{state.references.push(await api('/references/'+id));}catch(e){toast(e.message);}}
  if(p.sample_ids?.length) { try {state.sample=await api('/samples/'+p.sample_ids.at(-1)); state.page=1;} catch(e){toast(e.message);} }
  if(p.revision) state.history=await api('/profiles/'+p.id+'/tests');
  if(navigate)pushRoute();render(); refreshPreview();
}
async function home() {
  if(!confirmLeave()) return;
  clearTimeout(pollTimer); state.job=null; state.profile=null; state.dirty=false;
  state.profiles=await api('/profiles'); pushRoute();render();
}
async function restoreRoute() {
  const parts=location.hash.slice(1).split('/');
  if(parts[0]==='profile'&&parts[1]) {
    const view=['fields','preview','test'].includes(parts[2])?parts[2]:'fields';
    if(state.profile?.id===parts[1]){state.view=view;render();await refreshPreview();}
    else await loadProfile(await api('/profiles/'+parts[1]),view,false);
  } else {clearTimeout(pollTimer);state.profile=null;state.dirty=false;state.profiles=await api('/profiles');render();}
}
function newDialog() {
  modal('新建空白 Profile', `<p class="helper">已有 Field Mapping？请取消并使用首页的「导入已有 Field Mapping」。</p><div class="form-group"><label class="form-label" for="new-name">Profile 名称</label><input id="new-name" value="新建 Profile"></div><div class="form-group"><label class="form-label" for="new-table">主表名称</label><input id="new-table" value="AI_Document"><p class="helper">英文、数字或下划线，以字母或下划线开头；创建后仍可修改。</p></div>`, '<button class="primary" data-action="create-profile">创建并打开</button>');
}
function validTableName(name, excludeId=null) {
  if(!/^[A-Za-z_][A-Za-z0-9_]{0,99}$/.test(name)) throw Error('表名请使用英文、数字和下划线，不能以数字开头，最多 100 个字符');
  if(state.profile?.tables.some(t=>t.id!==excludeId&&t.name.toLowerCase()===name.toLowerCase())) throw Error('表名已存在，请使用不同名称');
}
function render() {
  $('#breadcrumb').innerHTML=state.profile ? `我的 Profiles <span>/</span> ${esc(state.profile.name)}` : '工作空间 <span>/</span> 我的 Profiles';
  $('#connection-status').textContent=state.settings?.has_key ? `● ${state.settings.model || '模型已配置'}` : '○ 配置视觉模型';
  if(!state.profile) {renderHome(); return;}
  const p=state.profile;
  $('#main').innerHTML=`<button class="small ghost" data-action="home">← 返回我的 Profiles</button><div class="page-heading"><div><div class="eyebrow">PROFILE WORKSPACE</div><h1>${esc(p.name)}</h1><p class="subtitle">上传样张与资料，让 AI 完善定义；审核后生成 Mapping、SQL 和提取提示词。</p></div>
    <div class="actions"><span class="save-state" id="save-state"></span><button data-action="save">保存草稿</button><button class="primary" data-action="export">↓ 导出 ZIP</button></div></div>
    <nav class="steps" aria-label="工作流程">${[['fields','字段定义'],['preview','输出预览'],['test','测试提取']].map(([v,label],i)=>`<button class="step ${state.view===v?'active':''}" data-action="view" data-view="${v}"><b>${i+1}</b>${label}</button>`).join('')}</nav>
    ${state.job?.status==='running'?`<div class="notice job-banner"><span><span class="spinner"></span> 正在${state.job.kind==='draft'?'分析文档并建议字段（视觉模型通常需要 1–2 分钟）':'读取样本并提取数据'}…</span><button class="small" data-action="cancel-job">取消请求</button></div>`:''}
    <div id="workspace-view"></div>`;
  updateSaveState();
  if(state.view==='fields') renderFields();
  else if(state.view==='preview') renderPreview();
  else renderTest();
  installPanelControls();
}

function installPanelControls() {
  $$('.panel-toggle').forEach(button=>button.remove());
  $$('.panel-head').forEach(head=>{
    const panel=head.closest('.panel'); if(!panel)return;
    const key=$('h3',head)?.textContent; if(!key)return;
    panel.classList.toggle('is-collapsed',state.collapsedPanels.has(key));
    const button=document.createElement('button');button.className='small ghost panel-toggle';
    button.dataset.action='toggle-panel';button.dataset.panel=key;
    button.textContent=state.collapsedPanels.has(key)?'展开 ＋':'收起 −';button.setAttribute('aria-expanded',String(!state.collapsedPanels.has(key)));
    head.append(button);
  });
  document.body.classList.toggle('samples-collapsed',state.samplesCollapsed);
}

function referencePanel() {
  return `<section class="panel"><div class="panel-head"><h3>参考文件</h3><button class="small ghost" data-action="upload-reference">＋ 上传</button></div><div class="panel-content reference-drop" data-drop="reference">
    <p class="helper">拖入字段说明、旧提示词或业务规则。支持 XLSX / DOCX / TXT / MD / JSON / CSV / SQL，每份 8MB。</p>
    ${state.references.map(r=>`<div class="reference-file"><span>${esc(r.name)}<small>${r.characters.toLocaleString()} 字符</small></span><button class="icon-button" data-action="remove-reference" data-id="${esc(r.id)}" aria-label="移除 ${esc(r.name)}">×</button></div>`).join('')}
    <p class="helper">仅在“AI 分析样张与文件”时发送当前样张和这些资料。Word 只读取正文和表格文字，内嵌图片请另上传为样张。</p></div></section>`;
}
function renderHome() {
  $('#main').innerHTML=`<div class="page-heading"><div><div class="eyebrow">DATARA / WORKSPACE</div><h1>我的 Profiles</h1><p class="subtitle">从一份样本开始，生成完整的文档处理配置。</p></div></div>
    <section class="hero"><div><div class="eyebrow">ONE DEFINITION. ALL OUTPUTS.</div><h2>把手动维护的文件，<br>变成一份统一定义。</h2><p>已有字段清单：导入已有 Field Mapping。没有字段清单：新建空白 Profile。两种方式都会进入同一个编辑器。</p><div class="actions"><button class="primary" data-action="new">新建空白 Profile</button><button class="ghost" data-action="import">导入已有 Field Mapping</button></div></div>
    <div><div class="flow-card"><div class="flow-row"><span class="flow-num">01</span><span>样本 PDF / 图片 + 字段清单</span></div></div><div class="flow-arrow">↓</div><div class="flow-card"><div class="flow-row"><span class="flow-num">02</span><span>审核一份 Profile 定义</span></div></div><div class="flow-arrow">↓</div><div class="flow-card"><div class="flow-row"><span class="flow-num">03</span><span>生成一致的配置文件</span></div><div class="outputs"><span>XLSX</span><span>SQL</span><span>PROMPT</span><span>JSON</span></div></div></div></section>
    <div class="section-heading"><h2>已保存的草稿 <span class="count">${state.profiles.length} 个</span></h2><button class="small ghost" data-action="import">↑ 导入 Excel</button></div>
    ${state.profiles.length?`<div class="profile-grid">${state.profiles.map(p=>`<button class="profile-card" data-action="open" data-id="${esc(p.id)}"><span class="file-mark">PROFILE</span><h3>${esc(p.name)}</h3><p>${p.table_count} 张表 · ${p.field_count} 个字段</p><div class="card-bottom"><span>${esc(p.updated_at?new Date(p.updated_at).toLocaleString('zh-CN'): '尚未保存')}</span><span>v${p.revision} ↗</span></div></button>`).join('')}</div>`:
    `<div class="empty"><h3>这里将保存你的 Profile</h3><p class="helper">可以新建一份，或先打开支票 / 供应商发票示例体验编辑与导出。</p><div class="actions" style="justify-content:center;margin-top:18px"><button data-action="demo" data-kind="cheque">打开支票示例</button><button data-action="demo" data-kind="invoice">打开发票示例</button></div></div>`}`;
}
function samplePanel() {
  return `<section class="panel sample-panel"><div class="panel-head"><h3>样本单据</h3><button class="small ghost" data-action="upload">${state.sample?'更换':'上传'}</button></div><div class="panel-content" data-drop="sample">${state.sample ?
    `<img class="sample-image" src="/api/samples/${esc(state.sample.id)}/pages/${state.page}" alt="样本第 ${state.page} 页" data-action="zoom"><p class="sample-caption">${esc(state.sample.name)}</p><div class="page-controls"><button class="small ghost" data-action="page-prev" ${state.page===1?'disabled':''}>←</button><span>${state.page} / ${state.sample.pages} 页</span><button class="small ghost" data-action="page-next" ${state.page===state.sample.pages?'disabled':''}>→</button></div>`:
    `<div class="sample-drop" data-action="upload" role="button" tabindex="0"><div class="upload-icon">⇧</div><strong>拖拽样张到这里，或点击上传</strong><p>PDF / PNG / JPEG<br>最大 20MB · PDF 最多 15 页</p></div>`}
    <div class="sample-note">样张保存在本机。点击「AI 分析样张与文件」或「测试提取」时，才发送页面图片到配置的模型端点；可拖入新样张进行更换。</div></div></section>`;
}
function renderFields() {
  const p=state.profile,t=currentTable();
  $('#workspace-view').innerHTML=`<div class="editor-layout"><div class="stack sample-column"><button class="small collapse-samples" data-action="toggle-samples" title="收起或展开样张与参考资料">${state.samplesCollapsed?'展开 →':'← 收起样张与资料'}</button>${samplePanel()}${referencePanel()}<div class="panel"><div class="panel-content"><div class="eyebrow">FIELD SOURCES</div><p class="helper"><span class="pill">AI · ${fieldCount('AI')}</span> 从单据提取，进入提示词</p><p class="helper"><span class="pill">Manual · ${fieldCount('Manual')}</span> 人工填写</p><p class="helper"><span class="pill">System · ${fieldCount('System')}</span> Datara 或数据库填充</p></div></div></div>
    <div class="stack"><section class="panel"><div class="panel-head"><h3>Profile 信息</h3><span class="inline-note">修改会同步到输出预览</span></div><div class="panel-content"><div class="profile-meta">
    ${profileInput('name','Profile 名称',p.name)}${profileInput('accepted_documents','接受的单据类型',p.accepted_documents,'例如：供应商发票或收据')}
    </div><details><summary class="details-toggle">更多设置：排除类型、识别规则和数据库</summary><div class="profile-meta">${profileInput('rejected_documents','排除的单据',p.rejected_documents)}${profileInput('description','业务说明（仅用于本地管理）',p.description)}${profileInput('database_schema','数据库 Schema',p.database_schema)}${profileInput('database_name','数据库名称（选填）',p.database_name,'不填则不生成 USE')}</div><div style="margin-top:15px"><label class="form-label" for="document-rules">单据 / 明细识别规则</label><textarea class="w-full" id="document-rules" data-profile="document_rules" placeholder="只填写文档识别规则；字段提取规则在字段详情中编辑。">${esc(p.document_rules)}</textarea><p class="helper">不加入默认值或额外输出要求。无法识别统一返回 null。</p></div></details></div></section>
    ${state.importNotes.length?`<details class="notice" open><summary>导入说明 · ${state.importNotes.length} 项</summary><ul>${state.importNotes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul></details>`:''}
    <section class="panel"><div class="table-tabs">${p.tables.map(t=>`<button class="table-tab ${t.id===state.tableId?'active':''}" data-action="table" data-id="${t.id}"><small>${t.role==='head'?'主':'子'}</small>${esc(t.name)}</button>`).join('')}<button class="small ghost" data-action="add-table" title="添加子表" aria-label="添加子表">＋</button></div>
    <div class="field-toolbar"><div class="actions"><select id="source-filter" aria-label="筛选字段来源">${['all','AI','Manual','System'].map(v=>`<option value="${v}" ${state.filter===v?'selected':''}>${v==='all'?'全部字段':v}</option>`).join('')}</select><button class="small" data-action="review-batch">批量审核</button><button class="small ghost" data-action="edit-table">修改表名</button><button class="small" data-action="suggest-local">补全类型与展示</button></div><div class="actions"><button class="small" data-action="import">导入 Excel</button><button class="small secondary" data-action="draft" ${state.job?.status==='running'?'disabled':''}>✧ AI 分析样张与文件</button><button class="small primary" data-action="add-field">＋ 添加字段</button></div></div>
    <div class="field-scroll"><table class="field-table"><thead><tr><th></th><th>字段名称 / 说明</th><th>来源</th><th>类型</th><th>必填</th><th title="主列表展示顺序，留空表示不展示">HeadDisplay</th><th>操作</th></tr></thead><tbody>${t.fields.filter(f=>state.filter==='all'||f.source===state.filter).map(f=>fieldRow(f)).join('')}</tbody></table></div>
    <div class="field-footer"><span>${t.fields.length} 个字段 · 拖动 ⋮⋮ 调整顺序</span><span>System 字段保留在映射和数据库中</span></div></section><div id="field-issues"></div></div></div>`;
  if(state.preview) $('#field-issues').innerHTML=issuesHTML(state.preview);
  installPanelControls();
}
function profileInput(prop,label,value,placeholder='') {return `<label><span class="form-label">${label}</span><input data-profile="${prop}" value="${esc(value)}" placeholder="${esc(placeholder)}"></label>`;}
function fieldRow(f) {
  const locked=isSystem(f);
  return `<tr data-field-id="${f.id}" class="${locked?'system-row':''}" draggable="false"><td><span class="drag" draggable="true" data-drag-id="${f.id}" title="拖动排序">⋮⋮</span></td><td><input aria-label="字段名称 ${esc(f.name)}" data-field="name" data-id="${f.id}" value="${esc(f.name)}" ${locked?'disabled':''}><input class="desc" aria-label="字段说明 ${esc(f.name)}" data-field="description" data-id="${f.id}" value="${esc(f.description)}" placeholder="添加字段说明">${f.reviewed?"":"<span class=\"pill\">待审核</span>"}</td>
    <td><select class="source-${f.source}" aria-label="字段来源 ${esc(f.name)}" data-field="source" data-id="${f.id}" ${locked?'disabled':''}>${['AI','Manual','System'].map(v=>`<option ${f.source===v?'selected':''}>${v}</option>`).join('')}</select></td>
    <td><select aria-label="字段类型 ${esc(f.name)}" data-field="data_type" data-id="${f.id}" ${locked?'disabled':''}>${Object.keys(state.meta.default_sql).map(v=>`<option ${f.data_type===v?'selected':''}>${v}</option>`).join('')}</select></td>
    <td><input type="checkbox" aria-label="必填 ${esc(f.name)}" data-field="is_required" data-id="${f.id}" ${f.is_required?'checked':''}></td><td><input class="display-order" type="number" min="1" max="999" placeholder="不展示" aria-label="HeadDisplay ${esc(f.name)}" data-field="head_display" data-id="${f.id}" value="${esc(f.head_display)}"></td><td><div class="row-actions"><button class="icon-button" data-action="edit-field" data-id="${f.id}" title="字段详情" aria-label="字段详情 ${esc(f.name)}">⋯</button><button class="icon-button" data-action="delete-field" data-id="${f.id}" ${locked?'disabled':''} aria-label="删除 ${esc(f.name)}">×</button></div></td></tr>`;
}
async function refreshPreview() {
  if(!state.profile) return;
  const snapshot=json(state.profile);
  try {
    const result=await api('/preview',state.profile);
    if(!state.profile||json(state.profile)!==snapshot) return;
    state.preview=result;
    if(state.view==='preview') renderPreview();
    if(state.view==='fields' && $('#field-issues')) $('#field-issues').innerHTML=issuesHTML(result);
    if(state.view==='test') renderTest();
    installPanelControls();
  } catch(e){toast(e.message);}
}
function renderPreview() {
  const pv=state.preview;
  $('#workspace-view').innerHTML=`<div class="section-heading"><div><h2>同一份定义，四份输出</h2><p class="helper">下载内容与预览一致。${state.history.some(r=>r.kind==='extract'&&r.fingerprint===pv?.fingerprint&&r.status==='completed')?'当前定义已有测试记录。':'当前定义尚未完成测试提取。'}</p></div><button data-action="refresh">↻ 更新预览</button></div>${pv?issuesHTML(pv):'<p class="helper">正在生成预览…</p>'}
    ${pv?.sql?`<div class="artifact-tabs">${[['mapping','Field Mapping · XLSX'],['sql','建表脚本 · SQL'],['prompt','提取提示词 · TXT'],['structure','输出结构 · JSON']].map(([id,label])=>`<button class="small ${state.artifact===id?'active':''}" data-action="artifact" data-id="${id}">${label}</button>`).join('')}</div><section class="panel">${state.artifact==='mapping'?`<div class="field-scroll"><table class="preview-table"><thead><tr>${pv.columns.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${pv.rows.map(r=>`<tr>${r.map(c=>`<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`:`<div class="panel-head"><h3>${state.artifact==='structure'?'结构示例；真实无明细时返回 []':state.artifact==='sql'?'SQL Server 建表脚本':'中文提取提示词'}</h3><button class="small ghost" data-action="copy">复制内容</button></div><pre>${esc(state.artifact==='structure'?json(pv.structure):pv[state.artifact])}</pre>`}</section>`:''}`;
  installPanelControls();
}
function renderTest() {
  const r=state.job?.kind==='extract'?state.job:state.history.find(r=>r.kind==='extract');
  const stale=r && (!state.preview || r.fingerprint!==state.preview.fingerprint || r.sample_id!==state.sample?.id);
  $('#workspace-view').innerHTML=`<div class="test-layout"><div class="stack">${samplePanel()}<section class="panel"><div class="panel-content"><h3>模型连接</h3><p class="helper">${esc(state.settings?.model||'尚未配置')}<br>${state.settings?.has_key?'API Key 已配置':'请先填写模型地址和 API Key'}</p><button class="small w-full" data-action="settings" style="margin-top:13px">模型设置</button><button class="primary w-full" data-action="extract" style="margin-top:10px" ${state.job?.status==='running'?'disabled':''}>开始测试提取</button><p class="helper">使用当前字段与提示词。结果不会改写你的字段定义。</p></div></section></div>
    <div class="stack"><div class="stat-grid"><div class="stat"><strong>${fieldCount('AI')}</strong><small>AI 提取字段</small></div><div class="stat"><strong>${state.profile.tables.length}</strong><small>数据表</small></div><div class="stat"><strong>${state.sample?.pages||'—'}</strong><small>样本页数</small></div></div>
    ${stale?'<div class="notice warning">这份结果对应先前的字段定义或样本。请重新测试当前版本。</div>':''}
    ${r?.error?`<div class="notice error">${esc(r.error)}</div>`:''}
    ${r?.validation?issuesHTML(r.validation):''}
    <section class="panel"><div class="panel-head"><h3>提取结果</h3>${r?`<span class="result-status">${r.status==='running'?'处理中…':r.validation?.status==='not_matched'?'文档不匹配':r.validation?.status==='valid'?'结构校验通过':r.validation?.status==='invalid'?'结果需要修正':esc(r.status)}</span>`:'<span class="inline-note">尚未测试</span>'}</div>
    ${r?.raw?`<pre>${esc(r.result?json(r.result):r.raw)}</pre><div class="panel-content"><p class="helper">${esc(r.model)} · ${esc(new Date(r.started_at).toLocaleString('zh-CN'))}<br>结构校验通过不代表值一定正确，请对照样本确认。</p></div>`:`<div class="test-empty"><div class="upload-icon">⌘</div><h3>让样本验证你的定义</h3><p>配置视觉模型后运行提取。这里会展示原始结果，以及缺失键、类型和日期格式检查。</p><button class="small" data-action="manual-result">粘贴已有 JSON 进行校验</button></div>`}</section>
    ${r?.raw?'<button class="small ghost" data-action="manual-result">粘贴其他 JSON 进行校验</button>':''}
    ${state.history.length?`<details><summary class="details-toggle">最近测试记录</summary>${state.history.filter(r=>r.kind==='extract').map(r=>`<button class="small" data-action="history" data-id="${r.id}">${esc(new Date(r.started_at).toLocaleString('zh-CN'))} · ${esc(r.status)}</button>`).join('')}</details>`:''}</div></div>`;
  installPanelControls();
}
function modal(title,body,footer='') {
  $('#modal-body').innerHTML=`<div class="modal-head"><h2>${esc(title)}</h2><button class="icon-button" data-action="close" aria-label="关闭">×</button></div><div class="modal-content">${body}</div><div class="modal-footer"><button data-action="close">取消</button>${footer}</div>`;
  if(!$('#modal').open) $('#modal').showModal();
}
function closeModal(){ $('#modal').close(); }
function fieldDialog(id) {
  editFieldId=id; const f=currentTable().fields.find(f=>f.id===id), locked=isSystem(f);
  modal('字段详情 · '+f.name,`<div class="form-grid"><div class="form-group"><label class="form-label" for="f-name">字段名</label><input id="f-name" value="${esc(f.name)}" ${locked?'disabled':''}></div><div class="form-group"><label class="form-label" for="f-display">主列表展示次序</label><input id="f-display" type="number" min="1" value="${esc(f.head_display)}" placeholder="留空不在主列表显示"></div></div>
    <div class="form-group"><label class="form-label" for="f-desc">中文说明</label><input id="f-desc" value="${esc(f.description)}"></div>
    ${f.data_type==='Choice'?`<div class="form-group"><label class="form-label" for="f-choices">选项（英文逗号分隔）</label><input id="f-choices" value="${esc(f.choice_values.join(','))}" placeholder="Y,N"></div>`:''}
    <div class="form-group"><label class="form-label" for="f-sample">样本值（仅用于 mapping，不作为提示词答案）</label><input id="f-sample" value="${esc(f.sample_data)}"></div>
    <div class="form-group"><label class="form-label" for="f-rule">${f.source==='AI'?'提取规则':'填充说明'}</label><textarea id="f-rule" placeholder="${f.source==='AI'?'说明如何定位和区分这个字段；缺失值统一 null。':'例如：由 Datara 根据公司名称查询公司代码。'}">${esc(f.source==='AI'?f.extraction:f.population)}</textarea></div>
    <div class="form-group"><label class="form-label" for="f-sql">SQL 存储类型</label><input id="f-sql" value="${esc(f.sql_type||'')}" placeholder="${esc(state.meta.default_sql[f.data_type])}" ${locked?'disabled':''}><p class="helper">留空使用默认值。支持 int、bigint、bit、date、datetime、nvarchar(n/max)、varchar(n/max)、decimal(p,s) 等白名单类型。业务字段始终允许 NULL。</p></div>
    <label class="checkbox-label"><input id="f-reviewed" type="checkbox" ${f.reviewed?'checked':''}> 已审核此字段</label>`, '<button class="primary" data-action="apply-field">保存字段</button>');
}
async function settingsDialog() {
  state.settings=await api('/settings'); const s=state.settings;
  modal('视觉模型设置',`<p class="helper" style="margin:0 0 20px">支持 OpenAI-compatible 视觉端点。模型 ID 请使用供应商实际提供的名称。</p>
    <div class="form-group"><label class="form-label" for="s-url">API Base URL</label><input id="s-url" placeholder="https://your-provider.example/v1" value="${esc(s.base_url)}"><p class="helper">填写以 /v1 等结尾的基础地址，程序会追加 /chat/completions。</p></div>
    <div class="form-group"><label class="form-label" for="s-key">API Key</label><input id="s-key" type="password" autocomplete="off" placeholder="${s.has_key?'已配置；留空保留当前密钥':'输入 API Key'}"><p class="helper">密钥只保留在本次服务运行中，不写入草稿或导出文件。重启后重新填写，或使用环境变量。</p></div>
    <div class="form-group"><label class="form-label" for="s-model">模型 ID</label><input id="s-model" value="${esc(s.model)}" placeholder="填写 Qwen / Kimi 或公司视觉模型的准确 ID"></div>
    <div class="form-grid"><div class="form-group"><label class="form-label" for="s-timeout">超时（秒）</label><input id="s-timeout" type="number" min="10" max="1800" value="${s.timeout}"><p class="helper">允许 10–1800 秒；图片和多份参考资料建议 600 秒。</p></div><div class="form-group"><label class="form-label" for="s-tokens">最大输出长度（tokens）</label><input id="s-tokens" type="number" min="256" max="32768" value="${s.max_tokens}"></div></div><div id="connection-test-result" class="helper"></div>`,
    `${s.has_key?'<button class="danger small" data-action="clear-key">清除密钥</button>':''}<button data-action="test-connection">测试连接</button><button class="primary" data-action="save-settings">保存设置</button>`);
}
async function persistSettings() {
  state.settings=await api('/settings',{base_url:$('#s-url').value.trim(),model:$('#s-model').value.trim(),api_key:$('#s-key').value||null,timeout:Number($('#s-timeout').value),max_tokens:Number($('#s-tokens').value)});
  $('#s-key').value=''; $('#connection-status').textContent=`${state.settings.has_key?'●':'○'} ${state.settings.model}`;
}
async function save() {
  if(!state.profile) return;
  const snapshot=json(state.profile),identity=state.profile.id;
  const saved=await api('/profiles/save',JSON.parse(snapshot));
  if(state.profile?.id!==identity)return;
  if(json(state.profile)===snapshot){state.profile=saved;state.dirty=false;}
  else {state.profile.revision=saved.revision;state.profile.updated_at=saved.updated_at;state.dirty=true;}
  updateSaveState();toast(state.dirty?'较早修改已保存，刚才的新修改仍待保存':'草稿已保存 · v'+saved.revision); await refreshPreview();
}
async function exportProfile() {
  if(state.dirty||!state.profile.revision) await save();
  if(state.dirty)throw Error('保存期间有新修改，请保存后重新导出');
  await refreshPreview();
  if(state.preview.errors.length){state.view='preview';render();throw Error('请先修正预览中列出的字段问题');}
  const blob=await api('/export',state.profile,{blob:true});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=state.profile.name.replace(/[^\p{L}\p{N}_-]/gu,'_')+'_v'+state.profile.revision+'.zip';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);toast('已导出同一版本的四份配置文件');
}
function importDialog(){
  const d=state.importData;
  modal('导入 Excel 字段',`<div class="form-group"><label class="form-label" for="i-sheet">选择工作表（第一行为列名）</label><select id="i-sheet">${d.sheets.map(s=>`<option>${esc(s.name)}</option>`).join('')}</select></div><div id="import-columns"></div><p class="helper">导入会创建一份新的草稿，不修改原始 Excel。当前草稿的样本不会自动复制。</p>`, '<button class="primary" data-action="apply-import">导入为新草稿</button>');
  importColumns();
}
function importColumns(){
  const sheet=state.importData.sheets.find(s=>s.name===$('#i-sheet').value);
  const full=['TableName','TableLevel','ColumnName','Source','DataType'].every(c=>sheet.headers.includes(c));
  $('#import-columns').innerHTML=full?`<div class="notice">已识别 Datara Field Mapping。原始 Excel 不会修改。</div>${sheet.import_error?`<div class="notice error">${esc(sheet.import_error)}</div>`:''}${sheet.repair_notes?.length?`<div class="notice warning"><strong>导入前检查</strong><ul>${sheet.repair_notes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul><label><input id="i-repair" type="checkbox" checked> 使用上述结构修复（字段归属将在草稿中标为待审核）</label></div>`:''}`:
    `<div class="notice" style="margin-bottom:15px">普通字段清单：请选择列对应关系，来源和类型建议需要人工审核。</div>${[['name','字段名（必选）'],['description','字段说明'],['type','业务类型'],['source','来源']].map(([k,l])=>`<div class="form-group"><label class="form-label" for="i-${k}">${l}</label><select id="i-${k}">${k!=='name'?'<option value="">未提供</option>':''}${sheet.headers.filter(Boolean).map(h=>`<option>${esc(h)}</option>`).join('')}</select></div>`).join('')}`;
}
async function startJob(kind,instructions='') {
  if(!state.sample) throw Error('请先上传一个样本 PDF 或图片');
  if(!state.settings?.has_key){await settingsDialog();toast('请先完成模型连接设置');return;}
  if(state.dirty||!state.profile.revision) await save();
  state.job=await api('/jobs',{profile:state.profile,sample_id:state.sample.id,kind,instructions});
  if(kind==='extract')state.view='test';
  render();
  toast(kind==='draft'?'AI 已开始分析，视觉模型通常需要 1–2 分钟':'AI 已开始提取');
  pollJob(state.job.id);
}
async function pollJob(id) {
  try {
    const r=await api('/jobs/'+id);
    if(state.job?.id!==id)return;
    state.job=r;
    if(r.status==='running'){pollTimer=setTimeout(()=>pollJob(id),1500);return;}
    state.history=[r,...state.history.filter(x=>x.id!==id)].slice(0,10);render();
    if(r.status==='failed'||r.status==='cancelled'){toast(r.error);return;}
    if(r.kind==='draft'&&r.status==='completed') {
      state.suggestions=r.suggestions||[];
      state.analysis=r; analysisDialog();
    }
  }catch(e){toast(e.message);pollTimer=setTimeout(()=>pollJob(id),4000);}
}

function analysisDialog() {
  const r=state.analysis, proposal=r.profile_suggestion||{};
  modal('审核 AI 分析结果',`<p class="helper">分析当前样张和 ${state.references.length} 份参考文件。勾选要应用的建议；现有字段显示修改前后类型。</p>
    ${r.evidence?`<div class="notice">${esc(r.evidence)}</div>`:''}
    ${r.questions?.length?`<div class="notice warning"><strong>需要确认</strong><ul>${r.questions.map(q=>`<li>${esc(q)}</li>`).join('')}</ul></div>`:''}
    ${Object.entries(proposal).map(([k,v])=>`<div class="form-group analysis-profile"><label class="checkbox-label"><input type="checkbox" data-profile-suggestion="${esc(k)}">应用${({accepted_documents:'单据类型建议',rejected_documents:'排除类型建议',document_rules:'详细提取规则'})[k]}</label><textarea id="analysis-${esc(k)}" class="w-full">${esc(v)}</textarea></div>`).join('')}
    <h3>字段建议 · ${state.suggestions.length} 项</h3><label class="checkbox-label"><input type="checkbox" id="select-all-suggestions">选择全部字段建议</label>
    ${state.suggestions.map((s,i)=>{const t=state.profile.tables.find(t=>t.id===s.table_id),old=t?.fields.find(f=>f.id===s.field.id);return `<div class="suggestion"><label class="checkbox-label"><input type="checkbox" data-suggestion="${i}"><strong>${esc(s.field.name)}</strong><span class="pill">${s.action==='update'?'更新':'新增'}</span><span class="pill">${old?esc(old.data_type)+' → ':''}${esc(s.field.data_type)}</span></label><p>${esc(t?.name)} · HeadDisplay: ${s.field.head_display??'不展示'}<br>${esc(s.field.description)}</p><details><summary>查看提取规则与依据</summary><p class="rule-text">${esc(s.field.extraction||'未提供具体规则')}</p><p>样本值：${esc(s.field.sample_data??'未识别')}<br>依据：${esc(s.evidence)}</p></details></div>`;}).join('')}`,
    '<button class="primary" data-action="apply-suggestions">应用勾选的建议</button>');
}

async function uploadSampleFile(file) {
  if(!state.profile)throw Error('请先打开一份 Profile');
  const identity=state.profile.id,data=new FormData();data.append('file',file);toast('正在准备样张页面…');
  const sample=await api('/samples',data);
  if(state.profile?.id!==identity)return;
  state.sample=sample;state.page=1;state.profile.sample_ids=[...new Set([...state.profile.sample_ids,sample.id])];dirty();render();toast('样张已载入，可开始 AI 分析');
}

async function uploadReferences(files) {
  if(!state.profile)throw Error('请先打开一份 Profile');
  const identity=state.profile.id;
  for(const file of files) {
    if(state.profile?.id!==identity)return;
    if(state.profile.reference_ids.length>=10)throw Error('每份 Profile 最多关联 10 份参考文件');
    const data=new FormData();data.append('file',file);
    const reference=await api('/references',data);
    if(state.profile?.id!==identity)return;
    state.references.push(reference);state.profile.reference_ids.push(reference.id);dirty();render();
  }
  toast('参考文件已就绪，点击“AI 分析样张与文件”生成建议');
}

const actions={
  'toggle-sidebar':()=>{document.body.classList.toggle('sidebar-collapsed');localStorage.setItem('datara-sidebar-collapsed',String(document.body.classList.contains('sidebar-collapsed')));},
  'toggle-samples':()=>{state.samplesCollapsed=!state.samplesCollapsed;render();},
  'toggle-panel':el=>{const key=el.dataset.panel;if(state.collapsedPanels.has(key))state.collapsedPanels.delete(key);else state.collapsedPanels.add(key);render();},
  'upload-reference':()=>$('#reference-upload').click(),
  'remove-reference':el=>{state.profile.reference_ids=state.profile.reference_ids.filter(id=>id!==el.dataset.id);state.references=state.references.filter(r=>r.id!==el.dataset.id);dirty();render();},
  'suggest-local':async()=>{const snapshot=json(state.profile);const r=await api('/fields/suggest',state.profile);if(json(state.profile)!==snapshot)throw Error('字段已变化，请重新点击补全');state.profile=r.profile;state.importNotes=r.notes;dirty();render();toast('已按字段含义补全类型；主表未配置展示列时选取前 5 个业务字段，请核对后保存');},
  home,
  new:()=>{if(confirmLeave())newDialog();},
  'create-profile':async()=>{const name=$('#new-name').value.trim(),table=$('#new-table').value.trim();if(!name)throw Error('请填写 Profile 名称');if(!/^[A-Za-z_][A-Za-z0-9_]{0,99}$/.test(table))throw Error('请填写有效的英文主表名称');const p=await api('/profiles/new',{});p.name=name;p.tables[0].name=table;const saved=await api('/profiles/save',p);closeModal();await loadProfile(saved);toast('已创建 Profile；添加字段或上传样本后继续');},
  demo:async el=>{if(confirmLeave())await loadProfile(await api('/demo/'+el.dataset.kind,{}));},
  open:async el=>{if(confirmLeave())await loadProfile(await api('/profiles/'+el.dataset.id));},
  "review-batch":()=>{const fields=currentTable().fields.filter(f=>!f.reviewed&&(state.filter==="all"||f.source===state.filter));modal("批量审核字段", `<p>勾选已经核对的字段。本次仅处理当前表及来源筛选；确认不会消除类型、命名等校验错误。</p>${fields.length?fields.map(f=>`<label class="checkbox-label"><input type="checkbox" data-review-id="${f.id}" checked> ${esc(f.name)} · ${f.source} · ${f.data_type}</label>`).join(""): "<p>当前范围没有待审核字段。</p>"}`, "<button class=\"primary\" data-action=\"apply-review-batch\">确认所选字段</button>");},
  "apply-review-batch":()=>{const ids=new Set($$("[data-review-id]:checked").map(el=>el.dataset.reviewId));currentTable().fields.forEach(f=>{if(ids.has(f.id))f.reviewed=true;});dirty();closeModal();render();toast(`已确认 ${ids.size} 个字段，请保存草稿`);},
  save,export:exportProfile,settings:settingsDialog,close:closeModal,
  view:async el=>{state.view=el.dataset.view;pushRoute();render();await refreshPreview();},
  table:el=>{state.tableId=el.dataset.id;render();},
  artifact:el=>{state.artifact=el.dataset.id;renderPreview();},
  refresh:refreshPreview,
  copy:async()=>{await navigator.clipboard.writeText(state.artifact==='structure'?json(state.preview.structure):state.preview[state.artifact]);toast('已复制');},
  upload:()=>$('#sample-upload').click(),
  import:()=>{if(confirmLeave())$('#excel-upload').click();},
  'page-prev':()=>{state.page=Math.max(1,state.page-1);render();},
  'page-next':()=>{state.page=Math.min(state.sample.pages,state.page+1);render();},
  zoom:()=>modal('样本预览',`<img style="width:100%" src="/api/samples/${state.sample.id}/pages/${state.page}" alt="样本预览">`),
  'add-field':()=>{const t=currentTable();let n=1;while(t.fields.some(f=>f.name==='new_field_'+n))n++;const f={id:crypto.randomUUID(),name:'new_field_'+n,description:'',source:'AI',data_type:'String',is_required:false,choice_values:[],sample_data:null,head_display:null,field_order:t.fields.length+1,sql_type:null,extraction:'',population:'',reviewed:true};t.fields.push(f);state.filter='all';dirty();render();fieldDialog(f.id);},
  'edit-field':el=>fieldDialog(el.dataset.id),
  'delete-field':el=>{const t=currentTable(),f=t.fields.find(f=>f.id===el.dataset.id);if(isSystem(f))return;t.fields=t.fields.filter(f=>f.id!==el.dataset.id);dirty();render();},
  'apply-field':async()=>{const f=currentTable().fields.find(f=>f.id===editFieldId);f.name=$('#f-name').value.trim();f.description=$('#f-desc').value;f.head_display=$('#f-display').value?Number($('#f-display').value):null;f.sql_type=$('#f-sql').value.trim()||null;f.sample_data=$('#f-sample').value||null;if($('#f-choices'))f.choice_values=$('#f-choices').value.replaceAll('，',',').split(',').map(v=>v.trim()).filter(Boolean);f[f.source==='AI'?'extraction':'population']=$('#f-rule').value;f.reviewed=$('#f-reviewed').checked;dirty();const snapshot=json(state.profile),r=await api('/normalize',state.profile);if(json(state.profile)===snapshot)state.profile=r.profile;closeModal();render();if(r.notes.length)toast(r.notes.join('；'));},
  'add-table':()=>modal('添加子表',`<div class="form-group"><label class="form-label" for="t-name">子表名称</label><input id="t-name" value="AI_Document_Detail"></div><p class="helper">通过 head_id 自动关联主表；删除主表记录时级联删除。</p>`, '<button class="primary" data-action="create-table">添加子表</button>'),
  'create-table':async()=>{const t={id:crypto.randomUUID(),name:$('#t-name').value.trim(),role:'detail',parent_table_id:state.profile.tables.find(t=>t.role==='head').id,fields:[]};state.profile.tables.push(t);const r=await api('/normalize',state.profile);state.profile=r.profile;state.tableId=t.id;dirty();closeModal();render();},
  'edit-table':()=>{const t=currentTable();modal('表设置',`<div class="form-group"><label class="form-label" for="t-name">表名</label><input id="t-name" value="${esc(t.name)}"></div><p class="helper">${t.role==='head'?'当前主表':'直接关联主表的子表'}。表名修改会同步到所有输出。</p>`,`${t.role==='detail'?'<button class="danger" data-action="delete-table">删除子表</button>':''}<button class="primary" data-action="apply-table">保存表名</button>`);},
  'apply-table':()=>{const name=$('#t-name').value.trim();validTableName(name,currentTable().id);currentTable().name=name;dirty();closeModal();render();toast('表名已修改；保存草稿后会保留，所有输出同步更新');},
  'delete-table':()=>{if(!confirm('删除此子表及其全部字段？'))return;state.profile.tables=state.profile.tables.filter(t=>t.id!==state.tableId);state.tableId=state.profile.tables[0].id;dirty();closeModal();render();},
  'apply-import':async()=>{const cols={};for(const k of ['name','description','type','source'])if($('#i-'+k)?.value)cols[k]=$('#i-'+k).value;const r=await api('/import/apply',{import_id:state.importData.import_id,sheet:$('#i-sheet').value,columns:Object.keys(cols).length?cols:null,repair_structure:!!$('#i-repair')?.checked});r.profile.name=state.importData.filename?.replace(/\.xlsx$/i,'')||'导入的 Profile';const saved=await api('/profiles/save',r.profile);closeModal();await loadProfile(saved);state.importNotes=r.notes;render();toast('已导入并保存新 Profile；请审核导入说明和字段规则');},
  'save-settings':async()=>{await persistSettings();closeModal();render();toast('模型设置已保存，密钥仅保留在本次服务运行中');},
  'test-connection':async()=>{await persistSettings();$('#connection-test-result').textContent='正在测试连接…';try{const r=await api('/settings/test',{});$('#connection-test-result').textContent=r.message;}catch(e){$('#connection-test-result').textContent=e.message;}},
  'clear-key':async()=>{state.settings=await api('/settings',{...Object.fromEntries(Object.entries(state.settings).filter(([k])=>k!=='has_key')),clear_key:true});await settingsDialog();},
  draft:()=>{if(!state.sample)throw Error('请先上传或拖入样张');modal('AI 分析样张与文件',`<div class="notice">当前样张：${esc(state.sample.name)} · ${state.sample.pages} 页<br>参考文件：${state.references.length?state.references.map(r=>esc(r.name)).join('、'):'尚未上传，可先在左侧添加'}<br>AI 将建议单据类型、字段类型、HeadDisplay、具体定位与提取规则，并检查已有 AI 字段。</div><div class="form-group"><label class="form-label" for="draft-request">补充需求（可选）</label><textarea id="draft-request" placeholder="例如：区分付款方与收款方，识别日期、币种、金额；请参考上传的字段说明完善提取规则。"></textarea><p class="helper">本次发送上方列出的样张及参考文件。分析完成后可逐项选择应用。需要明细字段时请先创建对应子表。</p></div>`, '<button class="primary" data-action="run-draft">开始分析</button>');},
  'run-draft':async()=>{const v=$('#draft-request').value;closeModal();await startJob('draft',v);},
  extract:()=>startJob('extract'),
  'cancel-job':async()=>{await api('/jobs/'+state.job.id+'/cancel',{});await pollJob(state.job.id);},
  'apply-suggestions':async()=>{
    const chosen=$$('[data-suggestion]:checked').map(el=>Number(el.dataset.suggestion));
    const updates=Object.fromEntries($$('[data-profile-suggestion]:checked').map(el=>[el.dataset.profileSuggestion,$('#analysis-'+el.dataset.profileSuggestion).value]));
    if(!chosen.length&&!Object.keys(updates).length)throw Error('请先勾选要应用的建议');
    const snapshot=json(state.profile),pv=await api('/preview',state.profile);
    if(json(state.profile)!==snapshot||pv.fingerprint!==state.analysis.fingerprint)throw Error('分析后字段定义已改变，请重新分析，避免覆盖新修改');
    let n=0;
    for(const i of chosen){const s=state.suggestions[i],t=state.profile.tables.find(t=>t.id===s.table_id);if(!t)continue;const index=t.fields.findIndex(f=>f.id===s.field.id);if(s.action==='update'&&index>=0)t.fields[index]=s.field;else if(!t.fields.some(f=>f.name===s.field.name))t.fields.push(s.field);n++;}
    Object.assign(state.profile,updates);dirty();closeModal();state.view='fields';state.filter='all';render();toast(`已应用 ${n} 项字段建议和 ${Object.keys(updates).length} 项单据规则；请预览并保存`);
  },
  history:el=>{state.job=state.history.find(r=>r.id===el.dataset.id);renderTest();},
  'manual-result':()=>modal('校验已有 JSON',`<div class="form-group"><label class="form-label" for="manual-json">粘贴提取结果</label><textarea id="manual-json" style="min-height:230px;font-family:monospace" placeholder='{"AI_Document": {}}'></textarea></div><div id="manual-validation"></div>`, '<button class="primary" data-action="validate-json">校验结构</button>'),
  'validate-json':async()=>{try{const r=await api('/results/validate',{profile:state.profile,raw:$('#manual-json').value});$('#manual-validation').innerHTML=issuesHTML(r)+(!r.errors.length?`<div class="notice">${r.status==='not_matched'?'文档不匹配 {}':'JSON 结构有效；请对照文档确认值。'}</div>`:'');}catch(e){$('#manual-validation').innerHTML=`<div class="notice error">${esc(e.message)}</div>`;}},
};
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]'); if(!el||el.disabled)return;
  const action=actions[el.dataset.action];if(!action)return;
  const hadDisabled=el.hasAttribute('disabled');el.disabled=true;
  try{await action(el);}catch(e){toast(e.message);}finally{if(!hadDisabled)el.disabled=false;}
});
document.addEventListener('input',event=>{
  const el=event.target;
  if(el.dataset.profile){state.profile[el.dataset.profile]=el.value;dirty();}
  if(el.dataset.field&&el.tagName==='INPUT'){
    const f=currentTable().fields.find(f=>f.id===el.dataset.id);if(!f)return;
    f[el.dataset.field]=el.type==='checkbox'?el.checked:el.type==='number'?(el.value?Number(el.value):null):el.value;dirty();
  }
});
document.addEventListener('change',event=>{
  const el=event.target;
  if(el.dataset.field&&el.tagName==='SELECT'){
    const f=currentTable().fields.find(f=>f.id===el.dataset.id);f[el.dataset.field]=el.value;
    dirty();render();
  }
  if(el.id==='source-filter'){state.filter=el.value;renderFields();}
  if(el.id==='i-sheet')importColumns();
  if(el.id==='select-all-suggestions')$$('[data-suggestion]').forEach(box=>{box.checked=el.checked;});
});
document.addEventListener('dragstart',event=>{const el=event.target.closest('[data-drag-id]');if(el){dragId=el.dataset.dragId;event.dataTransfer.setData('text/plain',dragId);event.dataTransfer.effectAllowed='move';}});
document.addEventListener('dragover',event=>{if(event.target.closest('[data-field-id]'))event.preventDefault();});
document.addEventListener('drop',event=>{const row=event.target.closest('[data-field-id]');if(!row||!dragId)return;event.preventDefault();const fs=currentTable().fields,from=fs.findIndex(f=>f.id===dragId),to=fs.findIndex(f=>f.id===row.dataset.fieldId);if(from>=0&&to>=0&&from!==to){const [f]=fs.splice(from,1);fs.splice(to,0,f);fs.forEach((f,i)=>f.field_order=i+1);dirty();renderFields();}dragId=null;});
document.addEventListener('keydown',event=>{if(event.key==='Enter'&&event.target.matches('.sample-drop'))$('#sample-upload').click();});
$('#sample-upload').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;
  try{await uploadSampleFile(file);}catch(e){toast(e.message);}finally{event.target.value='';}
});
$('#reference-upload').addEventListener('change',async event=>{try{await uploadReferences([...event.target.files]);}catch(e){toast(e.message);}finally{event.target.value='';}});
document.addEventListener('dragover',event=>{if(!event.dataTransfer?.types.includes('Files'))return;event.preventDefault();const zone=event.target.closest('[data-drop]');if(zone)zone.classList.add('drag-active');});
document.addEventListener('dragleave',event=>{const zone=event.target.closest('[data-drop]');if(zone&&!zone.contains(event.relatedTarget))zone.classList.remove('drag-active');});
document.addEventListener('drop',async event=>{if(!event.dataTransfer?.files.length)return;event.preventDefault();$$('.drag-active').forEach(el=>el.classList.remove('drag-active'));const zone=event.target.closest('[data-drop]');if(!zone)return;try{const files=[...event.dataTransfer.files];if(zone.dataset.drop==='reference')await uploadReferences(files);else {if(files.length!==1)throw Error('每次请拖入一份样张；多页请合并为 PDF');await uploadSampleFile(files[0]);}}catch(e){toast(e.message);}});
document.addEventListener('focusout',async event=>{if(event.target.dataset.field!=='name')return;const snapshot=json(state.profile);try{const r=await api('/normalize',state.profile);if(!state.profile||json(state.profile)!==snapshot)return;if(r.notes.length){state.profile=r.profile;dirty();render();toast(r.notes.join('；'));}}catch(e){toast(e.message);}});
document.body.classList.toggle('sidebar-collapsed',localStorage.getItem('datara-sidebar-collapsed')==='true');
$('#excel-upload').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;const data=new FormData();data.append('file',file);
  try{state.importData=await api('/import/inspect',data);state.importData.filename=file.name;importDialog();}catch(e){toast(e.message);}finally{event.target.value='';}
});
window.addEventListener('beforeunload',event=>{if(state.dirty){event.preventDefault();event.returnValue='';}});
window.addEventListener('popstate',async()=>{try{if(state.dirty)await save();await restoreRoute();}catch(e){pushRoute();toast(e.message);}});
(async()=>{try{[state.meta,state.settings,state.profiles]=await Promise.all([api('/meta'),api('/settings'),api('/profiles')]);if(!location.hash)window.history.replaceState(null,'','#home');await restoreRoute();}catch(e){$('#main').innerHTML=`<div class="notice error">页面未能打开：${esc(e.message)} <button data-action="home">返回我的 Profiles</button></div>`;}})();
