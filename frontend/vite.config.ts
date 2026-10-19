// 文件职责：Vite 开发配置：uni-app 编译插件及 API/WebSocket 反向代理；仅用于开发服务。
import { defineConfig } from 'vite'
import uni from '@dcloudio/vite-plugin-uni'
const target = process.env.HAI_DEV_API || 'http://127.0.0.1:8014'
export default defineConfig({
  plugins: [uni()],
  server: { port: Number(process.env.HAI_DEV_PORT || 5175), strictPort: true, proxy: { '/api': target, '/ws': { target: target.replace('http', 'ws'), ws: true } } },
})
