<script setup lang="ts">
// 文件职责：社区主页面：账号登录、角色与动态、聊天列表、消息发送及 WebSocket 恢复。
import { computed, reactive, ref, onUnmounted } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, baseURL, configureServer, csrfToken, sessionTokens, uploadImage } from '../../api'
import { useSession } from '../../store'
import PrivateImage from '../../components/PrivateImage.vue'
import AgentWorkbench from '../../components/AgentWorkbench.vue'
import ConversationAgent from '../../components/ConversationAgent.vue'

// mapLinks：从消息正文提取并去重 OpenStreetMap 链接。
function mapLinks(body:string){return [...new Set((body.match(/https:\/\/www\.openstreetmap\.org\/[^\s]+/g)||[]) as string[])]}
// openMap：按 H5/App 平台打开地图；浏览器使用 noopener 隔离新窗口。
function openMap(url:string){
  // #ifdef H5
  window.open(url,'_blank','noopener,noreferrer')
  // #endif
  // #ifdef APP-PLUS
  plus.runtime.openURL(url)
  // #endif
}
// copyToClipboard：调用 uni-app 跨平台剪贴板接口。
function copyToClipboard(options:any){uni.setClipboardData(options)}
// showDialog：调用跨平台模态对话框，供确认操作复用。
function showDialog(options:any){uni.showModal(options)}
// 共享 store 保存账号和当前发言角色；本页面的 ref/reactive 保存各列表和输入草稿。
const session = useSession()
const tab = ref('社区')
const tabs = ['社区', '角色广场', '消息', '我的角色', '模型与设置']
const error = ref(''), busy = ref(false), registering = ref(false), loading = ref(true)
const auth = reactive({ username: '', password: '', nickname: '', invite_code: '' })
const server = ref(baseURL)
const posts = ref<any[]>([]), directory = ref<any[]>([]), connections = ref<any[]>([]), conversations = ref<any[]>([]), runs = ref<any[]>([])
// next 游标为 null 表示无下一页；搜索词需 URL 编码后发送。
const postNext = ref<number|null>(null), roleNext = ref<number|null>(null), search = ref('')
const composer = ref(''), postAsset = ref(''), messageText = ref(''), chatAsset = ref('')
const comments = reactive<Record<string, any[]>>({}), commentText = reactive<Record<string, string>>({})
// 当前会话、已加载消息和后台生成状态分开保存，流式任务状态不等同已发布消息。
const chat = ref<any>(null), messages = ref<any[]>([]), currentRun = ref<any>(null)
const connectionIndex = ref(0), editRole = ref<any>(null)
const connectionDraft = reactive({ provider: 'mock', model: 'demo-role', api_key: '' })
const providers = ['mock', 'ollama', 'deepseek', 'dashscope']
const profile = reactive({ nickname: '', bio: '', budget: 100000 })
// socket 为当前连接，reconnect 为重连定时器；pendingId 保证发送重试幂等，mounted 防止卸载后回写。
let socket: any = null, reconnect: any = null, pendingId = '', mounted = true
const currentRole = computed(() => session.currentRole)
const chatSender = computed(() => session.roles.find(r => r.id === chat.value?.source_role_id || r.id === chat.value?.target_role_id)?.name || '当前会话角色')

const translations: Record<string,string> = {
  AUTH_REQUIRED: '登录已过期，请重新登录。', INVALID_CREDENTIALS: '账号或密码不正确。', INVITE_INVALID: '邀请码不正确。',
  CONNECTION_REQUIRED: '请先在模型与设置中添加自己的模型连接。', BUDGET_EXCEEDED: '额度不足，请调整你的总 token 预算。',
  AI_UNAVAILABLE: '该角色尚未允许 AI 对话，或已关闭 AI。', ROLE_LIMIT: '最多创建 5 个角色。',
  RUN_IN_PROGRESS: 'AI 正在回复，请等待或停止当前生成。', VERSION_CONFLICT: '角色已经更新，请重新打开后编辑。',
  NOT_FOUND: '内容不存在或你没有访问权限。', PUBLIC_ROLE_REQUIRED: '请先将当前角色设为公开。',
  PROVIDER_SCOPE_MISMATCH: '角色不允许向该供应商提供设定，请换一个模型连接。', INTERACTION_BLOCKED: '此互动已被屏蔽。',
  MOCK_DISABLED: '服务器未开启演示模式，请使用真实模型连接。', REGISTRATION_UNAVAILABLE: '账号已占用或不可注册。',
}
// runFailure：将任务错误和上下文占用诊断转成针对性的中文提示。
function runFailure(code: string, usage?: any) {
  if (code === 'MEMORY_CONTEXT_LIMIT') {
    if (!usage) return '请求上下文超限，请检查角色设定、聊天记忆和工具结果；旧任务没有容量明细，请重新发送。'
    const labels: Record<string,string> = {persona_rules:'角色设定及系统规则',memory:'长期记忆',dialogue:'近期消息',tool_results:'工具结果',tool_definitions:'工具描述'}
    const tips: Record<string,string> = {persona_rules:'请精简角色设定后新建会话，或由管理员调整模型上下文。清空记录不会缩短人设。',memory:'可清空聊天记忆或新建会话，或由管理员调整模型上下文。',dialogue:'请缩短单条消息，或由管理员调整模型上下文。',tool_results:'请缩小查询范围或减少本轮工具结果。',tool_definitions:'请关闭不需要的会话工具。'}
    const parts = Object.entries(usage.parts || {}).map(([k,v])=>`${labels[k] || k}约 ${v}`).join('，')
    return `上下文需要约 ${usage.required_tokens} token，上限 ${usage.limit_tokens}（输入 ${usage.input_tokens}，回复预留 ${usage.output_tokens}，安全余量 ${usage.margin_tokens}）。${parts}。${tips[usage.dominant] || ''}${usage.method==='byte_upper_bound'?'当前模型未匹配分词器，使用字节上界保守估算。':''}`
  }
  return ({ MEMORY_STORAGE_FAILED: '聊天记忆文件暂时无法读取，请检查服务器磁盘或稍后重试。', MEMORY_CONTEXT_LIMIT: '完整长期记忆及近期消息超过本地模型上下文容量，请清空记录、新建会话或换用更大上下文的模型。', MEMORY_SUMMARY_FAILED: '长期记忆摘要生成失败，请稍后重试。', RUN_BUDGET_LIMIT: '本轮上下文、记忆摘要与工具调用超过预算，请在会话设置中适当提高每轮 token 上限，或新建会话。', TOOL_BUDGET_EXCEEDED: '本轮工具次数已用完，请调整会话工具次数。', MCP_TOOL_FAILED: '外部工具调用失败，请稍后重试。', EMPTY_RESPONSE: '模型没有返回有效内容。', CONSENT_REVOKED: '角色授权已变更，请检查角色设置。', SESSION_CHANGED: '会话设置已变更，请重新发送。' } as Record<string,string>)[code] || '生成未完成，请稍后重试；如持续发生，请检查运行记录。' }
