<script setup lang="ts">
// 文件职责：带凭证加载私有图片；切换图片和卸载时释放 Blob URL，避免过期请求覆盖新图。
import { ref, watch, onUnmounted } from 'vue'
import { imageURL } from '../api'
const props = defineProps<{ id: string }>()
const src = ref('')
// 每次换图递增版本；异步下载回来时只有最新版本可以更新 src。
let generation = 0
// release：释放 H5 Blob URL；普通地址和其他平台不执行此操作。
function release(url: string) {
  // #ifdef H5
  if (url.startsWith('blob:')) URL.revokeObjectURL(url)
  // #endif
}
// 监听图片 ID，立即加载首图；过期请求即使晚返回，也只释放 URL 而不覆盖当前图片。
watch(() => props.id, async id => { const n = ++generation; release(src.value); src.value = ''; const next = await imageURL(id).catch(() => ''); if (n === generation) src.value = next; else release(next) }, { immediate: true })
// 组件卸载时让所有在途请求失效，并释放当前浏览器对象 URL。
onUnmounted(() => { generation++; release(src.value) })
</script>
<template><image v-if="src" :src="src" mode="widthFix" style="width:100%;max-width:420px;border-radius:12px"/><text v-else>图片加载中</text></template>
