<script lang="ts">
// 文件职责：应用根组件与全局样式；页面业务由 pages 下的组件负责。
// onLaunch 为应用启动生命周期；当前保留空实现，全局视觉规则在 style 中。
export default { onLaunch() {} }
</script>
<style>
page, uni-page-body { background: #f5f5f0; color: #243b35; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', sans-serif; }
button, uni-button { font-size: 14px; line-height: 1.4; margin: 0; padding: 12px 18px; border-radius: 12px; background: #e9eee8; color: #24483e; }
button::after, uni-button::after { border: none; }
input, textarea, uni-input, uni-textarea { box-sizing: border-box; }
uni-input, uni-textarea { background: #f4f5f1; border: 1px solid #e3e7df; border-radius: 10px; padding: 12px; font-size: 15px; width: 100%; }
uni-input { height: 46px; } uni-textarea { min-height: 100px; }
</style>