// tell：把接口异常翻译为用户提示，并写入页面错误状态。
function tell(e: any) { error.value = translations[e.message] || e.message || '请求失败，请检查服务连接。' }
// act：防止重复提交，统一设置忙碌/错误状态，并在 finally 恢复按钮可用性。
async function act(fn: () => Promise<any>) { if (busy.value) return; busy.value = true; error.value = ''; try { await fn() } catch(e) { tell(e) } finally { busy.value = false } }
// syncAccount：刷新账号、本人角色和连接；修正失效的选中角色并优先选择本地连接。
async function syncAccount() {
  session.account = await api('/me')
  Object.assign(profile, session.account)
  session.roles = await api('/me/roles')
  if (!session.roles.some(r => r.id === session.activeRole)) session.activeRole = session.roles[0]?.id || ''
  connections.value = await api('/me/connections'); const local = connections.value.findIndex(c => c.provider === 'ollama'); if (local >= 0) connectionIndex.value = local
}
// loadTab：按当前导航页加载所需数据，不一次性请求全部业务列表。
async function loadTab() {
  if (tab.value === '社区') { const r = await api('/posts'); posts.value = r.items; postNext.value = r.next }
  if (tab.value === '角色广场') { const r = await api('/roles?q=' + encodeURIComponent(search.value)); directory.value = r.items; roleNext.value = r.next }
  if (tab.value === '消息') conversations.value = await api('/me/conversations')
  if (tab.value === '模型与设置') { await syncAccount(); runs.value = await api('/me/runs') }
}
// more：使用 before 游标追加动态或角色搜索结果，并更新下一页游标。
async function more(kind: string) {
  const isPost = kind === 'post'; const cursor = isPost ? postNext.value : roleNext.value
  if (cursor === null) return
  const r = await api((isPost ? '/posts' : '/roles') + '?before=' + cursor + (isPost ? '' : '&q=' + encodeURIComponent(search.value)))
  if (isPost) { posts.value.push(...r.items); postNext.value = r.next } else { directory.value.push(...r.items); roleNext.value = r.next }
}
// navigate：切换导航标签并通过统一异步入口加载数据。
async function navigate(name: string) { tab.value = name; await act(loadTab) }
// login：根据注册开关选择接口，保存令牌后清除密码输入并加载账号页面。
async function login() {
  await act(async () => {
    const r = await api(registering.value ? '/auth/register' : '/auth/login', 'POST', registering.value ? auth : { username: auth.username, password: auth.password })
    sessionTokens(r.access_token, r.csrf_token); auth.password = ''; await syncAccount(); await loadTab()
  })
}
// logout：撤销服务端登录，关闭消息连接并清空前端会话状态。
async function logout() { await act(async () => { await api('/auth/logout', 'POST'); closeSocket(); sessionTokens(); session.$reset(); chat.value = null; messages.value = []; posts.value = []; directory.value = [] }) }
// chooseRole：更新发言角色并清空动态草稿；不会改变已打开聊天的固定身份。
function chooseRole(e: any) { session.activeRole = session.roles[Number(e.detail.value)]?.id || ''; composer.value = ''; postAsset.value = '' }
// newRole：初始化私有、AI 关闭的新角色编辑草稿。
function newRole() { editRole.value = { name: '', bio: '', persona: '温和、真诚，承认自己是 AI，不编造现实经历。', visibility: 'PRIVATE', ai_enabled: false, public_ai: false, provider_scope: [...providers] } }
// openEdit：复制角色和提供商列表到编辑草稿，避免未保存修改污染原列表。
function openEdit(role: any) { editRole.value = { ...role, provider_scope: [...role.provider_scope] } }
// saveRole：提交新角色或带版本的编辑请求，然后刷新本人角色列表。
async function saveRole() { await act(async () => {
  const { id, name, bio, persona, visibility, ai_enabled, public_ai, provider_scope, version } = editRole.value
  await api(id ? '/roles/' + id : '/me/roles', id ? 'PATCH' : 'POST', { name, bio, persona, visibility, ai_enabled, public_ai, provider_scope, ...(id ? { version } : {}) })
  editRole.value = null; await syncAccount()
}) }
// confirm：把模态框结果封装成 Promise，供需要确认的操作等待用户选择。
function confirm(title: string): Promise<boolean> { return new Promise(resolve => showDialog({ title, content: '该操作会改变角色或内容的可用状态。', success: r => resolve(r.confirm) })) }
// removeRole：确认后调用角色删除接口并刷新列表。
async function removeRole(r: any) { if (await confirm('删除角色「' + r.name + '」？')) await act(async () => { await api('/roles/' + r.id, 'DELETE'); await syncAccount() }) }
// publish：以当前角色发表动态，可附图片；成功后清理输入并刷新动态。
async function publish() { await act(async () => { if (!currentRole.value) throw new Error('请先创建一个角色。'); await api('/posts', 'POST', { role_id: session.activeRole, body: composer.value, asset_id: postAsset.value || null }); composer.value = ''; postAsset.value = ''; await loadTab() }) }
// toggleComments：展开时请求评论，已展开时移除本地评论列表以收起。
async function toggleComments(p: any) { await act(async () => { if (comments[p.id]) delete comments[p.id]; else comments[p.id] = await api('/posts/' + p.id + '/comments') }) }
// reply：以当前角色发表评论，成功后清空该动态输入并刷新评论。
async function reply(p: any) { await act(async () => { if (!session.activeRole) throw new Error('请先创建角色。'); await api('/posts/' + p.id + '/comments', 'POST', { role_id: session.activeRole, body: commentText[p.id] || '' }); commentText[p.id] = ''; comments[p.id] = await api('/posts/' + p.id + '/comments') }) }
// removePost：确认后删除动态并刷新当前页面数据。
async function removePost(p: any) { if (await confirm('删除这条动态？')) await act(async () => { await api('/posts/' + p.id, 'DELETE'); await loadTab() }) }
// startChat：用当前角色创建真人或 AI 会话；AI 场景选择自己的模型连接。
async function startChat(role: any, kind: string) { await act(async () => {
  if (!session.activeRole) throw new Error('请先创建一个角色，作为发言身份。')
  const c = await api('/conversations', 'POST', { kind, source_role_id: session.activeRole, target_role_id: role.id, connection_id: kind === 'AI_DIRECT' ? connections.value[connectionIndex.value]?.id || null : null })
  tab.value = '消息'; conversations.value = await api('/me/conversations'); await openChat(c)
}) }
// closeSocket：取消重连定时器并关闭旧 WebSocket，防止旧连接继续更新页面。
function closeSocket() { if (reconnect) clearTimeout(reconnect); reconnect = null; const previous = socket; socket = null; previous?.close() }
// removeFromList：移除指定会话；若正打开它，同步清理消息、附件、任务和连接状态。
function removeFromList(id:string) {
  conversations.value = conversations.value.filter(c => c.id !== id)
  if (chat.value?.id === id) {
    closeSocket(); chat.value = null; messages.value = []; currentRun.value = null
    messageText.value = ''; chatAsset.value = ''; pendingId = ''
  }
}
// deleteConversation：展示真人/AI 不同清理范围，确认后删除会话并更新本地列表。
async function deleteConversation(selected:any) {
  if (busy.value) return
  const accepted = await new Promise<boolean>(resolve => showDialog({
    title: '删除与「' + selected.title + '」的会话？',
    content: selected.kind === 'AI_DIRECT'
      ? '将删除此 AI 会话的消息和长期记忆，并从列表移除；保留角色。此操作不可恢复。'
      : '将清空你这边的聊天记录文件并移除会话，不影响对方。收到新消息或重新发起聊天后重新记录。',
    confirmText: '删除会话', confirmColor: '#a2493f',
    success: (r:any) => resolve(Boolean(r.confirm)), fail: () => resolve(false),
  }))
  if (!accepted) return
  await act(async () => {
    await api('/conversations/' + selected.id, 'DELETE')
    removeFromList(selected.id)
    uni.showToast({ title: '会话已删除', icon: 'success' })
  })
}
// clearChat：确认后清空当前聊天，采用服务端清空序号过滤消息，防止旧快照回填。
async function clearChat() {
  const selected = chat.value
  if (!selected || busy.value) return
  const accepted = await new Promise<boolean>(resolve => showDialog({
    title: '清空当前聊天记录？',
    content: selected.kind === 'AI_DIRECT'
      ? '将删除本会话全部消息并清除会话记忆，AI 将从新的对话开始。角色设定保留，此操作不可恢复。'
      : '只清空你这边的聊天记录，对方的记录不受影响。清空后无法在你的会话中查看这些旧消息。',
    confirmText: '确认清空', confirmColor: '#a2493f',
    success: (r:any) => resolve(Boolean(r.confirm)), fail: () => resolve(false),
  }))
  if (!accepted) return
  await act(async () => {
    const result = await api('/conversations/' + selected.id + '/messages', 'DELETE')
    if (chat.value?.id === selected.id) {
      chat.value.cleared_seq = result.cleared_seq
      messages.value = messages.value.filter(m => m.seq > result.cleared_seq)
      currentRun.value = null
      pendingId = ''
    }
    conversations.value = await api('/me/conversations')
    uni.showToast({ title: '聊天记录已清空', icon: 'success' })
  })
}
// connectChat：先申请一次性票据再建立 WebSocket；忽略旧连接事件并在断线后尝试重连。
async function connectChat(id: string) {
  const r = await api('/conversations/' + id + '/socket-ticket', 'POST')
  if (!mounted || chat.value?.id !== id) return
  let url = baseURL
  // #ifdef H5
  if (!url) url = window.location.origin
  // #endif
  const currentSocket = uni.connectSocket({ url: url.replace(/^http/, 'ws') + '/ws?ticket=' + r.ticket, complete: () => {} })
  socket = currentSocket
  currentSocket.onMessage((event: any) => { if (socket !== currentSocket || chat.value?.id !== id) return; const data = JSON.parse(event.data); if (data.type === 'conversation_removed') { removeFromList(id); return }; if (data.type === 'snapshot') {
    if ((data.cleared_seq || 0) < (chat.value.cleared_seq || 0)) return
    chat.value.cleared_seq = data.cleared_seq || 0
    // 按消息 ID 去重再按 seq 排序；始终过滤清空边界之前的数据，避免重连快照恢复旧历史。
    const map = new Map(messages.value.filter(m => m.seq > chat.value.cleared_seq).map(m => [m.id, m])); data.messages.forEach((m: any) => { if (m.seq > chat.value.cleared_seq) map.set(m.id, m) }); messages.value = [...map.values()].sort((a:any,b:any) => a.seq-b.seq)
    currentRun.value = data.run
    api('/conversations/' + id + '/read', 'PUT').catch(() => {})
  } })
  currentSocket.onClose(() => { if (socket === currentSocket && mounted && chat.value?.id === id) reconnect = setTimeout(() => connectChat(id).catch(tell), 2000) })
}
// olderMessages：按最早序号补拉历史，合并时排除已清空和已存在的消息。
async function olderMessages() { const id = chat.value.id; const older = await api('/conversations/' + id + '/messages?before=' + messages.value[0].seq); if (chat.value?.id === id) messages.value.unshift(...older.filter((m:any) => m.seq > (chat.value.cleared_seq || 0) && !messages.value.some(x => x.id === m.id))) }
// openChat：关闭旧连接并重置输入，加载所选会话历史后建立实时连接。
async function openChat(c: any) { closeSocket(); chat.value = c; messageText.value = ''; chatAsset.value = ''; pendingId = ''; currentRun.value = null; messages.value = await api('/conversations/' + c.id + '/messages'); await connectChat(c.id) }
// send：同一发送失败重试沿用 pendingId；成功后清空输入并通过 HTTP 补拉新消息。
async function send() { await act(async () => {
  if (!chat.value) return
  if (!pendingId) pendingId = Date.now() + '-' + Math.random().toString(36).slice(2)
  await api('/conversations/' + chat.value.id + '/messages', 'POST', { body: messageText.value, client_message_id: pendingId, asset_id: chatAsset.value || null })
  messageText.value = ''; chatAsset.value = ''; pendingId = ''
  // DB remains authoritative even if WebSocket is reconnecting.
  const fresh = await api('/conversations/' + chat.value.id + '/messages?after=' + (messages.value.at(-1)?.seq || 0)); messages.value.push(...fresh.filter((m:any) => !messages.value.some(x => x.id === m.id)))
}) }
// saveConnection：提交模型配置，成功后清除输入框 Key 并刷新连接列表。
async function saveConnection() { await act(async () => { await api('/me/connections', 'POST', connectionDraft); connectionDraft.api_key = ''; await syncAccount() }) }
// attach：按聊天或动态场景上传图片，将返回 ID 保存到对应草稿。
async function attach(kind: string) { await act(async () => { const id = await uploadImage(kind === 'chat' ? chat.value?.id : undefined); if (kind === 'chat') chatAsset.value = id; else postAsset.value = id }) }
// timeLabel：把服务端秒级时间戳转为本地中文展示时间。
function timeLabel(t: number) { return new Date(t * 1000).toLocaleString('zh-CN', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }) }
// 页面出现时尝试恢复登录并加载数据；Cookie 可刷新访问令牌，但密码不会保存在页面。
onShow(async () => {
  if (session.account) { await loadTab().catch(tell); return }
  try {
    // #ifdef H5
    const csrf = document.cookie.split('; ').find(x => x.startsWith('hai_csrf='))?.split('=')[1]
    if (csrf) { sessionTokens('', decodeURIComponent(csrf)); const r = await api('/auth/refresh', 'POST'); sessionTokens(r.access_token, r.csrf_token) }
    // #endif
    await syncAccount(); await loadTab()
  } catch { sessionTokens(); session.$reset() } finally { loading.value = false }
})
onUnmounted(() => { mounted = false; closeSocket() })
</script>

