<script setup lang="ts">
// 文件职责：角色拥有者的 Agent 工作台：管理学习授权、候选、知识与主动计划，不展示访客私聊。
import { ref, reactive, watch } from 'vue'
import { api } from '../api'
// #ifdef H5
import { uploadKnowledge } from '../api'
// #endif
const props=defineProps<{role:any,connections:any[]}>()
const error=ref(''),busy=ref(false),config=ref<any>(null),candidates=ref<any[]>([]),documents=ref<any[]>([]),plans=ref<any[]>([]),deliveries=ref<any[]>([]),runs=ref<any[]>([])
// connection 是本人连接列表索引；backfill 明确允许回溯旧记录，locked 控制已学特征是否保持。
const connection=ref(0),backfill=ref(false),locked=ref(false)
const tools=['vision_describe','image_generate','weather_query','maps_search','maps_route']
const labels:any={vision_describe:'识图',image_generate:'生图',weather_query:'天气',maps_search:'城市地图',maps_route:'驾车路线'}
const flags:any={learning_enabled:'允许记录学习',auto_apply_persona:'自动应用候选设定',allow_proactive_dm:'主动私聊',allow_auto_post:'主动动态',allow_auto_comment:'主动评论',allow_ai_comment_reply:'回复 AI 评论时继续由 AI 回应',auto_publish_proactive:'主动内容自动发布',accept_ai_comments:'接收 AI 评论'}
const knowledge=reactive({title:'',text:'',authorized:false})
const plan=reactive({kind:'POST',target_id:'',prompt:'',interval_hours:24,start_hour:9,end_hour:21})
const planKinds=['POST','DM','COMMENT']
const exportText=ref('')
// act：防止重复提交，统一设置忙碌/错误状态，并在 finally 恢复按钮可用性。
async function act(fn:()=>Promise<any>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e:any){error.value=e.message}finally{busy.value=false}}
// load：加载当前角色配置、候选、文档与本人计划/任务，更新工作台显示。
async function load(){if(!props.role)return;config.value=await api('/roles/'+props.role.id+'/agent');candidates.value=await api('/roles/'+props.role.id+'/candidates');documents.value=await api('/roles/'+props.role.id+'/knowledge');plans.value=await api('/me/plans');deliveries.value=await api('/me/deliveries');runs.value=await api('/me/runs');locked.value=config.value.locked_fields?.length>0}
// 切换管理角色时重新加载数据；immediate 使首次显示也加载。
watch(()=>props.role?.id,()=>act(load),{immediate:true})
// toggleTool：切换角色允许工具集合；最终是否执行还需会话授权。
function toggleTool(name:string){const set=new Set(config.value.enabled_tools);set.has(name)?set.delete(name):set.add(name);config.value.enabled_tools=[...set]}
// toggleSource：切换允许学习的来源类别，不直接读取任何聊天正文。
function toggleSource(name:string){const set=new Set(config.value.learning_sources);set.has(name)?set.delete(name):set.add(name);config.value.learning_sources=[...set]}
// ownConnection：取得当前选中的本人连接，缺失时给出明确配置提示。
function ownConnection(){const c=props.connections[connection.value];if(!c)throw new Error('请先添加自己的模型连接');return c}
// save：去掉响应元数据，设置锁定字段、学习提供商与自动学习连接，提交配置及可选历史回溯授权。
async function save(){await act(async()=>{const {consent_version,learning_since,active_candidate,...data}=config.value;data.locked_fields=locked.value?['personality','speaking_style','habits']:[];if(data.learning_enabled)data.learning_providers=[ownConnection().provider];if(data.auto_learn_hours)data.learning_connection_id=ownConnection().id;await api('/roles/'+props.role.id+'/agent','PUT',{...data,...(backfill.value?{backfill_from:0}:{})});backfill.value=false;await load()})}
// learn：用选定本人连接创建学习任务，再刷新工作台状态。
async function learn(){await act(async()=>{await api('/roles/'+props.role.id+'/learn','POST',{connection_id:ownConnection().id});await load()})}
// index：提交明确授权的知识文本与提供商配置，创建索引任务。
async function index(){await act(async()=>{const c=ownConnection();await api('/roles/'+props.role.id+'/knowledge','POST',{...knowledge,connection_id:c.id,providers:[c.provider]});knowledge.text='';await load()})}
// newPlan：使用本人连接创建主动交互计划并刷新工作台。
async function newPlan(){await act(async()=>{await api('/roles/'+props.role.id+'/plans','POST',{...plan,connection_id:ownConnection().id});await load()})}
// #ifdef H5
// upload：确认知识授权后上传文件并刷新文档列表；只在 H5 编译。
async function upload(){await act(async()=>{if(!knowledge.authorized)throw Error('请先同意知识授权');const c=ownConnection();await uploadKnowledge(props.role.id,{connection_id:c.id,providers:[c.provider],authorized:true});await load()})}
// #endif
</script>

