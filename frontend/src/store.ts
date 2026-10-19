// 文件职责：Pinia 共享登录状态：保存当前账号、角色列表和选中角色 ID，不保存模型密钥。
import { defineStore } from 'pinia'
// state 保存可响应的登录与角色数据；currentRole 是由 activeRole 派生的 getter，找不到时返回 undefined。
export const useSession = defineStore('session', { state: () => ({ account: null as any, roles: [] as any[], activeRole: '' }), getters: { currentRole: s => s.roles.find(r => r.id === s.activeRole) } })
