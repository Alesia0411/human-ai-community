// 文件职责：uni-app 的 Vue 应用工厂，安装 Pinia 后交由运行平台挂载。
import { createSSRApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
// createApp：创建 Vue 应用并安装 Pinia，返回 uni-app 约定的 app 对象。
export function createApp() { const app = createSSRApp(App); app.use(createPinia()); return { app } }