<template>
  <view v-if="!session.account" class="welcome">
    <view class="welcome-story"><text class="eyebrow">HUMAN / AI / TOGETHER</text><text class="welcome-title">每一种你，<br/>都值得被遇见。</text><text class="lead">在共栖，用不同的角色表达自己。<br/>与真人交流，也与有个性的 AI 相遇。</text><view class="orb">共<text>栖</text></view><text class="subtle">独立身份 · 私密对话 · 清楚的 AI 边界</text></view>
    <view class="auth-card"><view class="brand">◉ 共栖 <text>R3</text></view><text class="section-title">{{ registering ? '开始你的共栖旅程' : '欢迎回来' }}</text><text class="muted">一个账号，多种表达。先从真实的你开始。</text>
      <view class="form-stack"><input v-model="auth.username" placeholder="账号（至少 3 位）"/><input v-model="auth.password" password placeholder="密码（注册至少 10 位）" @confirm="login"/><input v-if="registering" v-model="auth.nickname" placeholder="你的昵称"/><input v-if="registering" v-model="auth.invite_code" placeholder="邀请码"/>
      <!-- #ifndef H5 --><input v-model="server" placeholder="服务器 HTTPS 地址" @blur="configureServer(server)"/><!-- #endif -->
      <text v-if="error" class="error">{{ error }}</text><button class="primary" :disabled="busy" @click="login">{{ busy ? '请稍候…' : registering ? '创建账号' : '进入共栖' }}</button><button class="text-button" @click="registering = !registering; error = ''">{{ registering ? '已有账号？登录' : '第一次来？使用邀请码注册' }}</button></view>
      <text class="fine-print">小圈子技术预览 · AI 内容始终带有标识</text>
    </view>
  </view>
  <view v-else class="app-shell">
    <view class="sidebar"><view class="brand">◉ 共栖<text>HUMAN AI</text></view><text class="nav-label">你的社交空间</text><view class="nav-list"><view v-for="(name,i) in tabs" :key="name" class="nav-item" :class="{selected:tab===name}" @click="navigate(name)"><text class="nav-icon">{{ ['◌','✧','☷','▧','⚙'][i] }}</text><text>{{ name }}</text><text v-if="tab===name" class="nav-dot">●</text></view></view>
      <view class="identity"><text class="nav-label">当前发言身份</text><picker :range="session.roles" range-key="name" @change="chooseRole"><view class="identity-pick"><view class="avatar">{{ currentRole?.name?.slice(0,1) || '+' }}</view><view><text class="bold">{{ currentRole?.name || '尚未创建角色' }}</text><text class="small muted">点击切换角色 ↕</text></view></view></picker><text class="small muted">切换角色不会改变已打开会话的身份。</text></view>
      <view class="account-foot"><text>{{ session.account.nickname }}</text><text class="link" @click="logout">退出</text></view>
    </view>
    <view class="main"><view class="topbar"><view><text class="eyebrow">YOUR SHARED SPACE</text><text class="page-title">{{ tab }}</text></view><view class="status"><text class="dot">●</text> 人与 AI，共同在场</view></view>
      <view v-if="error" class="error-banner" @click="error=''">{{ error }} <text> ×</text></view>
      <view v-if="!session.roles.length" class="notice">你还没有角色。<text class="link" @click="tab='我的角色'; newRole()"> 创建第一个角色 →</text></view>

      <view v-if="tab==='社区'" class="community-grid"><view class="feed">
        <view class="card composer"><view class="row"><view class="avatar">{{ currentRole?.name?.slice(0,1) || '?' }}</view><text class="bold">{{ currentRole?.name || '选择一个角色' }}</text><text class="pill">真人发布</text></view><textarea v-model="composer" maxlength="5000" placeholder="此刻，有什么想和大家分享？"/><PrivateImage v-if="postAsset" :id="postAsset"/><view class="row between"><button class="text-button" @click="attach('post')">＋ 添加图片</button><button class="primary" :disabled="busy || !composer.trim()" @click="publish">发布动态 ↗</button></view></view>
        <view class="feed-heading"><text class="bold">社区新鲜事</text><text class="small muted">真实表达，自然连接</text></view>
        <view v-if="!posts.length" class="card empty"><text class="empty-symbol">◌</text><text>这里还很安静</text><text class="muted small">分享一条动态，开启第一段连接。</text></view>
        <view v-for="p in posts" :key="p.id" class="card post"><view class="row"><view class="avatar peach">{{ p.role.name.slice(0,1) }}</view><view class="grow"><text class="bold">{{ p.role.name }}</text><text class="small muted">{{ timeLabel(p.created_at) }} · {{p.actor==='AI'?'AI':'真人'}}</text></view><text v-if="p.mine" class="link small" @click="removePost(p)">删除</text></view><text class="post-body">{{ p.body }}</text><PrivateImage v-if="p.asset_id" :id="p.asset_id"/><text v-if="p.actor==='AI'" class="pill ai-pill">AI 发布</text><text class="link small" @click="copyToClipboard({data:p.id})">复制动态 ID</text><view class="post-actions"><text @click="act(async()=>{await api('/posts/'+p.id+'/like','PUT'); await loadTab()})">♡ {{ p.likes || '喜欢' }}</text><text @click="toggleComments(p)">○ 评论</text></view><view v-if="comments[p.id]" class="comments"><view v-for="c in comments[p.id]" :key="c.id" class="comment"><text v-if="c.actor==='AI'" class="pill">AI</text><text v-if="c.actor==='AI'" class="link small" @click="act(async()=>{if(!connections[connectionIndex])throw Error('请先配置自己的模型连接');await api('/comments/'+c.id+'/ai-reply','POST',{role_id:session.activeRole,body:commentText[p.id]||'',connection_id:connections[connectionIndex].id,client_message_id:'reply-'+Date.now()});commentText[p.id]='';comments[p.id]=await api('/posts/'+p.id+'/comments')})">将下方输入直接回复此 AI（使用我的连接与额度）</text><text class="bold">{{ c.role_name }} </text><text>{{ c.body }}</text><text v-if="c.mine" class="link small" @click="act(async()=>{await api('/comments/'+c.id,'DELETE'); comments[p.id]=await api('/posts/'+p.id+'/comments')})"> · 删除</text></view><view class="row"><input v-model="commentText[p.id]" placeholder="留下你的回应"/><button @click="reply(p)" :disabled="busy">回复</button></view></view></view>
        <button v-if="postNext" @click="act(()=>more('post'))">更多动态</button>
      </view><view class="aside"><view class="feature-card"><text class="eyebrow">MORE THAN ONE YOU</text><text class="feature-title">换个角色，<br/>发现另一种连接。</text><text class="muted">你决定如何表达，也决定何时让 AI 扮演你的角色。</text><button @click="navigate('角色广场')">去认识新角色 ↗</button><view class="mini-orbits">✧ <text>◌</text> ✳</view></view><view class="card guidelines"><text class="bold">在共栖，边界很清楚</text><text>01　真人与 AI 分开交流</text><text>02　你的 AI 私聊只属于你</text><text>03　每次调用由自己掌握额度</text></view></view></view>

      <view v-if="tab==='角色广场'"><view class="toolbar"><input v-model="search" placeholder="搜索公开角色" @confirm="act(loadTab)"/><button @click="act(loadTab)">搜索</button></view><view class="notice">与 AI 对话使用你的模型连接。<picker :range="connections" range-key="model" @change="connectionIndex=Number($event.detail.value)"><text class="link">当前：{{ connections[connectionIndex]?.model || '请先添加连接' }} ↕</text></picker></view><view class="role-grid"><view v-for="r in directory" :key="r.id" class="card role-card"><view class="avatar large">{{ r.name.slice(0,1) }}</view><text class="section-title">{{ r.name }}</text><text class="muted role-bio">{{ r.bio || '还没有填写介绍，先打个招呼吧。' }}</text><view class="row"><text class="pill">公开角色</text><text class="link small" @click="copyToClipboard({data:r.id})">复制角色 ID</text><text v-if="r.ai_enabled && r.public_ai" class="pill ai-pill">AI 已开启</text></view><view class="role-buttons"><button @click="startChat(r,'HUMAN_DIRECT')">与真人聊</button><button class="primary" :disabled="!r.ai_enabled || !r.public_ai || busy" @click="startChat(r,'AI_DIRECT')">与 AI 聊 ✧</button></view><button class="text-button" @click="act(()=>api('/roles/'+session.activeRole+'/reception','PUT',{sender_role_id:r.id,enabled:true}))">允许该 AI 主动私聊</button><text class="link small" @click="act(()=>api('/roles/'+session.activeRole+'/reception','PUT',{sender_role_id:r.id,enabled:false}))">撤回接收许可</text><text class="small muted link" @click="act(async()=>{await api('/roles/'+r.id+'/block','PUT'); await loadTab()})">屏蔽该账号的角色</text></view></view><view v-if="!directory.length" class="empty">尚无匹配的公开角色</view><button v-if="roleNext" @click="act(()=>more('role'))">加载更多角色</button></view>

      <view v-if="tab==='消息'" class="chat-layout"><view class="card conversation-list"><text class="bold">你的会话</text><view v-for="c in conversations" :key="c.id" :data-conversation-id="c.id" class="conversation" :class="{active:chat?.id===c.id}" @click="act(()=>openChat(c))"><view class="avatar small-avatar">{{ c.title.slice(0,1) }}</view><view class="grow"><text class="bold">{{ c.title }}</text><text class="small muted">{{ c.kind==='AI_DIRECT' ? '✧ AI 私密会话' : '真人对话' }}</text></view><text v-if="c.unread" class="badge">{{ c.unread }}</text><button class="text-button delete-conversation" :disabled="busy" @click.stop="deleteConversation(c)">删除</button></view><text v-if="!conversations.length" class="small muted">去角色广场开始一段对话。</text></view><view class="card chat-panel"><template v-if="chat"><view class="chat-heading"><text class="bold">{{ chat.title }}</text><text class="pill" :class="{'ai-pill':chat.kind==='AI_DIRECT'}">{{ chat.kind==='AI_DIRECT' ? 'AI 分身' : '真人' }}</text><text class="small muted">以 {{ chatSender }} 发言</text><button class="text-button clear-chat" :disabled="busy" @click="clearChat">清空记录</button></view><view v-if="chat.kind==='AI_DIRECT'" class="chat-privacy">角色拥有者无法查看或接管此会话。每 5 条消息生成一句长期记忆；回复结合本条消息、此前 4 条消息及长期记忆，摘要计入你的模型用量。</view><ConversationAgent v-if="chat.kind==='AI_DIRECT'" :key="chat.id" :chat="chat" :connections="connections" :run="currentRun"/><view v-else class="chat-privacy"><text>真人私聊学习需双方分别授权，本设置只允许之后的记录。</text><view v-for="rid in [chat.source_role_id,chat.target_role_id]" :key="rid"><text>角色 {{rid.slice(0,8)}}</text><button class="text-button" @click="act(()=>api('/conversations/'+chat.id+'/learning-consent','PUT',{role_id:rid,enabled:true,providers:[connections[connectionIndex]?.provider||'mock']}))">允许该角色学习我的后续对话</button><button class="text-button" @click="act(()=>api('/conversations/'+chat.id+'/learning-consent','PUT',{role_id:rid,enabled:false,providers:[]}))">撤回许可</button></view></view><scroll-view scroll-y class="message-scroll" :scroll-into-view="'end-'+messages.length"><button v-if="messages.length && messages[0].seq>(chat.cleared_seq || 0)+1" class="text-button" @click="act(olderMessages)">加载更早消息</button><view v-for="m in messages" :key="m.id" class="message" :class="{agent:m.actor==='AI',own:m.actor!=='AI' && m.role_id===session.roles.find(r=>r.id===chat.source_role_id||r.id===chat.target_role_id)?.id}"><text class="message-label">{{ m.actor==='AI' ? '✧ AI' : session.roles.some(r=>r.id===m.role_id) ? '你 · 真人' : '对方 · 真人' }}</text><view class="bubble"><text>{{ m.body }}</text><button v-for="url in mapLinks(m.body)" :key="url" class="text-button" @click="openMap(url)">打开地图 ↗</button><PrivateImage v-if="m.asset_id" :id="m.asset_id"/></view><text v-if="m.actor==='HUMAN' && session.roles.some(r=>r.id===m.role_id)" class="retract" @click="act(()=>api('/messages/'+m.id+'/retract','POST'))">撤回</text></view><view v-if="currentRun && ['QUEUED','RUNNING','CANCEL_REQUESTED'].includes(currentRun.status)" class="message agent"><text class="message-label">✧ AI · {{ currentRun.status==='QUEUED' ? '排队中' : '生成中' }}</text><view class="bubble"><text>{{ currentRun.partial || '正在准备回复…' }}</text></view><text class="link small" @click="act(()=>api('/runs/'+currentRun.id+'/cancel','POST'))">停止生成</text></view><text v-if="currentRun && ['FAILED','UNKNOWN_USAGE','CANCELLED'].includes(currentRun.status)" class="error">本次生成已停止（{{ currentRun.status }}）。{{ runFailure(currentRun.error, currentRun.context_usage) }}</text><view :id="'end-'+messages.length" style="height:4px"/></scroll-view><view class="chat-compose"><text v-if="chatAsset" class="small">已附图片（仅在会话授权识图后允许模型读取）</text><textarea v-model="messageText" maxlength="4000" placeholder="说点什么吧…"/><view class="row between"><button class="text-button" @click="attach('chat')">＋ 图片</button><button class="primary" :disabled="busy || (!messageText.trim()&&!chatAsset)" @click="send">发送 ↗</button></view></view></template><view v-else class="empty"><text class="empty-symbol">☷</text><text>选择一段会话</text><text class="small muted">真人与 AI 对话各自独立。</text></view></view></view>

      <view v-if="tab==='我的角色'"><view class="toolbar between"><text class="muted">每一种表达，都由你决定。{{ session.roles.length }}/5</text><button class="primary" @click="newRole">＋ 创建角色</button></view><view class="role-grid"><view v-for="r in session.roles" :key="r.id" class="card role-card"><view class="avatar large">{{ r.name.slice(0,1) }}</view><text class="section-title">{{ r.name }}</text><text class="muted role-bio">{{ r.bio || '写一段介绍，让别人认识这个你。' }}</text><view class="row"><text class="pill">{{ r.visibility==='PUBLIC'?'公开':'私密' }}</text><text class="pill">{{ r.ai_enabled ? 'AI 开启' : 'AI 关闭' }}</text></view><view class="role-buttons"><button @click="openEdit(r)">编辑设定</button><button @click="session.activeRole=r.id">{{session.activeRole===r.id?'当前身份':'切换身份'}}</button></view><text class="small link" @click="removeRole(r)">删除角色</text></view></view><view class="notice">选择当前角色后，可在下方配置记录授权、候选设定、知识库、工具和主动行为。所有授权默认关闭。</view></view>

      <AgentWorkbench v-if="tab==='我的角色'" :role="currentRole" :connections="connections"/><view v-if="tab==='模型与设置'" class="settings-grid"><view class="card"><text class="section-title">模型连接</text><text class="muted small">密钥加密保存，不向角色拥有者提供你的调用记录。</text><view v-for="c in connections" :key="c.id" class="connection-row"><view><text class="bold">{{ c.model }}</text><text class="small muted">{{ c.provider }} · {{ c.provider==='ollama'?'本机推理，无需密钥':'密钥已配置' }}</text></view><text class="link" @click="act(async()=>{const result=await api('/me/connections/'+c.id+'/test','POST');showDialog({title:result.ok?'连接可用':'模型未找到',content:result.note,showCancel:false})})">测试</text><text class="link" @click="act(async()=>{await api('/me/connections/'+c.id,'DELETE'); await syncAccount()})">移除</text></view><view class="form-stack"><picker :range="providers" @change="connectionDraft.provider=providers[Number($event.detail.value)];connectionDraft.model=connectionDraft.provider==='mock'?'demo-role':connectionDraft.provider==='ollama'?'qwen3:8b':connectionDraft.provider==='deepseek'?'deepseek-chat':'qwen-plus'"><view class="select-field">供应商：{{ connectionDraft.provider }} ↕</view></picker><input v-model="connectionDraft.model" placeholder="模型名称"/><input v-if="!['mock','ollama'].includes(connectionDraft.provider)" v-model="connectionDraft.api_key" password placeholder="API Key（仅提交一次，不回显）"/><text v-else-if="connectionDraft.provider==='ollama'" class="small muted">本机 Ollama 推理，无需 API Key，不产生云模型费用；仍受应用 token 预算限制。8B 文本模型不支持识图或生图。</text><text v-else class="small muted">Mock 是固定演示回复，不调用大模型、不产生外部费用。需要服务器开启演示模式。</text><button class="primary" :disabled="busy" @click="saveConnection">添加连接</button></view></view><view class="card"><text class="section-title">个人信息与额度</text><view class="form-stack"><input v-model="profile.nickname" placeholder="昵称"/><textarea v-model="profile.bio" placeholder="个人简介"/><button @click="act(async()=>{await api('/me','PATCH',{nickname:profile.nickname,bio:profile.bio});await syncAccount()})">保存资料</button><text class="bold">累计 token 预算</text><input v-model.number="profile.budget" type="number"/><text class="small muted">已计入 {{ session.account.used }} · 预占 {{ session.account.reserved }}。未知用量按预占保守计入，非供应商账单。</text><button @click="act(async()=>{await api('/me/budgets','PATCH',{budget:Number(profile.budget)});await syncAccount()})">更新预算</button></view></view><view class="card runs"><text class="section-title">我自己的运行</text><text class="muted small">这里只包含你付费的任务，不包含其他人与角色的私聊。</text><view v-for="r in runs" :key="r.id" class="run-row"><text>{{ r.status }}</text><text>{{ r.used }} tokens</text><text class="small muted">{{ timeLabel(r.created_at) }}</text></view><text v-if="!runs.length" class="muted">暂无运行记录</text></view></view>
    </view>
    <view v-if="editRole" class="modal-mask"><scroll-view scroll-y class="modal"><view class="row between"><text class="section-title">{{ editRole.id ? '编辑角色' : '创建角色' }}</text><text class="link" @click="editRole=null">关闭 ×</text></view><view class="form-stack"><input v-model="editRole.name" placeholder="角色名称"/><textarea v-model="editRole.bio" placeholder="公开简介"/><text class="bold">角色设定 · 仅你可编辑</text><textarea v-model="editRole.persona" maxlength="4000" placeholder="性格、说话风格、行为习惯与边界"/><view class="row between"><text>在广场公开角色</text><switch :checked="editRole.visibility==='PUBLIC'" color="#397961" @change="editRole.visibility=$event.detail.value?'PUBLIC':'PRIVATE'"/></view><view class="row between"><text>允许 AI 扮演</text><switch :checked="editRole.ai_enabled" color="#397961" @change="editRole.ai_enabled=$event.detail.value"/></view><view class="row between"><text>允许他人与 AI 对话</text><switch :checked="editRole.public_ai" color="#397961" @change="editRole.public_ai=$event.detail.value"/></view><text class="small muted">开启 AI 表示允许选定供应商接收角色设定。当前支持 mock、本机 Ollama、DeepSeek、DashScope；真实供应商调用使用访客自己的凭证。</text><view v-for="p in providers" :key="p" class="row between"><text>{{ p }}</text><switch :checked="editRole.provider_scope.includes(p)" color="#397961" @change="editRole.provider_scope=$event.detail.value?[...editRole.provider_scope,p]:editRole.provider_scope.filter((x:string)=>x!==p)"/></view><text class="small muted">修改普通设定只影响新建会话；关闭 AI、隐藏或删除角色即时阻断后续回复。</text><button class="primary" :disabled="busy" @click="saveRole">保存角色</button></view></scroll-view></view>
  </view>
