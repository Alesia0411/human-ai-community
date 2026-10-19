// 文件职责：统一封装 HTTP、令牌刷新及受保护文件传输；H5 使用同源地址，App 可配置服务器。
// Access tokens deliberately stay in memory. Native long-term secure storage is not required for R1.
// 访问令牌仅驻留内存；页面刷新通过服务端刷新会话恢复，避免把令牌永久存本地。
export let accessToken = ''
// CSRF 令牌用于 Cookie 认证的写请求；不是模型 API Key。
export let csrfToken = ''
// 空字符串表示 H5 同源请求；App 分支从本地配置读取远程服务地址。
export let baseURL = ''
// #ifndef H5
baseURL = uni.getStorageSync('hai_server') || ''
// #endif
// configureServer：规范化 App 服务器地址并保存，下次启动可复用；H5 通常使用同源。
export function configureServer(value: string) {
  baseURL = value.replace(/\/$/, '')
  uni.setStorageSync('hai_server', baseURL)
}
// sessionTokens：更新内存中的访问令牌与 CSRF 令牌；无参数调用用于清空登录状态。
export function sessionTokens(access = '', csrf = '') { accessToken = access; csrfToken = csrf }
// api：发送带认证的 API 请求；H5 遇到一次 401 可刷新并重试，避免无限刷新循环。
export async function api(path: string, method = 'GET', data?: any, retried = false): Promise<any> {
  const r: any = await new Promise((resolve, reject) => uni.request({
    url: baseURL + '/api/v1' + path, method: method as any, data, withCredentials: true,
    header: { ...(accessToken ? { Authorization: 'Bearer ' + accessToken } : {}), ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}) },
    success: resolve, fail: reject,
  }))
  // #ifdef H5
  if (r.statusCode === 401 && !retried && !path.startsWith('/auth/') && csrfToken) {
    const fresh = await api('/auth/refresh', 'POST')
    sessionTokens(fresh.access_token, fresh.csrf_token)
    return api(path, method, data, true)
  }
  // #endif
  if (r.statusCode >= 400) throw new Error(typeof r.data.detail === 'string' ? r.data.detail : '输入有误，请检查必填信息')
  return r.data
}
// uploadImage：选择一张图片并带身份上传，返回后端 Asset ID 而不是公开文件地址。
export async function uploadImage(conversationId?: string): Promise<string> {
  const choice: any = await new Promise((resolve, reject) => uni.chooseImage({ count: 1, success: resolve, fail: reject }))
  const r: any = await new Promise((resolve, reject) => uni.uploadFile({
    url: baseURL + '/api/v1/assets', filePath: choice.tempFilePaths[0], name: 'file',
    formData: conversationId ? { conversation_id: conversationId } : {},
    header: { Authorization: 'Bearer ' + accessToken }, success: resolve, fail: reject,
  }))
  const body = JSON.parse(r.data)
  if (r.statusCode >= 400) throw new Error(body.detail || '上传失败')
  return body.id
}
// imageURL：带认证下载图片；H5 返回临时 Blob URL，App 返回本地临时文件路径。
export async function imageURL(id: string): Promise<string> {
  // Protected images require a credentialed download, never a permanently public URL.
  // #ifdef H5
  const response = await fetch(baseURL + '/api/v1/assets/' + id + '/content', { headers: { Authorization: 'Bearer ' + accessToken }, credentials: 'include' })
  if (!response.ok) return ''
  return URL.createObjectURL(await response.blob())
  // #endif
  // #ifndef H5
  const r: any = await new Promise((resolve, reject) => uni.downloadFile({ url: baseURL + '/api/v1/assets/' + id + '/content', header: { Authorization: 'Bearer ' + accessToken }, success: resolve, fail: reject }))
  return r.statusCode === 200 ? r.tempFilePath : ''
  // #endif
}

// #ifdef H5
// uploadKnowledge：H5 选择并上传文本/PDF 知识文件，附带已确认的授权与连接配置。
export async function uploadKnowledge(roleId: string, options: any): Promise<any> {
  const choice: any = await new Promise((resolve, reject) => uni.chooseFile({count:1,extension:['.txt','.md','.pdf'],success:resolve,fail:reject}))
  const r: any = await new Promise((resolve,reject)=>uni.uploadFile({url:baseURL+'/api/v1/roles/'+roleId+'/knowledge-file',
    filePath:choice.tempFilePaths[0],name:'file',formData:{options:JSON.stringify(options)},
    header:{Authorization:'Bearer '+accessToken},success:resolve,fail:reject}))
  const body=JSON.parse(r.data)
  if(r.statusCode>=400)throw Error(typeof body.detail==='string'?body.detail:'文档上传失败')
  return body
}
// #endif
