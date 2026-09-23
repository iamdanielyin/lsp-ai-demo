'use strict';
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons = {
  chat: '<svg viewBox="0 0 24 24"><path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-9l-5 3v-3a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z"/><path d="M7 9h10M7 13h7"/></svg>',
  settings: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2" fill="currentColor"/><circle cx="16" cy="12" r="2" fill="currentColor"/><circle cx="8" cy="18" r="2" fill="currentColor"/></svg>'
};
const labels = {inbound:'入站事件',history:'原会话历史',manual_text:'手动文本',image:'图片',video:'视频',file:'附件',ai_text:'AI 自动文本',ai_media:'AI 自动媒体',omni_visible:'工作台显示',ticket:'Ticket',quote_bubble:'引用气泡',openai:'OpenAI',platform_read:'平台读取'};
const states = {not_tested:'未测试',passed:'通过',failed:'失败',blocked:'受阻',unsupported:'不支持',queued:'排队',generating:'处理中',pending:'待发送',sending:'发送中',accepted:'平台已受理',delivered:'回执确认送达',unknown:'结果不明',cancelled:'已取消',completed:'已完成',paused:'已暂停',pending_upload:'待上传',scanning:'扫描中',sendable:'可发送',disabled:'已停用'};
const roleNames = {customer:'客户',agent:'人工坐席',ai:'AI 助理',system:'系统',private:'私有备注'};
const fmtTime = v => v ? new Date(typeof v === 'number' ? v * 1000 : v).toLocaleString('zh-CN', {hour12:false}) : '—';
const bytes = n => n ? (n / 1000000).toFixed(2) + ' MB' : '大小未知';
const pill = (state, text) => `<span class="pill ${['passed','sendable','completed','accepted'].includes(state)?'good':['failed','unknown'].includes(state)?'bad':['blocked','scanning','paused'].includes(state)?'warn':''}"><span class="dot"></span>${esc(text || states[state] || state)}</span>`;
const S = {csrf:'', settings:null, assets:[], conversations:[], selected:Number(new URLSearchParams(location.search).get('conversation'))||null, detail:null, drafts:[], limit:50, page:location.pathname === '/settings' ? 'settings' : 'conversations', poll:null, dirty:false};
let toastTimer;
function toast(message, error=false) { const node=$('#toast');node.textContent=message;node.className='show'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>node.className='',6500); }
async function api(path, options={}) {
  const headers={'X-CSRF-Token':S.csrf,...options.headers};
  if(options.body && !(options.body instanceof FormData)) { headers['Content-Type']='application/json'; options.body=JSON.stringify(options.body); }
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),195000);
  try {
    const response=await fetch(path,{...options,headers,signal:controller.signal,credentials:'same-origin'});
    const data=await response.json();
    if(!response.ok) { if(response.status===401 && data.error==='login_required') {clearInterval(S.poll);await boot();} throw new Error(data.message || '请求失败'); }
    return data;
  } catch(e) { if(e.name==='AbortError')throw new Error('请求超时。外发可能已提交，请查看任务，不要直接重复发送。');throw e; }
  finally {clearTimeout(timer);}
}
function bind(node, event, fn) { if(node)node.addEventListener(event, async e=>{try {await fn(e);} catch(error){toast(error.message,true);}}); }
function buttonAction(node, fn) {bind(node,'click',async e=>{e.preventDefault();node.disabled=true;try{await fn(e);}finally{node.disabled=false;}});}
function modal(title, content, onSubmit, submit='确认', wide=false) {
  const dialog=$('#dialog');if(dialog.open)dialog.close();
  $('#dialog-content').innerHTML=`<form id="dialog-form"><div class="dialog-head"><h2>${esc(title)}</h2><button type="button" id="close-dialog" class="ghost" aria-label="关闭">✕</button></div><div class="dialog-body">${content}</div><p id="dialog-error" class="error-text" role="alert"></p><div class="dialog-actions"><button type="button" id="cancel-dialog">取消</button>${onSubmit?`<button type="submit" class="primary">${esc(submit)}</button>`:''}</div></form>`;
  dialog.style.width=wide?'min(960px,calc(100vw - 32px))':'';
  $('#close-dialog').onclick=$('#cancel-dialog').onclick=()=>dialog.close();
  bind($('#dialog-form'),'submit',async e=>{e.preventDefault();const b=$('button[type=submit]',e.target);b.disabled=true;$('#dialog-error').textContent='';try{await onSubmit(new FormData(e.target));dialog.close();}catch(error){$('#dialog-error').textContent=error.message;}finally{b.disabled=false;}});
  dialog.showModal();
}
async function boot() {
  const auth=await api('/api/auth');S.csrf=auth.csrf;
  if(!auth.authenticated) {login();return;}
  S.settings=await api('/api/settings');S.assets=(await api('/api/assets')).assets;
  shell();await renderPage();
  clearInterval(S.poll);S.poll=setInterval(async()=>{if(!$('#dialog').open){try{if(S.page==='conversations')await refreshConversations();else await refreshDiscovery();}catch(e){toast(e.message,true);}}},3000);
}
function login() {
  $('#app').innerHTML=`<main class="login-page"><section class="login-story"><div class="brand"><img src="/static/favicon.svg" alt="LSP"><div>LSP<span> · AI</span><small>CUSTOMER CONNECTION LAB</small></div></div><div><div class="eyebrow">FRESHDESK OMNI × OPENAI</div><h1>让每一次回复，<br>回到原来的对话。</h1><p>在真实渠道中验证 AI 客服的完整流程。连接会话、读取上下文，再把经过审核的回复送回客户。</p><div class="login-flow"><span>客户原渠道</span>→<span>Omni 会话</span>→<span>AI service</span></div></div><footer>LSP / INTEGRATION DEMO · 2026</footer></section><section class="login-main"><form class="login-form" id="login-form"><div class="eyebrow">ADMIN WORKSPACE</div><h2>进入验证工作台</h2><p class="muted">使用部署时创建的管理员口令登录。<br>所有业务配置均在设置页完成。</p><label for="password">管理员口令</label><input type="password" id="password" name="password" autocomplete="current-password" required placeholder="请输入管理员口令"><p class="error-text" id="login-error" role="alert"></p><button class="primary" type="submit">登录工作台 →</button><p class="login-note">当前实现：Freshchat API 路线<br>租户与渠道能力需通过真实接口逐项验证。</p></form></section></main>`;
  bind($('#login-form'),'submit',async e=>{e.preventDefault();const b=$('button',e.target);b.disabled=true;try{await api('/api/login',{method:'POST',body:{password:$('#password').value}});$('#password').value='';await boot();}catch(error){$('#login-error').textContent=error.message;}finally{b.disabled=false;}});
}
function shell() {
  $('#app').innerHTML=`<div class="shell"><nav class="rail" aria-label="主导航"><a class="brand" href="/conversations" aria-label="LSP 工作台"><img src="/static/favicon.svg" alt="LSP"></a><a href="/conversations" data-nav="conversations" class="nav-item">${icons.chat}<span>消息验证</span></a><a href="/settings" data-nav="settings" class="nav-item">${icons.settings}<span>集中设置</span></a><div class="end">DEMO<br><small>v0.1</small></div></nav><div class="workspace"><header class="topbar"><div class="breadcrumb"><strong>LSP · AI</strong><span class="slash">/</span><span id="breadcrumb-page"></span></div><div class="top-right">${pill('blocked','Freshchat · 待租户验证')}<span class="avatar" title="管理员">AD</span><button id="logout" class="ghost small">退出</button></div></header><main id="page" class="page"></main></div></div>`;
  $$('[data-nav]').forEach(a=>bind(a,'click',async e=>{e.preventDefault();if(S.dirty&&!confirm('设置尚未保存，确认离开？'))return;S.dirty=false;S.page=a.dataset.nav;history.pushState({},'',a.href);await renderPage();}));
  buttonAction($('#logout'),async()=>{await api('/api/logout',{method:'POST',body:{}});clearInterval(S.poll);await boot();});
}
window.addEventListener('popstate',async()=>{S.page=location.pathname==='/settings'?'settings':'conversations';await renderPage();});
async function renderPage() {
  $$('[data-nav]').forEach(a=>a.classList.toggle('active',a.dataset.nav===S.page));
  $('#breadcrumb-page').textContent=S.page==='settings'?'集中设置':'消息验证';
  if(S.page==='settings')await settingsPage();else await conversationsPage();
}
const groups = [
  ['platform','平台连接','确认区域、发送身份和测试范围',[
    ['integration_profile','当前已实现路线','readonly'],['platform_api_base_url','区域 API 地址','text','https://<tenant>.<region>.freshchat.com','从平台 API 设置复制官方区域主机，不预填推测地址。'],
    ['freshchat_token','Freshchat API Token','secret'],['reply_actor_id','发送坐席','agent','','点击读取坐席，再选择用于测试的 Agent。'],
    ['source_mapping','真实来源 → 渠道映射','json','{"web":"Webchat","whatsapp":"WhatsApp"}','按实际 message_source 建立映射；channel_id 仅作为 Topic 保存。'],
    ['allowed_channels','允许外发的渠道','list','WhatsApp, Webchat','先配置来源映射，再填写渠道名；初始为空。'],
    ['test_identity_allowlist','测试客户 / 会话白名单（兼容限制，可留空）','list','user:<ID>, conversation:<ID>','自动发现模式请留空；填写后仅接收指定客户或会话，用于兼容旧的限定测试流程。',true],
    ['seed_conversation_id','首次会话 ID','text'],['seed_user_id','首次客户 ID','text']]],
  ['webhook','Webhook 入站','平台订阅、RSA 验签与回调地址',[
    ['public_base_url','服务公网 HTTPS 地址','text','https://<DEMO_HOST>','公网部署或隧道由部署者提供。'],['webhook_path','回调路径','readonly'],
    ['freshchat_public_key','Freshchat RSA 验签公钥','textarea','','可粘贴 PEM 或 Base64 DER 公钥；原始请求体使用 RSA-SHA256 验证。',true]]],
  ['openai','OpenAI 与客服知识','真实模型调用、上下文与结构化输出',[
    ['openai_api_key','OpenAI API Key','secret'],['openai_model','模型 ID','text','','默认 gpt-6-astra（2026-09-18 官方推荐）；留空恢复默认。账号无权限时可在此修改。'],
    ['openai_base_url','OpenAI API 请求地址','text','https://api.openai.com/v1','支持公网 HTTPS 基础地址或完整 /responses 地址；留空用官方默认。自定义服务需兼容 Responses 和结构化输出，请填写该服务对应的 Key。'],['store','Responses 状态存储','readonly'],
    ['openai_project','Project（可选）','text'],['openai_organization','Organization（可选）','text'],
    ['system_instructions','客服说明','textarea','','服务端固定真实性及素材边界不能被客户消息覆盖。',true],
    ['knowledge_text','获准使用的 Demo 知识','textarea','','填写示例方案、停车地址和 FAQ；没有文档解析或媒体理解。',true],
    ['max_output_tokens','最大输出 Token','number'],['context_token_budget','输入上下文 Token 预算','number'],['model_timeout_seconds','模型超时（秒）','number']]],
  ['policy','AI 回复策略','每个会话单独控制，默认关闭',[
    ['auto_reply_enabled','全局自动回复','readonly','','请在消息验证页对单个会话开启或关闭 AI。',true],
    ['debounce_ms','连续输入防抖（毫秒）','number'],['max_reply_messages','每次最多独立回复数','number'],
    ['ai_requests_per_minute','AI 每分钟请求数','number'],['daily_token_budget','每日 Token 预算','number'],['max_safe_retries','明确安全请求重试次数','number']]],
  ['freshdesk','Freshdesk 跟进工单','真实 requester 映射与重复提交保护',[
    ['freshdesk_domain','Freshdesk 官方域名','text','https://<TENANT>.freshdesk.com'],['freshdesk_api_key','Freshdesk API Key','secret'],
    ['requester_mapping','Freshchat user_id → requester_id','json','{"<FRESHCHAT_USER_ID>":12345}','不能将 Freshchat user_id 直接作为 requester_id。',true],
    ['ticket_group_id','工单组 ID（可选）','number'],['priority','优先级','select',[[1,'低'],[2,'中'],[3,'高'],[4,'紧急']]],['status','初始状态','select',[[2,'开放'],[3,'待处理'],[4,'已解决'],[5,'已关闭']]],
    ['ticket_policy','建单策略','select',[['manual','手动'],['confirm','建议确认'],['automatic','规则自动']]],
    ['ticket_tags','工单标签','list'],['ticket_allowed_reasons','自动建单允许原因','list','','模型建议必须精确匹配允许原因。'],
    ['custom_field_mapping','自定义字段固定审核值','json','{"cf_parking_site":"<VALUE>"}','字段须已在租户存在；一期仅支持固定审核值。',true]]],
  ['media','素材与媒体','管理员审核的素材，按版本绑定发送任务',[
    ['media_size_limits','各类大小上限（字节）','json','','按平台与实际渠道较小限制填写。'],['allowed_mime_types','允许 MIME 类型','list'],
    ['media_host_allowlist','远程媒体主机白名单','list','media.example.com','完整小写主机名；每次重定向均检查，拒绝内网地址。',true]]],
  ['operations','历史与调度','本地缓存、任务恢复及独立鉴权',[
    ['history_page_size','历史每页条数（最大50）','number'],['local_retention_days','本地缓存保留天数','number'],['scheduler_token','独立 Scheduler Token','secret','','至少32字符；轮换后需更新外部调度中心。',true]]]
];
function field(f) {
  const [key,label,type,placeholder='',help='',wide=false]=f;const v=S.settings.values[key];const id='s-'+key;
  let control='';
  if(type==='readonly')control=`<div class="inline-value">${key==='integration_profile'?'Freshchat · 新版 Omni Ticket 未实现':key==='store'?'false（固定）· 不等于供应商零留存':key==='auto_reply_enabled'?'已改为会话页单独控制（默认关闭）':esc(S.settings.webhook_path)}</div>`;
  else if(type==='agent')control=`<div class="control-row"><select id="${id}" data-key="${key}" data-type="text"><option value="">请选择坐席</option>${v?`<option selected value="${esc(v)}">已保存 · ${esc(v)}</option>`:''}</select><button type="button" id="load-agents">读取坐席</button></div><details class="manual-agent"><summary>已有 Agent ID？手动填写</summary><label for="manual-agent-id">平台 Agent ID</label><input id="manual-agent-id" value="${esc(v)}" autocomplete="off"></details>`;
  else if(type==='checkbox')control=`<label class="check-label"><input id="${id}" data-key="${key}" data-type="${type}" type="checkbox" ${v?'checked':''}>启用新的客户消息自动回复</label>`;
  else if(type==='select')control=`<select id="${id}" data-key="${key}" data-type="${type}">${placeholder.map(([value,label])=>`<option value="${value}" ${String(value)===String(v)?'selected':''}>${label}</option>`).join('')}</select>`;
  else if(type==='textarea'||type==='json')control=`<textarea id="${id}" class="${type==='json'?'json':''}" data-key="${key}" data-type="${type}" placeholder="${esc(placeholder)}" rows="${type==='textarea'?4:3}">${esc(type==='json'?JSON.stringify(v,null,2):v)}</textarea>`;
  else control=`<input id="${id}" data-key="${key}" data-type="${type}" type="${type==='secret'?'password':type==='number'?'number':'text'}" ${type==='secret'?'autocomplete="new-password"':''} value="${esc(type==='list'?v.join(', '):type==='secret'?'':v)}" placeholder="${esc(type==='secret'?(S.settings.secrets[key]?'•••••••• 已设置，留空保持':'尚未设置'):placeholder)}">${type==='secret'?`<label class="check-label"><input type="checkbox" data-clear="${key}">明确清除已保存凭证</label>`:''}`;
  return `<div class="field ${wide?'wide':''}"><label for="${id}">${label}</label>${control}${help?`<small>${esc(help)}</small>`:''}</div>`;
}
const quickSections = [
  ['connect','连接平台和 AI','填写平台地址与两项密钥；OpenAI 地址可按需修改',['platform_api_base_url','freshchat_token','openai_api_key','openai_base_url']],
  ['test','坐席与单会话 AI','保存发送坐席后自动接收新会话；AI 仍需在单会话中明确恢复',['reply_actor_id']],
  ['webhook','接收新消息','测试 Webhook 和自动回复时再填写',['public_base_url','freshchat_public_key']],
  ['knowledge','知识与素材','测试停车问答、图片、视频或 PDF 时再添加',['knowledge_text']],
  ['freshdesk','跟进工单','需要创建 Ticket 时再填写',['freshdesk_domain','freshdesk_api_key','requester_mapping']],

];
const settingFields = groups.flatMap(g=>g[3]);
function settingsDirty() {S.dirty=true;$('#save-note').textContent='有未保存修改。检查或读取前会先保存；保存不会发送消息。';}
async function saveSettings() {
  const patch={clear_secrets:$$('[data-clear]:checked').map(x=>x.dataset.clear)};
  $$('[data-key]').forEach(el=>{const key=el.dataset.key,type=el.dataset.type;let value=el.value;
    if(type==='json'){try{value=JSON.parse(value);}catch{throw new Error(`${settingFields.find(f=>f[0]===key)[1]}不是有效 JSON`);}}
    else if(type==='checkbox')value=el.checked;
    else if(type==='list')value=value.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean);
    else if(type==='number')value=key==='ticket_group_id'&&!value?null:Number(value);
    else if(type==='select'&&['priority','status'].includes(key))value=Number(value);
    if(JSON.stringify(value)!==JSON.stringify(S.settings.values[key]))patch[key]=value;
  });
  if($('#s-reply_actor_id').value&&!Object.hasOwn(patch,'test_identity_allowlist'))patch.reply_actor_id=$('#s-reply_actor_id').value;
  const tenantChanged=Object.hasOwn(patch,'platform_api_base_url')||!!patch.freshchat_token||patch.clear_secrets.includes('freshchat_token');
  S.settings=await api('/api/settings',{method:'PUT',body:patch});S.dirty=false;
  $$('[data-type="secret"]').forEach(el=>{el.value='';el.placeholder=S.settings.secrets[el.dataset.key]?'•••••••• 已设置，留空保持':'尚未设置';});
  $$('[data-clear]').forEach(el=>el.checked=false);
  $('#s-openai_model').value=S.settings.values.openai_model;
  $('#s-openai_base_url').value=S.settings.values.openai_base_url;
  $('#model-default').textContent='当前模型 '+S.settings.values.openai_model+' · 可在高级设置修改';
  $$('[data-type="json"],[data-type="list"]').forEach(el=>{const value=S.settings.values[el.dataset.key];el.value=el.dataset.type==='json'?JSON.stringify(value,null,2):value.join(', ');});
  $('#save-note').textContent='设置已保存。密钥留空保持；保存不会发送消息。';
  if(tenantChanged||['test_identity_allowlist','source_mapping','allowed_channels'].some(key=>Object.hasOwn(patch,key)))await refreshTestTargets();
  await refreshDiscovery();
}
async function settingsPage() {
  S.settings=await api('/api/settings');
  const visible=new Set(quickSections.flatMap(g=>g[3]));
  const advanced=groups.map(g=>{const fields=g[3].filter(f=>!visible.has(f[0]));return fields.length?`<div class="advanced-group"><h3>${g[1]}</h3><div class="field-grid">${fields.map(field).join('')}</div>${g[0]==='operations'?sectionExtra('operations'):''}</div>`:'';}).join('');
  const sections=quickSections.map((g,i)=>`<details id="section-${g[0]}" class="config-section" ${i<2?'open':''}><summary><span class="section-number">0${i+1}</span><div class="section-title"><h2>${g[1]}</h2><small>${g[2]}</small></div></summary><div class="section-body"><div class="field-grid">${g[3].map(key=>field(settingFields.find(f=>f[0]===key))).join('')}</div>${g[0]==='connect'?`<div class="section-footer"><button type="button" id="check-openai">保存并检查 OpenAI</button><small>少量真实调用费用；不会发给客户。</small></div><small id="model-default">当前模型 ${esc(S.settings.values.openai_model)} · 可在高级设置修改</small>`:g[0]==='test'?`<div id="test-discovery" class="test-targets" aria-live="polite"></div><details class="manual-agent"><summary>已有会话？手动导入或绑定</summary><div id="test-targets" class="test-targets"></div><div class="section-footer"><button type="button" id="import-from-settings">导入已知会话</button><button type="button" id="refresh-targets">刷新会话</button><button type="button" id="check-read">检查平台读取</button></div></details>`:g[0]==='knowledge'?sectionExtra('media'):sectionExtra(g[0])}</div></details>`).join('');
  $('#page').innerHTML=`<div class="pagehead"><div><div class="eyebrow">QUICK START</div><h1>填好连接信息，开始测试</h1><p>模型、客服说明和运行参数已预设。先连接，再选测试会话。</p></div><div class="actions"><button id="show-events">最近事件</button><button id="matrix">能力矩阵 ↗</button></div></div><p class="setup-route">Freshchat 会话路线 · 待租户验证。新版 Omni Ticket 路线尚未实现。</p><form id="settings-form"><div class="settings-layout"><aside class="section-nav">${quickSections.map((g,i)=>`<a href="#section-${g[0]}"><span class="num">0${i+1}</span>${g[1]}</a>`).join('')}<a href="#section-advanced">高级设置</a><div class="side-note">开启会话 AI 后将直接回复新消息。<br>自动回复默认关闭。</div></aside><div class="settings-main">${sections}<details id="section-advanced" class="config-section"><summary><div class="section-title"><h2>高级设置</h2><small>模型、限额、来源映射、工单策略与调度。通常无需修改。</small></div></summary><div class="section-body">${advanced}</div></details><div class="savebar"><small id="save-note">密钥留空保持原值；保存不会发送消息。</small><button class="primary" type="submit">保存设置</button></div></div></div></form>`;
  bind($('#settings-form'),'input',e=>{if(e.target.matches('[data-key],[data-clear],#manual-agent-id'))settingsDirty();});
  bind($('#settings-form'),'submit',async e=>{e.preventDefault();const b=$('button[type=submit]',e.target);b.disabled=true;try{await saveSettings();toast('设置已保存');}finally{b.disabled=false;}});
  $$('.section-nav a').forEach(a=>bind(a,'click',()=>{$(a.getAttribute('href')).open=true;}));
  buttonAction($('#matrix'),showMatrix);buttonAction($('#show-events'),showEvents);
  buttonAction($('#check-read'),async()=>{await saveSettings();showResult('平台读取结果',await api('/api/checks',{method:'POST',body:{kind:'platform_read',conversation_id:S.conversations.find(c=>c.id===Number($('#test-conversation')?.value))?.platform_id}}));});
  buttonAction($('#check-openai'),async()=>{await saveSettings();toast('正在检查 OpenAI…');showResult('OpenAI 检查结果',await api('/api/checks',{method:'POST',body:{kind:'openai'}}));});
  buttonAction($('#import-from-settings'),async()=>{await saveSettings();importModal();});
  buttonAction($('#refresh-targets'),refreshTestTargets);
  buttonAction($('#copy-webhook'),async()=>{await saveSettings();if(!S.settings.values.public_base_url)throw new Error('请先填写公网 HTTPS 地址');await navigator.clipboard.writeText(S.settings.values.public_base_url+S.settings.webhook_path);toast('回调 URL 已复制');});
  buttonAction($('#add-asset'),async()=>{await saveSettings();assetModal();});
  buttonAction($('#load-agents'),async()=>{await saveSettings();const data=await api('/api/agents');const select=$('#s-reply_actor_id'),current=select.value;
    select.innerHTML=`<option value="">请选择用于测试的坐席</option>${data.agents.map(a=>`<option value="${esc(a.id)}">${esc(a.name)} · ${esc(a.id)}</option>`).join('')}`;
    if(current&&!data.agents.some(a=>a.id===current))select.add(new Option('已保存 · '+current,current));select.value=current;
    toast(data.agents.length?'坐席已读取，请选择专用测试坐席':'未找到可用坐席，请管理员核对权限');
  });
  bind($('#manual-agent-id'),'input',e=>{const select=$('#s-reply_actor_id'),value=e.target.value.trim();if(![...select.options].some(o=>o.value===value))select.add(new Option(value,value));select.value=value;});
  bind($('#s-public_base_url'),'input',()=>{$('#webhook-url').textContent=($('#s-public_base_url').value||'https://<DEMO_HOST>')+S.settings.webhook_path;});
  await refreshTestTargets();await refreshDiscovery();await renderAssets();
}
async function refreshDiscovery() {
  const root=$('#test-discovery');if(!root)return;
  const d=await api('/api/test-discovery');if(!root.isConnected)return;
  const fingerprint=JSON.stringify(d);if(root.dataset.state===fingerprint)return;
  root.dataset.state=fingerprint;
  const cards=(d.bindings||[]).map(b=>`<div class="advanced-group"><h3>${esc(b.channel)} · ${b.enabled?'AI 自动回复中':b.paused?'人工接管中':'AI 已关闭'}</h3><p class="mono-block">客户 ID：${esc(b.user_id)}<br>会话 ID：${esc(b.conversation_id)}<br>平台来源：${esc(b.source)}</p><a href="/conversations?conversation=${b.local_id}">打开会话，手动回复 →</a></div>`).join('');
  root.innerHTML=`<div class="notice"><div><strong>${d.auto_discovery?'已启用新消息自动收集':'新消息自动收集未启用'}</strong><br><span class="muted">${d.auto_discovery?'新客户消息自动出现在消息验证页，默认 AI 关闭。事件含坐席归属时按所选坐席过滤；缺少时先展示并在后台读取会话归属，其他坐席的会话将移出列表。':'请重新保存发送坐席以恢复自动收集。'}</span></div></div>${cards}<p class="readonly-note">打开 /conversations 后会自动刷新新会话。选中会话默认 AI 关闭，在会话右侧一键开启或关闭；Webhook 最近事件可查看事件原始记录。无需填写客户 ID 或会话 ID。</p><div class="actions"><a href="/conversations">打开消息验证 →</a><button type="button" id="toggle-discovery">${d.auto_discovery?'暂停收集':'使用已选坐席恢复收集'}</button></div>`;
  buttonAction($('#toggle-discovery'),async()=>{if(d.auto_discovery)await api('/api/test-discovery',{method:'DELETE',body:{}});else await api('/api/settings',{method:'PUT',body:{reply_actor_id:$('#s-reply_actor_id').value,test_identity_allowlist:[]}});await settingsPage();toast(d.auto_discovery?'已暂停收集和 AI，已提交请求无法撤回':'已恢复收集，新会话默认 AI 关闭');});
}
async function refreshTestTargets() {
  S.conversations=(await api('/api/conversations')).conversations;
  const root=$('#test-targets');if(!root)return;
  const selected=$('#test-conversation')?.value;
  const scope=S.settings.values.test_identity_allowlist;
  const scopeNote=`<p class="mono-block">${scope.length?'当前接收与外发范围：'+scope.map(esc).join('、'):'未设置额外客户限制；自动收集状态见上方。'}</p>${scope.length?'<button type="button" id="pause-test" class="small">暂停测试</button>':''}`;
  root.innerHTML=scopeNote+(S.conversations.length?`<div class="field-grid test-targets"><div class="field"><label for="test-conversation">此客户的一条真实会话</label><select id="test-conversation"><option value="">请选择会话</option>${S.conversations.map(c=>`<option value="${c.id}">${esc(c.platform_id)} · ${esc(c.user_id||'客户待同步')}</option>`).join('')}</select></div><div class="field"><label for="test-channel">确认原客户渠道</label><input id="test-channel" list="test-channel-options" placeholder="请选择或填写实际渠道"><datalist id="test-channel-options"><option value="WhatsApp"><option value="WeChat"><option value="Webchat"></datalist></div></div><p id="test-source" class="readonly-note"></p><div class="actions"><button type="button" id="allow-test">只用此客户测试</button><a href="/conversations">打开消息验证 →</a></div><p class="readonly-note">替换原测试范围，仅保留此客户 ID。同一 ID 的新会话也会接收；其他客户事件不保存、不拉历史、不调用 AI。此为兼容入口，日常无需绑定或填写 ID。</p>`:'<p class="muted">新会话通过 Webhook 自动出现；仅需要旧历史入口时才手动导入。</p>');
  buttonAction($('#pause-test'),async()=>{await saveSettings();S.settings=await api('/api/settings',{method:'PUT',body:{test_identity_allowlist:[],auto_reply_enabled:false}});toast('已暂停测试，新回调将忽略；已提交请求无法撤回');await settingsPage();});
  if(!S.conversations.length)return;
  if(S.conversations.some(c=>String(c.id)===selected))$('#test-conversation').value=selected;
  const update=()=>{const c=S.conversations.find(c=>c.id===Number($('#test-conversation').value));$('#test-channel').value=c&&c.channel!=='unknown'?c.channel:'';$('#test-source').textContent=c?`客户 ID：${c.user_id||'待同步'} · 平台来源：${c.source||'待同步'} · ${c.sync_complete?'历史已同步':'请先在消息页同步历史，再刷新'}`:'选定会话后，按真实客户 ID 绑定，不使用昵称匹配。';$('#allow-test').disabled=!c||!c.source||!c.user_id||!c.sync_complete;};
  bind($('#test-conversation'),'change',update);update();
  buttonAction($('#allow-test'),async()=>{const cid=Number($('#test-conversation').value),channel=$('#test-channel').value.trim();if(!channel)throw new Error('请确认此会话的实际渠道');await saveSettings();S.settings=await api('/api/conversations/'+cid+'/test-access',{method:'POST',body:{channel,scope:'customer'}});toast('已限定为此客户；尚未发送消息');await settingsPage();});
}
function sectionExtra(group) {
  if(group==='platform')return `<div class="section-footer"><button type="button" id="check-read">检查平台读取</button><button type="button" id="import-from-settings">导入测试会话</button><small>先保存配置；读取检查使用首次会话 ID。</small></div>`;
  if(group==='webhook')return `<div id="webhook-url" class="mono-block">${esc((S.settings.values.public_base_url||'https://<DEMO_HOST>')+S.settings.webhook_path)}</div><div class="actions"><button type="button" class="small" id="copy-webhook">复制回调 URL</button></div><p class="readonly-note">在 Freshchat 管理设置的 Webhooks 中订阅 message_create，将公钥填入上方。请求需含 X-Freshchat-Signature；同时记录载荷版本与重试次数。测试前协调关闭目标会话的既有机器人、自动回复，并检查坐席分配。</p>`;
  if(group==='openai')return `<div class="section-footer"><button type="button" id="check-openai">检查 OpenAI</button><small>会产生少量真实调用费用；不会向客户发送。</small></div>`;
  if(group==='policy')return `<p class="readonly-note">AI 由会话页单独开启或关闭。开启时记录消息时间边界，不补发历史回复；关闭后只保留人工回复。外发结果不明不会自动重试。</p>`;
  if(group==='media')return `<div class="section-footer"><h3>已审核素材</h3><button id="add-asset" type="button" class="small">＋ 新增 / 上传素材</button></div><div id="asset-list" class="asset-list"></div><p class="readonly-note">支持 JPEG、PNG、MP4 和 PDF。替换同一 asset_id 会生成新版本；扫描中素材不可发送。视频固定审核版本后通过本应用24小时限定用途地址发送，需公网 HTTPS。</p>`;
  if(group==='operations')return `<div class="mono-block">POST /api/internal/jobs/drain<br>X-Scheduler-Token: &lt;SCHEDULER_TOKEN&gt;</div><p class="readonly-note">建议每分钟调用一次，默认 dry_run:true，limit 最大100。真实执行需 allow_external_effects:true。固定任务：recover_pending、cleanup_cache。清理不删除平台历史，不处理结果不明外发。</p>`;
  return '';
}
function showResult(title,result) {modal(title,`<pre class="mono-block">${esc(JSON.stringify(result,null,2))}</pre>`,null);}
async function showEvents() {
  const data=await api('/api/events');
  modal('Webhook 最近事件',data.events.length?`<p class="muted">管理员可点击事件查看保存的原始 JSON 载荷。消息正文只在本地加密数据库中保存，新会话默认 AI 关闭，只有单会话明确启用后才调用模型。</p><div class="table-wrap"><table class="data-table"><thead><tr><th>时间</th><th>事件 / 消息 ID</th><th>版本 / 重试</th><th></th></tr></thead><tbody>${data.events.map(e=>`<tr><td>${fmtTime(e.created)}</td><td>${esc(e.action)}<small class="mono">${esc(e.platform_id||'—')}</small></td><td>${esc(e.version)} / ${esc(e.retries)}</td><td><button type="button" class="ghost small event-payload" data-event-id="${e.id}">查看原始记录</button></td></tr>`).join('')}</tbody></table></div>`:'<div class="empty"><h3>尚未收到真实事件</h3><p>先保存坐席并配置 Webhook。收到新消息后会自动出现在消息验证页和这里。</p></div>',null,'',true);
  $$('.event-payload').forEach(button=>buttonAction(button,()=>{const event=data.events.find(e=>String(e.id)===button.dataset.eventId);showResult('Webhook 原始事件 · '+(event?.platform_id||'—'),event?.payload||{message:'该事件未保存载荷'});}));
}
async function showMatrix() {
  const data=await api('/api/capabilities');const latest=(ch,cap)=>data.checks.find(x=>x.channel===ch&&x.capability===cap&&!x.stale);
  modal('逐渠道能力矩阵',`<p class="muted">版本 ${data.revision} · API 受理、原工作台展示和客户实际收到分别核对。灰色表示未测试，旧配置证据仅供查阅。</p>${data.channels.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>能力</th>${data.channels.map(ch=>`<th>${esc(ch)}</th>`).join('')}</tr></thead><tbody>${data.capabilities.map(cap=>`<tr><td>${labels[cap]}</td>${data.channels.map(ch=>{const check=latest(ch,cap);return `<td><button class="ghost small evidence-cell" data-check="${check?.id||''}">${pill(check?.status||'not_tested')}</button></td>`;}).join('')}</tr>`).join('')}</tbody></table></div>`:'<div class="empty"><h3>先配置实际测试渠道</h3><p>至少验证 WhatsApp 和客户指定的第二渠道；WeChat 必须测试实际连接器。</p></div>'}<div class="actions"><button type="button" id="record-evidence">记录失败 / 阻塞证据</button><button type="button" id="check-history">检查记录（${data.checks.length}）</button></div>`,null,'',true);
  $$('.evidence-cell').forEach(el=>buttonAction(el,()=>{const row=data.checks.find(x=>x.id===Number(el.dataset.check));if(row)showResult(labels[row.capability]+' · '+row.channel,row);else toast('此项尚无当前配置下的验证记录');}));
  buttonAction($('#check-history'),()=>showResult('脱敏检查记录',data.checks));
  buttonAction($('#record-evidence'),()=>modal('记录渠道能力证据',`<label>渠道<select name="channel">${data.channels.map(ch=>`<option>${esc(ch)}</option>`).join('')}</select></label><label>能力<select name="capability">${data.capabilities.map(cap=>`<option value="${cap}">${labels[cap]}</option>`).join('')}</select></label><label>状态<select name="status"><option value="blocked">受阻（权限 / 环境）</option><option value="failed">已执行失败</option><option value="unsupported">官方确认不支持</option><option value="not_tested">未测试</option></select></label><label>测试对象<input name="target" placeholder="会话 ID"></label><label>脱敏证据<textarea name="evidence" required placeholder="记录错误码、官方证据链接及测试时间；不要填写凭证或客户内容。"></textarea></label><small>403 通常只能证明权限不足，不能直接标记不支持。</small>`,async f=>{await api('/api/checks',{method:'POST',body:{kind:'record',...Object.fromEntries(f)}});toast('证据已保存');},'保存记录'));
}
async function renderAssets() {
  S.assets=(await api('/api/assets')).assets;const root=$('#asset-list');if(!root)return;
  root.innerHTML=S.assets.length?S.assets.map(a=>`<div class="asset-row">${a.kind==='image'?`<img class="asset-thumb" src="${esc(a.preview_url)}" alt="${esc(a.name)}">`:`<div class="asset-thumb">${a.kind==='video'?'MP4':'PDF'}</div>`}<div><strong>${esc(a.name)}</strong> ${pill(a.state)}<small>${esc(a.purpose)}</small><small class="mono">${esc(a.id)} · ${bytes(a.size)} · ${esc(a.mime)}</small><small>${esc(a.filename)} · ${esc(a.channels.join(' / '))} · ${esc(a.source)}</small>${a.error?`<small class="error-text">${esc(a.error)}</small>`:''}</div><div class="actions"><button type="button" class="small asset-preview" data-id="${esc(a.id)}">预览</button><button type="button" class="small asset-upload" data-id="${esc(a.id)}">${a.state==='scanning'?'重新上传检查':'上传平台'}</button><button type="button" class="small asset-edit" data-id="${esc(a.id)}">编辑</button></div></div>`).join(''):'<div class="empty"><h3>还没有审核素材</h3><p>上传停车入口图、指引视频或方案 PDF，填写用途并授权测试渠道。</p></div>';
  $$('.asset-upload').forEach(b=>buttonAction(b,async()=>{const a=await api('/api/assets/'+b.dataset.id+'/upload',{method:'POST',body:{}});toast(a.state==='scanning'?'平台返回 AV_PENDING，保持扫描中；不会作为可发送素材。':'素材引用已保存；仍需逐渠道发送验证。');await renderAssets();}));
  $$('.asset-edit').forEach(b=>buttonAction(b,()=>editAsset(S.assets.find(a=>a.id===b.dataset.id))));
  $$('.asset-preview').forEach(b=>buttonAction(b,()=>previewAsset(S.assets.find(a=>a.id===b.dataset.id))));
}
function previewAsset(a) {modal(a.name,`<p>${esc(a.purpose)}</p>${a.kind==='image'?`<img class="media-preview" src="${esc(a.preview_url)}" alt="${esc(a.name)}">`:a.kind==='video'?`<video class="media-preview" src="${esc(a.preview_url)}" controls preload="metadata"></video>`:`<a class="attachment" href="${esc(a.preview_url)}">↓ 下载 ${esc(a.filename)} · ${bytes(a.size)}</a>`}<small>版本 ${a.version} · ${esc(a.mime)} · ${esc(a.source)} · ${a.has_platform_reference?'已有平台/视频引用':'暂无可发引用'}</small>`,null);}
function assetModal(after) {
  modal('新增审核素材',`<div class="field-grid"><label>素材逻辑 ID<input name="asset_id" required placeholder="parking_entry_map"></label><label>名称<input name="name" required placeholder="停车场入口位置图"></label></div><label>用途说明<textarea name="purpose" required placeholder="说明素材对应停车场、可回答的问题和适用条件，供模型选择。"></textarea></label><label>标签（逗号分隔）<input name="tags"></label><label>允许渠道（逗号分隔）<input name="channels" required value="${esc(S.settings.values.allowed_channels.join(', '))}"></label><label>本地文件<input type="file" name="file" accept=".jpg,.jpeg,.png,.mp4,.pdf"></label><label>或审核 HTTPS URL<input type="url" name="url" placeholder="https://<APPROVED_HOST>/guide.mp4"></label><small>选择文件优先于 URL；远程主机必须先保存至白名单。新增同一逻辑 ID 自动生成新版本。</small>`,async f=>{
    const metadata={asset_id:f.get('asset_id'),name:f.get('name'),purpose:f.get('purpose'),tags:String(f.get('tags')).split(/[,，]/).map(x=>x.trim()).filter(Boolean),channels:String(f.get('channels')).split(/[,，]/).map(x=>x.trim()).filter(Boolean)};
    const file=f.get('file');let result;
    if(file?.size){const form=new FormData();form.append('file',file);form.append('metadata',JSON.stringify(metadata));result=await api('/api/assets',{method:'POST',body:form});}
    else {if(!f.get('url'))throw new Error('请选择文件或填写审核 URL');result=await api('/api/assets',{method:'POST',body:{...metadata,url:f.get('url')}});}
    if(after){if(result.state!=='sendable')result=await api('/api/assets/'+result.id+'/upload',{method:'POST',body:{}});if(result.state!=='sendable')throw new Error('素材已保存但扫描未完成，暂不可发送。请在设置页查看。');S.assets=(await api('/api/assets')).assets;after(result);}
    else await renderAssets();toast('素材已保存，版本 '+result.version);
  },'保存审核素材');
}
function editAsset(a) {modal('编辑素材 · '+a.name,`<label>名称<input name="name" value="${esc(a.name)}" required></label><label>用途<textarea name="purpose" required>${esc(a.purpose)}</textarea></label><label>标签<input name="tags" value="${esc(a.tags.join(', '))}"></label><label>允许渠道<input name="channels" value="${esc(a.channels.join(', '))}"></label><label class="check-label"><input type="checkbox" name="enabled" ${a.enabled?'checked':''}>启用素材（重新启用后需重新上传确认）</label><small>替换文件请使用同一逻辑 ID 新增版本，排队任务绑定原版本。</small>`,async f=>{await api('/api/assets/'+a.id,{method:'PATCH',body:{name:f.get('name'),purpose:f.get('purpose'),tags:String(f.get('tags')).split(',').map(x=>x.trim()).filter(Boolean),channels:String(f.get('channels')).split(',').map(x=>x.trim()).filter(Boolean),enabled:f.has('enabled')}});await renderAssets();toast('素材已更新');},'保存');}
function importModal() {modal('导入真实测试会话',`<p class="muted">读取当前平台账号有权访问的会话。填写会话 ID，或仅填写客户 ID 导入该客户的其他会话。</p><label>平台会话 ID<input name="conversation_id" value="${esc(S.settings.values.seed_conversation_id)}"></label><label>平台客户 ID<input name="user_id" value="${esc(S.settings.values.seed_user_id)}"></label><small>仅从真实接口导入，不自动合并跨渠道客户。</small>`,async f=>{const result=await api('/api/conversations/import',{method:'POST',body:Object.fromEntries(f)});toast('会话已导入，后台正在同步全部可访问历史');if(result.conversation_id)S.selected=result.conversation_id;if(S.page==='conversations')await refreshConversations();else await refreshTestTargets();},'读取并导入');}
async function conversationsPage() {
  $('#page').innerHTML=`<div class="pagehead"><div><div class="eyebrow">CONVERSATION WORKSPACE</div><h1>消息验证</h1><p>同一客户、同一会话、原始渠道。逐条核对真实回复。</p></div><div class="actions"><button id="conv-matrix">能力矩阵</button><button id="conv-events" class="primary">Webhook 最近事件</button><details><summary>其他入口</summary><button id="conv-import">导入已有会话</button></details></div></div><div class="notice"><span>ⓘ</span><div><strong>真实接口待验证</strong> · 当前实现 Freshchat 路线。API 受理后，请分别核对 Omni 原工作台与客户原渠道，人工确认不会伪造送达回执。</div></div><div class="conversation-grid"><aside class="conversation-list"><div class="list-head"><h3>已接入会话 <span id="conv-count" class="pill">0</span></h3><input id="conversation-search" aria-label="按客户或会话 ID 筛选" placeholder="搜索客户 / 会话 ID"><select id="channel-filter" aria-label="按渠道筛选"><option value="">全部渠道</option>${S.settings.values.allowed_channels.map(c=>`<option>${esc(c)}</option>`).join('')}</select></div><div id="conversation-items" class="list-items"></div></aside><section id="message-pane" class="message-pane"></section><aside id="inspector" class="inspector"></aside></div>`;
  buttonAction($('#conv-import'),importModal);buttonAction($('#conv-matrix'),showMatrix);buttonAction($('#conv-events'),showEvents);
  bind($('#conversation-search'),'input',renderConversationList);bind($('#channel-filter'),'change',renderConversationList);
  await refreshConversations(true);
}
async function refreshConversations(force=false) {
  const result=await api('/api/conversations');if(S.page!=='conversations')return;
  S.conversations=result.conversations;const filter=$('#channel-filter'),selectedChannel=filter.value;filter.innerHTML='<option value="">全部渠道</option>'+[...new Set(S.conversations.map(c=>c.channel))].map(ch=>`<option>${esc(ch)}</option>`).join('');filter.value=selectedChannel;renderConversationList();
  if(S.selected&&!S.conversations.some(c=>c.id===S.selected)){S.selected=null;S.detail=null;}
  if(!S.selected){emptyConversation();return;}
  const requested=S.selected;
  const detail=await api('/api/conversations/'+requested+'/messages?limit='+S.limit);
  if(S.page!=='conversations'||S.selected!==requested)return;
  const previous=S.detail;
  if(previous?.conversation.id===requested && previous.messages.length>detail.messages.length && detail.total>=previous.messages.length){
    const merged=new Map([...previous.messages,...detail.messages].map(m=>[m.platform_id,m]));
    detail.messages=[...merged.values()].sort((a,b)=>a.created.localeCompare(b.created)||a.platform_id.localeCompare(b.platform_id));
    detail.next_before=detail.messages.length<detail.total?detail.messages.length:null;
  }
  S.detail=detail;
  if(force||!previous||previous.conversation.id!==requested)renderThread();
  else updateThread();
  renderInspector(previous?.conversation.id===requested);
}
function renderConversationList() {
  const q=($('#conversation-search')?.value||'').toLowerCase(),channel=$('#channel-filter')?.value;
  const rows=S.conversations.filter(c=>(!q||(c.platform_id+' '+c.user_id).toLowerCase().includes(q))&&(!channel||c.channel===channel));
  $('#conv-count').textContent=S.conversations.length;
  $('#conversation-items').innerHTML=rows.length?rows.map(c=>`<button class="conversation-item ${c.id===S.selected?'selected':''}" data-id="${c.id}"><strong>${esc(c.user_id||c.platform_id)}</strong>${pill('',c.channel)} <span class="pill">${c.mode==='manual'?'人工':c.mode==='auto'?'自动':'AI 关闭'}</span><p>${esc(c.preview||'正在同步历史…')}</p><small class="mono">${esc(c.platform_id)}</small></button>`).join(''):`<div class="empty"><small>${S.conversations.length?'没有匹配的会话':'暂无真实会话'}<br>保存发送坐席后，Webhook 会自动接收新消息</small></div>`;
  $$('.conversation-item').forEach(b=>buttonAction(b,async()=>{if(S.drafts.length||$('#message-text')?.value){if(!confirm('切换会话将清空当前未发送草稿，继续？'))return;}S.drafts=[];S.selected=Number(b.dataset.id);S.detail=null;S.limit=50;await refreshConversations(true);}));
}
function emptyConversation() {
  $('#message-pane').innerHTML=`<div class="empty"><div class="empty-symbol">↔</div><div class="eyebrow">READY WHEN YOU ARE</div><h2>${S.conversations.length?'选择一个会话开始验证':'等待第一条真实消息'}</h2><p>保存设置页的发送坐席后，用 WhatsApp 或 WeChat 发一条公开消息，<br>Webhook 会自动取得会话并显示在这里。</p><a class="primary" href="/settings">查看连接设置</a><div class="step-list"><span><b>1</b>配置平台</span><span><b>2</b>接收事件</span><span><b>3</b>单会话开关 AI</span></div></div>`;
  $('#inspector').innerHTML=`<div class="inspector-section"><h3>AI 助理</h3>${pill('','未选择会话')}<p class="muted">每个会话单独控制，默认关闭。打开会根据该会话历史处理后续新消息。</p></div><div class="inspector-section"><h3>验证的三个层次</h3><p class="muted">01　API 受理及消息 ID<br>02　Omni 原时间线可见<br>03　客户原渠道实际收到</p></div><div class="inspector-section"><h3>人工优先</h3><p class="muted">关闭 AI 后只保留人工回复。已在途请求无法撤回，其余未发送任务取消。</p></div>`;
}
function threadHeader() {const d=S.detail,c=d.conversation;return `<div class="thread-head"><div><h3>${esc(c.user_id||'客户 ID 待同步')}</h3><small class="mono">${esc(c.platform_id)}</small></div><div class="actions">${pill('',c.channel)}<button id="sync-history" class="small">同步历史</button><button id="same-user" class="small" ${c.user_id?'':'disabled'}>同客户会话</button></div></div><div id="thread-meta" class="thread-meta"></div>`;}
function renderThread() {
  $('#message-pane').innerHTML=`${threadHeader()}<div id="thread" class="thread"></div><div class="composer"><textarea id="message-text" aria-label="回复文本" placeholder="输入回复内容。发送后，此会话将进入人工模式。"></textarea><div class="actions"><button id="add-text" class="small">＋ 加入一条文本</button><button id="choose-asset" class="small">选择素材</button><button id="upload-inline" class="small">上传文件</button></div><div id="draft-list" class="draft-list"></div><div class="composer-footer"><small>预览目标和内容后，再确认外发</small><button id="send-message" class="primary">预览并发送 →</button></div></div>`;
  buttonAction($('#sync-history'),async()=>{await api('/api/conversations/'+S.selected+'/sync',{method:'POST',body:{}});toast('历史同步已排队');});
  buttonAction($('#same-user'),async()=>{await api('/api/conversations/import',{method:'POST',body:{conversation_id:'',user_id:S.detail.conversation.user_id}});toast('该客户的其他真实会话已导入');await refreshConversations();});
  buttonAction($('#add-text'),()=>{const text=$('#message-text').value.trim();if(!text)throw new Error('请输入文本');S.drafts.push({type:'text',text,asset_id:null});$('#message-text').value='';renderDrafts();});
  buttonAction($('#choose-asset'),chooseAsset);buttonAction($('#upload-inline'),()=>assetModal(a=>{S.drafts.push({type:a.kind,text:null,asset_id:a.id});renderDrafts();}));
  buttonAction($('#send-message'),sendModal);updateThread(true);renderDrafts();
}
function messageHTML(m) {
  return `<article class="message ${esc(m.role)}"><div class="message-meta"><strong>${roleNames[m.role]||m.actor}</strong><span>${fmtTime(m.created)}</span><span>${esc(m.parts.map(p=>labels[p.type]||p.type).join(' / '))}</span></div><div class="bubble">${m.parts.map(p=>{
    if(p.type==='text'||p.type==='unsupported')return esc(p.text);
    if(!p.url)return `<span>${esc(p.type)}：${esc(p.name)} · 无可用预览地址</span>`;
    if(p.type==='image')return `<img class="media-preview" loading="lazy" src="${esc(p.url)}" alt="客户或平台图片"><small>${esc(p.name)} · ${bytes(p.size)}</small>`;
    if(p.type==='video')return `<video class="media-preview" controls preload="none" src="${esc(p.url)}"></video><small>${esc(p.name)} · ${bytes(p.size)}</small>`;
    return `<a class="attachment" href="${esc(p.url)}">↓ ${esc(p.name)} · ${bytes(p.size)}<small>${esc(p.mime)}</small></a>`;
  }).join('')}<small class="media-fallback hidden">预览受来源白名单、链接有效期或浏览器限制；不代表客户发送失败。</small></div><span class="mono">${esc(m.platform_id)}${m.interaction?' · interaction '+esc(m.interaction):''}</span></article>`;
}
function updateThread(scroll=false) {
  const d=S.detail,c=d.conversation;$('#thread-meta').innerHTML=`已同步 ${d.total} 条 · ${c.sync_complete?'所有可访问历史已同步':'同步尚未完成'}<br>${esc(d.earliest||'—')} → ${esc(d.latest||'—')}<br>来源 ${esc(c.source||'未知')} · Topic ${esc(c.topic_id||'未提供')} · 坐席归属 ${esc(c.assigned_agent_id||'平台未提供，请核对目标')}${c.sync_error?`<span class="error-text"> · ${esc(c.sync_error)}</span>`:''}`;
  const thread=$('#thread');if(!thread)return;const nearBottom=thread.scrollHeight-thread.scrollTop-thread.clientHeight<80;
  const content=(d.next_before!==null?'<button class="small" id="load-earlier">加载更早消息</button>':'')+d.messages.map(messageHTML).join('');
  if(thread.dataset.signature!==content){thread.innerHTML=content;thread.dataset.signature=content;if(scroll||nearBottom)thread.scrollTop=thread.scrollHeight;buttonAction($('#load-earlier'),async()=>{if(S.limit>=100){const older=await api('/api/conversations/'+S.selected+'/messages?before='+d.next_before+'&limit=100');S.detail.messages=[...older.messages,...d.messages];S.detail.next_before=older.next_before;updateThread();}else {S.limit=100;await refreshConversations();}});
    $$('.media-preview',thread).forEach(el=>el.addEventListener('error',()=>{const note=$('.media-fallback',el.closest('.bubble'));if(note)note.classList.remove('hidden');}));}
}
function renderDrafts() {const root=$('#draft-list');if(!root)return;root.innerHTML=S.drafts.map((m,i)=>`<div class="draft-row"><span>${i+1}. ${m.type==='text'?esc(m.text):`${labels[m.type]} · ${esc(S.assets.find(a=>a.id===m.asset_id)?.name||m.asset_id)}`}</span><button class="ghost small remove-draft" data-index="${i}" aria-label="移除第${i+1}条">✕</button></div>`).join('');$$('.remove-draft').forEach(b=>buttonAction(b,()=>{S.drafts.splice(Number(b.dataset.index),1);renderDrafts();}));}
async function chooseAsset() {
  S.assets=(await api('/api/assets')).assets;const assets=S.assets.filter(a=>a.enabled&&a.state==='sendable'&&a.channels.includes(S.detail.conversation.channel));
  modal('选择已审核素材',assets.length?`<div class="asset-picker">${assets.map(a=>`<button type="button" class="asset-choice" data-id="${esc(a.id)}"><span><strong>${esc(a.name)}</strong><small>${esc(a.purpose)}</small><small>${esc(a.id)} · ${bytes(a.size)}</small></span>${pill('',labels[a.kind])}</button>`).join('')}</div>`:'<p class="muted">当前渠道没有可发送且启用的素材。请在设置页上传并确认扫描状态及渠道授权。</p>',null);
  $$('.asset-choice').forEach(b=>buttonAction(b,()=>{const a=assets.find(x=>x.id===b.dataset.id);S.drafts.push({type:a.kind,text:null,asset_id:a.id});renderDrafts();$('#dialog').close();}));
}
function sendModal() {
  const messages=[...S.drafts],text=$('#message-text').value.trim();if(text)messages.push({type:'text',text,asset_id:null});if(!messages.length)throw new Error('请先输入文本或选择素材');
  const c=S.detail.conversation,cid=c.id;
  modal('确认发送到原会话',`<div class="notice"><div><strong>目标渠道：${esc(c.channel)}</strong><br>客户：${esc(c.user_id)}<br>会话：<span class="mono">${esc(c.platform_id)}</span></div></div>${messages.map((m,i)=>{const a=S.assets.find(a=>a.id===m.asset_id);return `<div class="draft-row"><span>${i+1}. ${m.type==='text'?esc(m.text):esc(a?.name||m.asset_id)}${a?.kind==='image'?`<img class="media-preview" src="${esc(a.preview_url)}" alt="素材预览">`:a?.kind==='video'?`<video class="media-preview" controls src="${esc(a.preview_url)}"></video>`:a?`<a class="attachment" href="${esc(a.preview_url)}">查看 ${esc(a.filename)}</a>`:''}</span>${pill('',labels[m.type]||'文本')}</div>`;}).join('')}<p class="warning-text">确认后进入人工模式，取消未发送 AI 任务。已提交到平台的请求无法撤回。共 ${messages.length} 条，按顺序逐条外发。</p>`,async()=>{const result=await api('/api/conversations/'+cid+'/messages',{method:'POST',body:{messages,confirm_send:true}});S.drafts=[];$('#message-text').value='';renderDrafts();toast(result.message);await refreshConversations();},'确认发送 '+messages.length+' 条');
}
function renderInspector(preserveScroll=false) {
  const taskList=$('#task-list'),taskScroll=preserveScroll?taskList?.scrollTop||0:0;
  const taskFocused=preserveScroll&&taskList===document.activeElement;
  const openSections=$$('#inspector details[open]').map(el=>$('summary',el)?.textContent);
  const d=S.detail,c=d.conversation;const generated=d.jobs.find(j=>j.kind==='generate'&&j.result?.plan);
  $('#inspector').innerHTML=`<div class="inspector-section"><h3>AI 助理 ${pill('',c.mode==='auto'?'自动回复':'人工回复')}</h3><p class="muted">${c.mode==='auto'?'已开启：收到新客户消息后直接回复，并携带本会话最近公开历史；不确定时如实说明。':'已关闭：新消息只进入历史，回复由人工发送。'}</p><div class="actions"><button id="toggle-ai" class="${c.mode==='auto'?'small':'primary'}">${c.mode==='auto'?'关闭 AI':'开启 AI'}</button></div>${generated?`<div class="job"><small>最近回复 · ${esc(generated.result.model)} · ${generated.result.elapsed_ms} ms</small><small>用量 ${generated.result.usage?.total_tokens??'未知'} tokens · 上下文 ${generated.result.context?.count??0}/${generated.result.context?.total??0}${generated.result.context?.truncated?' · 已截断':''}</small><small class="mono">${esc(generated.result.context?.first)} → ${esc(generated.result.context?.last)}</small></div>`:''}</div><div class="inspector-section"><h3>跟进工单</h3>${d.tickets.length?d.tickets.map(t=>`<p>${t.url?`<a href="${esc(t.url)}" target="_blank" rel="noopener noreferrer">Ticket #${esc(t.ticket_id)} ↗</a>`:pill(t.state)}<small> ${t.status?'状态 '+esc(t.status):''}</small></p>`).join(''):'<p class="muted">尚未关联工单</p>'}<div class="actions"><button id="create-ticket" class="small">创建 / 复用工单</button></div></div><div class="inspector-section"><h3>处理任务</h3><div id="task-list" class="task-list" role="region" aria-label="处理任务与运行记录" tabindex="0">${d.jobs.length?d.jobs.slice(0,20).map(jobHTML).join(''):'<p class="muted">暂无处理任务</p>'}<details class="job-details"><summary>精简运行记录</summary>${d.logs.map(l=>`<div class="log-row"><time>${fmtTime(l.created)}</time><br><strong>${esc(l.event)}</strong> · ${esc(l.detail)}</div>`).join('')||'<small>暂无记录</small>'}</details></div></div>`;
  $$('#inspector details').forEach(el=>{if(openSections.includes($('summary',el)?.textContent))el.open=true;});
  $('#task-list').scrollTop=taskScroll;
  if(taskFocused)$('#task-list').focus({preventScroll:true});
  buttonAction($('#toggle-ai'),async()=>{const mode=c.mode==='auto'?'manual':'auto';const result=await api('/api/conversations/'+c.id+'/mode',{method:'PUT',body:{mode}});toast(result.message);await refreshConversations();});
  buttonAction($('#create-ticket'),()=>modal('创建或复用跟进工单',`<label>跟进事项<textarea name="reason" required>${esc(generated?.result.plan.ticket_reason||'')}</textarea></label><label class="check-label"><input type="checkbox" name="new_matter">明确开启新的跟进事项，允许另建工单</label><small>默认返回当前会话已有工单。提交结果不明时不会自动重建。</small>`,async f=>{await api('/api/conversations/'+c.id+'/ticket',{method:'POST',body:{reason:f.get('reason'),new_matter:f.has('new_matter')}});await refreshConversations();toast('工单任务已提交或已有记录已复用');},'确认'));
  $$('.verify-job').forEach(b=>buttonAction(b,()=>verifyModal(b.dataset.id)));
  $$('.resolve-job').forEach(b=>buttonAction(b,()=>resolveModal(b.dataset.id)));
}
function jobHTML(j) {const type=j.kind==='send'?(labels[j.payload?.type]||'文本')+' #'+(j.seq+1):({generate:j.origin==='preview'?'AI 预览':'AI 自动生成',sync:'历史同步',activate_test:'启动账号测试',ticket:'创建工单'}[j.kind]||j.kind);
  return `<div class="job"><div class="job-top"><strong>${esc(type)}</strong>${pill(j.state)}</div><small>${fmtTime(j.created)} · 尝试 ${j.attempts} 次</small>${j.platform_id?`<small class="mono">平台 ID：${esc(j.platform_id)}</small>`:''}${j.result?.send_queue_to_acceptance_ms!==undefined?`<small>发送排队至受理 ${j.result.send_queue_to_acceptance_ms} ms${j.result.inbound_to_acceptance_ms!==null?' · 入站至受理 '+j.result.inbound_to_acceptance_ms+' ms':''}</small>`:''}${j.error?`<small class="error-text">${esc(j.error)}</small>`:''}${j.result?.verification?'<small>✓ 测试人员已确认工作台与客户收到（非机器回执）</small>':''}${j.kind==='send'?`<details class="job-details"><summary>查看发送内容</summary><pre>${esc(JSON.stringify(j.payload,null,2))}</pre></details>`:''}<div class="actions">${j.kind==='send'&&j.state==='accepted'?`<button class="small verify-job" data-id="${esc(j.id)}">核对工作台 / 客户接收</button>`:''}${['failed','unknown','paused'].includes(j.state)?`<button class="small resolve-job" data-id="${esc(j.id)}">人工处理</button>`:''}</div></div>`;
}
function verifyModal(id) {modal('记录原渠道接收证据',`<p class="muted">请在真实 Omni 工作台和客户设备核对，不可仅凭此 Demo 页面确认。</p><label class="check-label"><input type="checkbox" name="omni_visible" required>Omni 原会话时间线可见，发送 Agent 正确</label><label class="check-label"><input type="checkbox" name="customer_received" required>客户原渠道实际收到，文本 / 媒体可打开</label><label>脱敏证据<textarea name="evidence" required placeholder="测试时间、测试人、截图/证据位置；勿填写密钥或隐私内容。"></textarea></label>`,async f=>{await api('/api/checks',{method:'POST',body:{kind:'verify_outbound',job_id:id,omni_visible:f.has('omni_visible'),customer_received:f.has('customer_received'),evidence:f.get('evidence')}});await refreshConversations();toast('三层验收证据已记录；人工确认与机器回执分别显示');},'保存核验');}
function resolveModal(id) {const j=S.detail.jobs.find(x=>x.id===id);modal('人工处理任务',`<p class="warning-text">结果不明时，平台可能已创建消息或工单。请先核实，不以相同文字或近似时间推断归属。</p><label>处理方式<select name="action"><option value="cancel">确认取消 / 保留记录</option><option value="retry">人工确认后重试当前条</option>${j.kind==='ticket'?'<option value="link_existing">关联已核实的真实工单</option>':''}</select></label>${j.kind==='ticket'?'<label>已核实工单编号<input name="platform_id"></label>':''}<label>处理依据<textarea name="evidence" required></textarea></label><label class="check-label"><input type="checkbox" name="ack_duplicate_risk">如选择重试，我已核实并接受重复外发风险</label>`,async f=>{await api('/api/jobs/'+id+'/resolve',{method:'POST',body:{...Object.fromEntries(f),ack_duplicate_risk:f.has('ack_duplicate_risk')}});await refreshConversations();toast('人工处理已记录');},'确认处理');}
boot().catch(e=>{$('#app').textContent='应用加载失败：'+e.message;});