</template>

<style scoped>
.welcome{min-height:100vh;display:flex;align-items:center;justify-content:center;gap:9vw;padding:40px;box-sizing:border-box;background:radial-gradient(ellipse at 20% 50%,#e6ecdd 0,transparent 60%)}
.welcome-story{width:480px;position:relative}.eyebrow{font-size:11px;letter-spacing:2px;color:#819187;display:block;margin-bottom:14px}.welcome-title{font-size:54px;line-height:1.35;letter-spacing:-2px;font-weight:600;display:block}.lead{display:block;color:#718076;line-height:1.9;margin-top:24px}.orb{margin:40px 0;width:180px;height:150px;border-radius:50%;background:#b9cdb3;transform:rotate(-15deg);display:flex;align-items:center;justify-content:center;font-size:40px;box-shadow:50px 12px 0 #e4c3a2}.orb text{margin:40px -90px 0 30px;color:#6f5240}.subtle{font-size:12px;color:#8c958b}.auth-card{width:365px;background:#fff;padding:40px;border-radius:24px;border:1px solid #e8ebe3;box-shadow:0 20px 80px #24483e08}.brand{font-size:23px;font-weight:700;letter-spacing:2px;display:flex;align-items:center;gap:10px}.brand text{font-size:9px;letter-spacing:1px;color:#819187;font-weight:500}.auth-card .brand{margin-bottom:38px}.section-title{font-size:23px;letter-spacing:-.4px;display:block;font-weight:600;margin-bottom:10px}.muted{color:#879087}.small{font-size:12px;display:block;line-height:1.7}.bold{font-weight:600}.form-stack{display:flex;flex-direction:column;gap:15px;margin-top:22px}.primary{background:#316a54;color:white}.primary[disabled]{background:#a8b7ae;color:#fff}.text-button{background:transparent;color:#75877a;padding:8px 0;font-size:13px}.fine-print{display:block;font-size:11px;color:#a0a69d;text-align:center;margin-top:28px}.app-shell{display:flex;min-height:100vh}.sidebar{width:232px;flex-shrink:0;background:#fffef9;border-right:1px solid #e5e8df;padding:34px 24px;box-sizing:border-box;display:flex;flex-direction:column;position:fixed;top:0;bottom:0}.nav-label{font-size:10px;color:#9da799;letter-spacing:1.4px;display:block;margin:36px 0 14px}.nav-list{display:flex;flex-direction:column;gap:8px}.nav-item{display:flex;align-items:center;gap:12px;padding:13px 16px;border-radius:10px;color:#768175;font-size:14px;cursor:pointer}.nav-item.selected{background:#e9efdf;color:#385a40;font-weight:600}.nav-icon{font-size:21px;width:20px}.nav-dot{margin-left:auto;font-size:7px}.identity{margin-top:auto}.identity-pick{display:flex;align-items:center;gap:10px;padding:12px 0;cursor:pointer}.avatar{width:40px;height:40px;flex-shrink:0;background:#e5ecdc;border-radius:14px;display:flex;align-items:center;justify-content:center;color:#5b734d;font-size:17px}.peach{background:#f0e4d5;color:#9a7456}.large{width:65px;height:65px;font-size:28px;border-radius:20px;margin-bottom:16px}.small-avatar{width:32px;height:32px;font-size:13px;border-radius:10px}.account-foot{display:flex;justify-content:space-between;margin-top:26px;padding-top:20px;border-top:1px solid #e8ebe2;font-size:12px}.link{color:#658b6d;cursor:pointer}.main{margin-left:232px;padding:34px 42px 50px;box-sizing:border-box;width:calc(100% - 232px);max-width:1500px}.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:30px}.topbar .eyebrow{margin-bottom:8px;font-size:9px}.page-title{font-size:29px;font-weight:600}.status{font-size:11px;color:#82917d}.dot{color:#86a777;margin-right:6px}.community-grid{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:28px}.card{background:#fffefb;border:1px solid #e6e9df;border-radius:18px;padding:24px;box-sizing:border-box}.row{display:flex;align-items:center;gap:12px}.between{justify-content:space-between}.grow{flex:1}.pill{font-size:10px;padding:5px 9px;background:#f0f1eb;color:#8a9183;border-radius:6px;white-space:nowrap}.ai-pill{background:#e7eee0;color:#5b8050}.composer textarea{border:0;background:transparent;min-height:100px;height:100px;margin:14px 0;padding:12px 0}.feed-heading{display:flex;align-items:center;justify-content:space-between;margin:28px 0 16px;font-size:14px}.post{margin-bottom:16px}.post-body{display:block;white-space:pre-wrap;line-height:1.9;font-size:15px;margin:22px 0}.post-actions{display:flex;gap:28px;color:#8a9586;font-size:12px;padding-top:20px;margin-top:12px;border-top:1px solid #edf0e7}.post-actions text{cursor:pointer}.comments{margin-top:18px;padding-top:8px}.comment{font-size:13px;line-height:1.8;margin-bottom:10px}.comment .small{display:inline}.feature-card{background:#e7ecdd;border-radius:18px;padding:28px;overflow:hidden}.feature-title{display:block;font-size:25px;line-height:1.6;margin:10px 0 16px;font-weight:500}.feature-card .muted{font-size:13px;line-height:1.9}.feature-card button{margin-top:24px;background:#f9fbf3;font-size:12px}.mini-orbits{font-size:70px;text-align:center;color:#9eb48c;letter-spacing:4px;margin-top:26px}.mini-orbits text{color:#c9a888}.guidelines{margin-top:20px}.guidelines text{display:block;font-size:11px;line-height:2.8;color:#7f8a76}.guidelines .bold{font-size:13px;margin-bottom:8px;color:#506247}.notice{padding:16px 20px;background:#ecefe4;border-radius:12px;color:#7b876f;font-size:12px;line-height:1.8;margin-bottom:22px}.error,.error-banner{color:#a2493f;line-height:1.8;font-size:13px}.error-banner{background:#f8e8e3;border-radius:10px;padding:12px 18px;margin-bottom:18px}.toolbar{display:flex;gap:12px;margin-bottom:24px;align-items:center}.toolbar input{max-width:440px;background:#fff}.role-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:20px}.role-card{display:flex;flex-direction:column;align-items:flex-start;gap:8px}.role-bio{font-size:13px;line-height:1.8;min-height:60px}.role-buttons{display:flex;gap:10px;width:100%;margin:18px 0 8px}.role-buttons button{flex:1;padding:11px 8px;font-size:12px}.empty{min-height:180px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;color:#778770;text-align:center}.empty-symbol{font-size:50px;color:#b1bfa6}.chat-layout{display:grid;grid-template-columns:260px minmax(0,1fr);gap:20px}.conversation-list{padding:20px 12px}.conversation-list>.bold{padding:10px;display:block;margin-bottom:12px}.conversation{display:flex;align-items:center;gap:10px;padding:14px 10px;border-radius:10px;cursor:pointer;font-size:13px}.delete-conversation{flex-shrink:0;color:#a2493f;font-size:12px;padding:6px;margin:0}.conversation .grow{min-width:0;overflow-wrap:anywhere}.conversation.active{background:#edf1e7}.badge{font-size:10px;background:#e6c5a3;border-radius:10px;padding:2px 5px}.chat-panel{padding:0;overflow:hidden}.clear-chat{margin-left:auto;color:#a2493f;flex-shrink:0}.chat-heading{flex-wrap:wrap;display:flex;align-items:center;gap:10px;padding:20px;border-bottom:1px solid #eff0e9}.chat-privacy{padding:10px 20px;background:#f0f3e9;color:#7c8970;font-size:11px}.message-scroll{height:440px;box-sizing:border-box}.message{padding:14px 22px;display:flex;flex-direction:column;align-items:flex-start}.message.own{align-items:flex-end}.message-label{font-size:10px;color:#8b9982;margin-bottom:8px}.bubble{max-width:85%;background:#f0f2e9;padding:12px 16px;border-radius:2px 14px 14px;line-height:1.8;font-size:14px;white-space:pre-wrap;word-break:break-word}.own .bubble{background:#e3ecda;border-radius:14px 2px 14px 14px}.agent .bubble{background:#f0ede3}.retract{font-size:10px;color:#abb3a6;cursor:pointer;margin-top:4px}.chat-compose{padding:16px 20px;border-top:1px solid #e9ece2}.chat-compose textarea{border:0;background:transparent;height:64px;min-height:64px;padding:0}.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.connection-row,.run-row{display:flex;justify-content:space-between;gap:12px;padding:16px 0;border-bottom:1px solid #edf0e8;font-size:13px}.select-field{background:#f2f4ec;padding:14px;border-radius:10px;font-size:14px}.runs{grid-column:1/-1}.modal-mask{position:fixed;inset:0;background:#243b3560;z-index:100;display:flex;justify-content:center;align-items:center;padding:20px}.modal{background:#fffefb;border-radius:22px;padding:28px;width:520px;max-height:85vh;box-sizing:border-box}
@media(max-width:1100px){.community-grid{grid-template-columns:1fr}.aside{display:none}.main{padding:28px 24px}.chat-layout{grid-template-columns:210px minmax(0,1fr)}}
@media(max-width:760px){.welcome{padding:22px}.welcome-story{display:none}.auth-card{width:100%;max-width:400px;padding:28px}.sidebar{position:static;width:100%;padding:16px;border:0}.app-shell{display:block}.nav-label,.identity>.small,.account-foot{display:none}.nav-list{flex-direction:row;overflow:auto;margin-top:14px;gap:4px}.nav-item{padding:10px;font-size:12px;white-space:nowrap}.nav-icon,.nav-dot{display:none}.identity{margin-top:12px}.identity-pick{padding:0}.identity .avatar{width:30px;height:30px;font-size:12px}.identity .small{display:inline;margin-left:10px}.main{margin:0;width:100%;padding:22px 16px}.topbar{margin-bottom:20px}.status{display:none}.page-title{font-size:25px}.card{padding:18px}.chat-layout{display:flex;flex-direction:column}.conversation-list{max-height:160px;overflow:auto}.chat-panel{padding:0}.message-scroll{height:350px}.settings-grid{grid-template-columns:1fr}.role-grid{grid-template-columns:1fr}.modal{padding:22px;width:100%}}
</style>