<template>
 <view v-if="role" class="workbench" :data-busy="busy ? 'true' : 'false'">
  <view class="head"><text class="title">{{role.name}} · Agent 工作台</text><button @click="act(load)">刷新任务状态</button></view>
  <text class="note">这里只显示你自己的配置、素材与付费任务。访客与角色的 AI 会话不会出现在这里。</text>
  <text v-if="busy" class="note">正在处理…</text><text v-if="error" class="error">{{error}}</text>
  <picker :range="connections" range-key="model" @change="connection=Number($event.detail.value)"><view class="select">本次使用的自有连接：{{connections[connection]?.model||'尚未配置'}} ↕</view></picker>
  <view v-if="config" class="grid">
   <view class="panel"><text class="title">授权与行为</text>
    <view v-for="(label,key) in flags" :key="key" class="row"><text>{{label}}</text><switch :checked="config[key]" @change="config[key]=$event.detail.value"/></view>
    <text class="note">开启学习默认只处理之后的本人真人记录。私聊还须双方授权；AI 私聊永不进入全局学习。</text>
    <view v-for="source in ['posts','comments','messages']" :key="source" class="row"><text>{{({posts:'动态',comments:'评论',messages:'双方许可的真人私聊'} as any)[source]}}</text><switch :checked="config.learning_sources.includes(source)" @change="toggleSource(source)"/></view>
    <view class="row"><text>本次明确回溯已有合格记录</text><switch :checked="backfill" @change="backfill=$event.detail.value"/></view>
    <view class="row"><text>锁定已生效特征</text><switch :checked="locked" @change="locked=$event.detail.value"/></view>
    <text>自动学习间隔（小时，0 表示关闭）</text><input type="number" v-model.number="config.auto_learn_hours"/>
    <text class="note">自动任务使用保存的本人连接。撤回会使旧候选不可再次激活；已发送到供应商的数据无法追回。</text>
    <text class="title">允许的工具</text><view v-for="name in tools" :key="name" class="row"><text>{{labels[name]}}</text><switch :checked="config.enabled_tools.includes(name)" @change="toggleTool(name)"/></view>
    <text class="note">访客还须在自己的会话中授权。天气/地图使用公共演示服务；识图/生图使用付款方单独配置的连接。</text>
    <button :disabled="busy" @click="save">保存 Agent 设置</button>
   </view>
   <view class="panel"><text class="title">学习候选与知识库</text><button :disabled="busy" @click="learn">从授权记录生成候选</button>
    <view v-for="c in candidates" :key="c.id" class="item"><text>{{c.valid?'可激活':'已失效'}} · {{config.active_candidate===c.id?'当前版本':''}}</text><text v-for="(value,key) in c.fields" :key="key">{{key}}：{{value}}</text><text class="note">授权样本 {{c.evidence.length}} 条</text><button :disabled="!c.valid||busy" @click="act(async()=>{await api('/roles/'+role.id+'/candidates/'+c.id+'/activate','POST');await load()})">应用 / 回滚到此版本</button></view>
    <button @click="act(async()=>{const result=await api('/roles/'+role.id+'/persona-export');exportText=JSON.stringify(result,null,2)})">查看可导出的设定 JSON</button><textarea v-if="exportText" :value="exportText" auto-height/>
    <text class="title">新增授权知识（文本 / Markdown）</text><input v-model="knowledge.title" placeholder="公开引用标题"/><textarea v-model="knowledge.text" maxlength="20000" placeholder="粘贴知识正文，最多 20000 字"/>
    <view class="row"><text>允许访客回答使用，接收方为所选供应商</text><switch :checked="knowledge.authorized" @change="knowledge.authorized=$event.detail.value"/></view>
    <button :disabled="busy||!knowledge.authorized" @click="index">切分并建立向量索引</button>
    <!-- #ifdef H5 --><button :disabled="busy||!knowledge.authorized" @click="upload">上传 TXT / Markdown / PDF（2 MB 内）</button><!-- #endif -->
    <view v-for="d in documents" :key="d.id" class="item"><text>{{d.title}} · {{d.status}}</text><button @click="act(async()=>{await api('/knowledge/'+d.id,'DELETE');await load()})">撤回文档</button></view>
   </view>
   <view class="panel"><text class="title">主动计划</text><text class="note">默认先生成草稿。私聊对象须许可你的角色；评论对象须允许 AI 评论。每日时段按北京时间。</text>
    <picker :range="['动态','私聊','评论']" @change="plan.kind=planKinds[Number($event.detail.value)]"><view class="select">类型：{{plan.kind}} ↕</view></picker>
    <input v-if="plan.kind!=='POST'" v-model="plan.target_id" :placeholder="plan.kind==='DM'?'接收角色 ID（从广场复制）':'目标动态 ID（从动态复制）'"/>
    <textarea v-model="plan.prompt" placeholder="本次主动内容的主题"/>
    <text>间隔小时（最少 1）</text><input type="number" v-model.number="plan.interval_hours"/><text>每日开始 / 结束小时</text><view class="row"><input type="number" v-model.number="plan.start_hour"/><input type="number" v-model.number="plan.end_hour"/></view>
    <button :disabled="busy" @click="newPlan">创建计划</button>
    <view v-for="p in plans.filter(x=>x.role_id===role.id)" :key="p.id" class="item"><text>{{p.kind}} · {{p.enabled?'开启':'停止'}} · {{p.prompt}}</text><button v-if="p.enabled" @click="act(async()=>{await api('/plans/'+p.id+'/trigger','POST');await load()})">执行到期计划</button><button v-if="p.enabled" @click="act(async()=>{await api('/plans/'+p.id,'DELETE');await load()})">停止计划</button></view>
   </view>
   <view class="panel"><text class="title">本人主动投递记录</text><view v-for="d in deliveries" :key="d.id" class="item"><text>{{d.kind}} · {{d.status==='SUBMITTED'?'服务端已提交':d.status}}</text><text>{{d.body}}</text><button v-if="d.status==='DRAFT'" @click="act(async()=>{await api('/deliveries/'+d.id+'/publish','POST');await load()})">审核并发布</button></view><text class="note">不展示接收者是否阅读、是否回复或后续会话。</text>
    <text class="title">本人最近任务</text><view v-for="r in runs" :key="r.id" class="item"><text>{{r.purpose}} · {{r.status}} · token {{r.used}}</text><text v-if="r.error" class="error">{{r.error}}</text><button v-if="r.status==='PAUSED'" @click="act(async()=>{await api('/runs/'+r.id+'/resume','POST');await load()})">从安全检查点恢复</button></view>
   </view>
  </view>
 </view>
</template>

<style scoped>
.workbench{margin-top:24px}.head,.row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:12px 0}.title{display:block;font-size:18px;font-weight:600;margin:16px 0}.note{display:block;font-size:12px;color:#718068;line-height:1.8;margin:12px 0}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.panel{padding:22px;background:#fffefb;border:1px solid #e6e9df;border-radius:16px}.row{font-size:13px}.row input{width:40%}.select,.item{padding:14px;background:#f1f3ec;border-radius:10px;margin:12px 0}.item text{display:block;font-size:13px;line-height:1.8}.panel button{margin:12px 0}.panel input,.panel textarea{margin:12px 0}.error{color:#a2493f;display:block}.head button{font-size:12px}@media(max-width:800px){.grid{grid-template-columns:1fr}.head{flex-wrap:wrap}}
</style>
