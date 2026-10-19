// 文件职责：浏览器端 Agent 工作台验收：学习授权、候选、知识索引与会话工具配置。
import {test,expect} from '@playwright/test'

// 浏览器测试回调：先准备合成数据，再操作页面，最后通过 expect 判断可见结果。
test('R2 workbench learns with consent, indexes knowledge and configures session tools',async({page,request})=>{
  const username='r2_'+Date.now(),password='test-password-123'
  const signup=await request.post('/api/v1/auth/register',{data:{username,password,nickname:'R2 测试',invite_code:'local-demo'}})
  expect(signup.status()).toBe(201)
  const headers={Authorization:'Bearer '+(await signup.json()).access_token}
  const role=await (await request.post('/api/v1/me/roles',{headers,data:{name:username,bio:'合成测试角色',visibility:'PUBLIC',ai_enabled:true,public_ai:true}})).json()
  const connection=await (await request.post('/api/v1/me/connections',{headers,data:{provider:'mock',model:'demo-r2'}})).json()
  await page.goto('/')
  await page.locator('uni-input').filter({hasText:'账号（至少 3 位）'}).locator('input').fill(username)
  await page.locator('uni-input').filter({hasText:'密码（注册至少 10 位）'}).locator('input').fill(password)
  await page.getByText('进入共栖',{exact:true}).click()
  await page.locator('.nav-item').filter({hasText:'我的角色'}).click()
  const workbench=page.locator('.workbench')
  await expect(workbench.getByText('授权与行为',{exact:true})).toBeVisible()
  await expect(workbench).toHaveAttribute('data-busy','false')
  await workbench.locator('.row').filter({hasText:'允许记录学习'}).locator('uni-switch').click()
  await workbench.locator('.row').filter({hasText:'天气'}).locator('uni-switch').click()
  await workbench.getByText('保存 Agent 设置',{exact:true}).click()
  await expect(workbench).toHaveAttribute('data-busy','false')
  await expect(workbench.locator('.error')).toHaveCount(0)
  await request.post('/api/v1/posts',{headers,data:{role_id:role.id,body:'我每天喜欢先安排日程再出门。'}})
  await workbench.getByText('从授权记录生成候选',{exact:true}).click()
  await expect(workbench).toHaveAttribute('data-busy','false')
  await expect.poll(async()=>{
    const runs=await (await request.get('/api/v1/me/runs',{headers})).json()
    return runs.find((r:any)=>r.purpose==='LEARN')?.status
  },{timeout:20000}).toBe('SUCCEEDED')
  await workbench.getByText('刷新任务状态',{exact:true}).click()
  await expect(workbench).toHaveAttribute('data-busy','false')
  await expect(workbench.getByText('应用 / 回滚到此版本',{exact:true})).toBeVisible()
  await workbench.getByText('应用 / 回滚到此版本',{exact:true}).click()
  await expect(workbench).toHaveAttribute('data-busy','false')
  await workbench.locator('uni-input').filter({hasText:'公开引用标题'}).locator('input').fill('小屋开放时间')
  await workbench.locator('uni-textarea').filter({hasText:'粘贴知识正文'}).locator('textarea').fill('山间小屋周六上午九点开放。')
  await workbench.locator('.row').filter({hasText:'允许访客回答使用'}).locator('uni-switch').click()
  await workbench.getByText('切分并建立向量索引',{exact:true}).click()
  await expect(workbench).toHaveAttribute('data-busy','false')
  await expect.poll(async()=>{
    const docs=await(await request.get('/api/v1/roles/'+role.id+'/knowledge',{headers})).json()
    return docs[0]?.status
  },{timeout:20000}).toBe('READY')
  await workbench.getByText('刷新任务状态',{exact:true}).click()
  await expect(workbench).toHaveAttribute('data-busy','false')
  await expect(workbench.getByText('小屋开放时间 · READY',{exact:true})).toBeVisible()
  await page.screenshot({path:'test-results/r2-workbench.png',fullPage:true})
  await request.post('/api/v1/conversations',{headers,data:{kind:'AI_DIRECT',source_role_id:role.id,target_role_id:role.id,connection_id:connection.id}})
  await page.locator('.nav-item').filter({hasText:'消息'}).click()
  await page.locator('.conversation').first().click()
  await page.getByText('会话工具、模型与私有记忆',{exact:false}).click()
  await page.locator('.settings .row').filter({hasText:'天气'}).locator('uni-switch').click()
  await page.getByText('保存会话授权',{exact:true}).click()
  await expect(page.locator('.settings .error')).toHaveCount(0)
})
