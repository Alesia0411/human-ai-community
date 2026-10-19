<script setup lang="ts">
// 文件职责：访客会话配置：选择自有模型与工具连接、限制额度并提交私有知识。
import {ref,watch} from 'vue'
import {api} from '../api'
const props=defineProps<{chat:any,connections:any[],run:any}>()
const error=ref(''),open=ref(false),busy=ref(false),cfg=ref<any>({}),calls=ref<any[]>([]),memory=ref(''),authorized=ref(false)
const names=['vision_describe','image_generate','weather_query','maps_search','maps_route']
const labels:any={vision_describe:'识图',image_generate:'生图',weather_query:'天气',maps_search:'城市地图',maps_route:'驾车路线'}
// 会话变化时用默认限制合并服务端 grants，清除上个会话的错误和调用记录。
watch(()=>props.chat.id,()=>{cfg.value={connection_id:props.chat.connection_id,enabled_tools:[],tool_connections:{},max_tool_calls:0,max_images:0,max_tokens:16000,memory_enabled:props.chat.memory_enabled,...props.chat.grants};error.value='';calls.value=[]},{immediate:true})
// act：防止重复提交，统一设置忙碌/错误状态，并在 finally 恢复按钮可用性。
async function act(fn:()=>Promise<any>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e:any){error.value=e.message}finally{busy.value=false}}
// toggle：切换当前会话的工具集合，不自动授予角色级权限。
function toggle(name:string){const set=new Set(cfg.value.enabled_tools);set.has(name)?set.delete(name):set.add(name);cfg.value.enabled_tools=[...set]}
// save：将 cfg 中本会话的模型、工具额度和知识授权提交给后端，成功后显示保存提示。
async function save(){await act(async()=>{await api('/conversations/'+props.chat.id+'/agent-settings','PUT',cfg.value);uni.showToast({title:'已保存',icon:'none'})})}
// remember：校验手动授权和本人嵌入连接，提交限定当前会话的私有知识。
async function remember(){await act(async()=>{if(!authorized.value)throw Error('请先授权');const conn=props.connections.find(c=>c.id===(cfg.value.embedding_connection_id||cfg.value.connection_id));if(!conn)throw Error('请选择连接');await api('/roles/'+props.chat.target_role_id+'/knowledge','POST',{title:'我的会话记忆',text:memory.value,connection_id:conn.id,providers:[conn.provider],conversation_id:props.chat.id,authorized:true});memory.value=''})}
</script>
<template><view class="settings"><text class="link" @click="open=!open">会话工具、模型与私有记忆 {{open?'▴':'▾'}}</text>
<view v-if="open" class="content"><text class="note">回复后使用你的模型配置与额度，全程由 AI 回复。工具授权仅限本会话；不会向角色拥有者显示。</text><text v-if="error" class="error">{{error}}</text>
<picker :range="connections" range-key="model" @change="cfg.connection_id=connections[Number($event.detail.value)].id"><view class="select">对话模型：{{connections.find(c=>c.id===cfg.connection_id)?.model||'必须选择自己的连接'}} ↕</view></picker>
<view v-for="name in names" :key="name"><view class="row"><text>{{labels[name]}}</text><switch :checked="cfg.enabled_tools.includes(name)" @change="toggle(name)"/></view><picker v-if="['vision_describe','image_generate'].includes(name) && cfg.enabled_tools.includes(name)" :range="connections" range-key="model" @change="cfg.tool_connections[name]=connections[Number($event.detail.value)].id"><view class="select">专用模型：{{connections.find(c=>c.id===cfg.tool_connections[name])?.model||'选择自己的视觉 / 生图模型连接'}} ↕</view></picker></view>
<text class="note">天气/地图为公共演示额度，不记作你的供应商扣费。识图/生图需 DashScope 相应模型；图像按次数记录，金额以供应商账单为准。</text>
<text>每轮工具次数（0—4）</text><input type="number" v-model.number="cfg.max_tool_calls"/><text>每轮图片张数（0—1）</text><input type="number" v-model.number="cfg.max_images"/><text>每轮 token 上限</text><input type="number" v-model.number="cfg.max_tokens"/>
<view class="row"><text>允许本会话私有长期记忆</text><switch :checked="cfg.memory_enabled" @change="cfg.memory_enabled=$event.detail.value"/></view>
<picker :range="connections" range-key="model" @change="cfg.embedding_connection_id=connections[Number($event.detail.value)].id"><view class="select">嵌入连接：{{connections.find(c=>c.id===cfg.embedding_connection_id)?.model||'默认对话连接'}} ↕</view></picker>
<button :disabled="busy" @click="save">保存会话授权</button>
<button :disabled="busy" @click="act(()=>api('/conversations/'+chat.id+'/refresh-definition','POST'))">更新到角色最新设定</button>
<button v-if="run?.status==='PAUSED'" @click="act(()=>api('/runs/'+run.id+'/resume','POST'))">恢复已暂停的运行</button>
<view v-if="cfg.memory_enabled"><textarea v-model="memory" placeholder="明确提交给本会话使用的记忆；不会自动扫描历史"/><view class="row"><text>同意发送给所选嵌入供应商</text><switch :checked="authorized" @change="authorized=$event.detail.value"/></view><button @click="remember">添加私有记忆</button></view>
<button v-if="run" @click="act(async()=>{calls=await api('/runs/'+run.id+'/calls')})">查看本轮工具与费用状态</button>
<text v-if="run" class="note">运行耗时：{{run.duration_ms == null ? '尚未测得' : run.duration_ms+' ms'}} · 首段文字：{{run.first_token_ms == null ? '尚未测得（演示模型不计）' : run.first_token_ms+' ms'}}</text>
<text v-for="call in calls" :key="call.id" class="note">{{call.kind}} · {{call.state}} · {{call.units}} 次 / {{call.tokens}} tokens{{call.usage_known?'（已知）':'（估算或未知）'}} · {{call.billing}} · {{call.duration_ms == null ? '耗时未知' : call.duration_ms+' ms'}}</text>
</view></view></template>
<style scoped>.settings{padding:12px 20px;background:#f6f7f0;font-size:12px}.content{padding-top:16px}.row{display:flex;justify-content:space-between;align-items:center;margin:12px 0}.select{padding:12px;background:#e9eee2;margin:10px 0;border-radius:8px}.note{display:block;line-height:1.8;margin:10px 0;color:#718068}.content input,.content textarea,.content button{margin:12px 0}.error{color:#a2493f}.link{cursor:pointer;color:#4e785d}</style>
