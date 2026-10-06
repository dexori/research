// Run against tests/preview_server.py (use --fake-codex with TEST_ASSISTANT=1).
// Never uses the owner's real workspace.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {execFileSync} = require('node:child_process');

(async () => {
  const output = path.join(__dirname, '..', 'artifacts');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, acceptDownloads: true, reducedMotion:'reduce' });
  const page = await context.newPage();
  const errors = [];
  const externalRequests = [];
  page.on('request',request=>{if(!request.url().startsWith('http://127.0.0.1:8766/') && /^https?:/.test(request.url())) externalRequests.push(request.url());});
  await context.route(/^https?:\/\/(?!127\.0\.0\.1:8766\/)/,route=>route.abort());
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if(message.type()==='error') errors.push(message.text()); });
  const base = 'http://127.0.0.1:8766';
  try {
    await page.goto(base);
    assert.match(page.url(), /\/setup$/);
    const password = crypto.randomBytes(18).toString('hex');
    await page.locator('#password').fill(password);
    await page.locator('#confirm').fill(password);
    await page.getByRole('button', { name: '创建工作空间', exact: true }).click();
    await page.getByRole('heading', { name: '工作概览', exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, 'workspace-desktop.png'), fullPage: true });
    const newRecord = async (kind,title) => {
      await page.goto(`${base}/new/${kind}`);
      await page.locator('#title').fill(title);
      await page.locator('#content').fill('## 浏览器验证\n\n这是隔离测试数据。**保存与显示正常**。');
    };
    await newRecord('project','浏览器验证项目（隔离测试）');
    await page.getByRole('button',{name:'保存项目',exact:true}).click();
    const projectId = page.url().split('/').pop();
    await newRecord('paper','浏览器验证文献（隔离测试）');
    await page.locator('#project_id').selectOption(projectId);
    await page.locator('#authors').fill('测试作者');
    await page.locator('#tags').fill('VLA, browser-check');
    await page.locator('#url').fill('https://example.org/paper');
    await page.getByRole('button',{name:'保存文献',exact:true}).click();
    const paperUrl = page.url();
    assert.equal(await page.locator('.prose strong').textContent(), '保存与显示正常');
    await page.locator('#files').setInputFiles({ name:'结果.csv', mimeType:'text/csv', buffer:Buffer.from('seed,score\n0,76\n') });
    await page.getByRole('button',{name:'上传附件',exact:true}).click();
    await page.locator('.attachment-list a').waitFor();
    const attachmentPromise = page.waitForEvent('download');
    await page.locator('.attachment-list a').click();
    const attachment = await attachmentPromise;
    assert.equal(attachment.suggestedFilename(), '结果.csv');
    assert.equal(await attachment.failure(), null);
    await newRecord('experiment','浏览器验证实验（隔离测试）');
    await page.locator('#project_id').selectOption(projectId);
    await page.locator('#model').fill('测试模型');
    await page.locator('#dataset').fill('测试数据集');
    await page.locator('#seed').fill('0');
    await page.locator('#metric_name').fill('Success rate (%)');
    await page.locator('#metric_value').fill('76');
    await page.locator('#params').fill('lr: 1e-4\nsteps: 70000');
    await page.getByRole('button',{name:'保存实验',exact:true}).click();
    const experimentUrl = page.url();
    assert.equal(await page.locator('.experiment-facts > div').last().locator('strong').textContent(),'76');
    await page.goto(`${base}/library/experiment`);
    const csvPromise = page.waitForEvent('download');
    await page.getByRole('link',{name:'导出 CSV',exact:true}).click();
    assert.equal((await csvPromise).suggestedFilename(),'experiments.csv');
    await page.goto(`${base}/search?q=browser-check`);
    assert.equal(await page.locator('.search-results > a').count(),1);
    await page.goto(`${base}/entry/${projectId}`);
    assert.equal(await page.locator('.related-panel .recent-list > a').count(),2);
    await page.goto(paperUrl);
    await page.getByRole('link',{name:'编辑文献',exact:true}).click();
    await page.locator('#content').fill('## 更新成功\n\n浏览器实际修改。');
    await page.getByRole('button',{name:'保存文献',exact:true}).click();
    await page.getByRole('heading',{name:'更新成功',exact:true}).waitFor();
    const mdPromise = page.waitForEvent('download');
    await page.getByRole('link',{name:'导出 Markdown',exact:true}).click();
    assert.match((await mdPromise).suggestedFilename(), /\.md$/);
    await page.goto(`${base}/backup`);
    const backupPromise = page.waitForEvent('download');
    await page.getByRole('link',{name:'下载完整备份 ZIP',exact:true}).click();
    assert.match((await backupPromise).suggestedFilename(), /\.zip$/);
    let authState = {status:'signed_out',code:'',message:'这台电脑尚未登录'};
    await page.route('**/sync/github/status', route => route.fulfill({json:authState}));
    await page.route('**/sync/github/login', route => {
      assert.match(route.request().postData() || '', /csrf/);
      authState = {status:'awaiting',code:'ABCD-1234',message:'请在 GitHub 输入授权码'};
      return route.fulfill({json:authState});
    });
    await page.goto(`${base}/sync`);
    await page.getByRole('heading',{name:'GitHub 同步',exact:true}).waitFor();
    await page.getByRole('button',{name:'登录 GitHub',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#github-device-code').value === 'ABCD-1234');
    assert.equal(await page.locator('[data-github-device]').isVisible(),true);
    assert.equal(await page.getByRole('link',{name:'前往 GitHub 授权 ↗',exact:true}).getAttribute('href'),'https://github.com/login/device');
    assert.equal(await page.locator('[data-github-login-button]').isDisabled(),true);
    await page.screenshot({path:path.join(output,'github-login-code-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth),false);
    await page.screenshot({path:path.join(output,'github-login-code-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1440,height:1000});
    authState = {status:'error',code:'',message:'授权码已过期，请重试'};
    await page.getByRole('button',{name:'重新登录 GitHub',exact:true}).waitFor();
    assert.equal(await page.locator('[data-github-device]').isHidden(),true);
    await page.getByRole('button',{name:'重新登录 GitHub',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#github-device-code').value === 'ABCD-1234');
    authState = {status:'signed_in',account:'researcher',code:'',message:'已登录 GitHub：researcher'};
    await page.getByRole('button',{name:'检查登录状态',exact:true}).waitFor();
    assert.equal(await page.locator('[data-github-device]').isHidden(),true);
    assert.equal(await page.locator('[data-sync-button]').isDisabled(), true);
    assert.match(await page.locator('.sync-stats').textContent(), /3/);
    await page.locator('#remote').fill('https://github.com/example/research-hub.git');
    await page.getByRole('button',{name:'保存仓库设置',exact:true}).click();
    assert.equal(await page.locator('[data-sync-button]').isEnabled(), true);
    assert.match(await page.locator('.sync-stats').textContent(), /已配置/);
    await page.screenshot({path:path.join(output,'github-sync-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.locator('.github-new-computer summary').click();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth),false);
    await page.screenshot({path:path.join(output,'github-new-computer-mobile.png'),fullPage:true});
    for (const route of ['/', '/library/paper','/library/experiment','/library/dataset','/library/idea','/library/writing','/new/paper','/new/experiment','/backup','/sync','/assistant','/imports', new URL(paperUrl).pathname,new URL(experimentUrl).pathname]) {
      await page.goto(base + route);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      assert.equal(overflow,false,`Horizontal overflow: ${route}`);
    }
    await page.goto(base);
    await page.getByRole('button',{name:'展开导航'}).click();
    assert.equal(await page.getByRole('button',{name:'展开导航'}).getAttribute('aria-expanded'),'true');
    await page.getByRole('link',{name:'文献笔记',exact:true}).first().click();
    await page.getByRole('heading',{name:'文献笔记',exact:true}).waitFor();
    await page.goto(base);
    await page.screenshot({path:path.join(output,'workspace-mobile.png'),fullPage:true});
    await page.goto(experimentUrl);
    await page.screenshot({path:path.join(output,'experiment-mobile.png'),fullPage:true});
    if (process.env.TEST_ASSISTANT === '1') {
      // Requires preview_server.py --fake-codex. This never calls the actual model.
      await page.setViewportSize({width:1440,height:1080});
      await page.goto(paperUrl);
      await page.getByRole('link',{name:'与 Codex 讨论',exact:true}).click();
      assert.equal(new URL(page.url()).pathname,new URL(paperUrl).pathname);
      assert.match(await page.locator('.paper-chat-context').textContent(),/仅本篇文献/);
      await page.locator('.codex-setup summary').click();
      await page.getByRole('button',{name:'检查本机登录状态',exact:true}).click();
      await page.locator('[data-codex-status]').filter({hasText:'隔离测试'}).waitFor();
      await page.getByRole('button',{name:'读取论文链接',exact:true}).click();
      await page.waitForURL('**/entry/**#paper-discussion');
      await page.locator('[data-paper-source-status]').filter({hasText:'已提取正文'}).waitFor();
      assert.equal(await page.locator('#chat-project').inputValue(),projectId);
      await page.locator('[data-model-select]').selectOption('__custom__');
      await page.locator('[data-custom-model]').fill('browser-test-model');
      await page.locator('[data-effort-select]').selectOption('high');
      assert.match(await page.locator('[data-model-summary]').textContent(),/browser-test-model.*high/);
      await page.locator('#chat-message').fill('根据这条笔记帮我设计对照实验');
      await page.getByRole('button',{name:'发送给 Codex',exact:true}).click();
      await page.locator('.chat-message.assistant.complete').waitFor();
      await page.locator('.chat-message.assistant .katex').first().waitFor();
      assert.equal(await page.locator('.chat-message.assistant .katex').count(),2);
      assert.match(await page.locator('.chat-message.assistant').textContent(),/browser-test-model/);
      assert.match(await page.locator('.chat-message.assistant').textContent(),/high/);
      assert.match(page.url(), /thread=/);
      assert.equal(await page.locator('.chat-message').count(),2);
      assert.ok(await page.locator('.chat-sources a').count() >= 1);
      assert.equal(await page.locator('.chat-sources a').count(),1);
      assert.match(await page.locator('.chat-sources a').textContent(),/原文/);
      const threadUrl = page.url();
      await page.locator('#chat-message').fill('继续细化研究假设');
      await page.locator('#chat-message').press('Control+Enter');
      await page.waitForFunction(() => document.querySelectorAll('.chat-message').length===4);
      await page.reload();
      assert.equal(await page.locator('[data-model-select]').inputValue(),'__custom__');
      assert.equal(await page.locator('[data-custom-model]').inputValue(),'browser-test-model');
      assert.equal(await page.locator('[data-effort-select]').inputValue(),'high');
      assert.equal(await page.locator('.chat-message').count(),4);
      assert.equal(await page.locator('.chat-message.assistant .katex').count(),4);
      await page.screenshot({path:path.join(output,'research-assistant-desktop.png'),fullPage:true});
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth>window.innerWidth),false);
      await page.screenshot({path:path.join(output,'research-assistant-mobile.png'),fullPage:true});
      await page.locator('.chat-draft-form').last().locator('[name=kind]').selectOption('idea');
      await page.locator('.chat-draft-form').last().locator('[name=target_id]').selectOption('');
      await page.locator('.chat-draft-form').last().locator('button').click();
      assert.match(await page.locator('#content').inputValue(), /待验证的假设/);
      assert.equal(await page.locator('form.editor-form').getAttribute('action'),'/new/idea');
      await page.locator('#title').fill('已审核的研究想法（隔离测试）');
      await page.getByRole('button',{name:'保存想法',exact:true}).click();
      await page.getByRole('heading',{name:'已审核的研究想法（隔离测试）',exact:true}).waitFor();
      await page.goto(threadUrl);
      await page.locator('.chat-draft-form').first().locator('[name=target_id]').selectOption(new URL(paperUrl).pathname.split('/').pop());
      await page.locator('.chat-draft-form').first().locator('button').click();
      assert.match(await page.locator('#content').inputValue(), /浏览器实际修改。/);
      assert.match(await page.locator('#content').inputValue(), /与 Codex 的讨论草稿/);
      // Deliberately leave this append draft unsaved and confirm original content was not changed.
      await page.goto(paperUrl);
      assert.equal(await page.getByRole('heading',{name:'与 Codex 的讨论草稿（待审核）',exact:true}).count(),0);
    }
    if (process.env.TEST_DATA_DIR) {
      await page.setViewportSize({width:1440,height:1080});
      const tick = String.fromCharCode(96);
      const tex = String.raw`# 终端导入的表示学习讨论

行内 $z_t=f_\theta(o_t)$ 和 \(d(z_i,z_j)\)。

$$
L=-\log\frac{\exp(s^+/\tau)}{\sum_j\exp(s_j/\tau)}
$$

\[x < y\]

$$
\begin{aligned}
z_t &= f_\theta(o_t) \\
a_t &= g_\phi(z_t)
\end{aligned}
$$

粘贴的窗口 \\(H\\)，动作表示 \\(\widetilde A\\)。
使用 DTW 得到：
\[
d_{ij}^{H}=\operatorname{DTW}(A_i,A_j)
\]
继续权重：
\[
w=\begin{cases}
1-F_H(d_{ij}^H), & \text{有效正样本}\\
0, & \text{其他}
\end{cases}
\]
后续正文。

坏公式 $\unsupportedmacro$ 应保留原文。

链接命令 $\href{javascript:alert(1)}{禁止链接}$ 不得产生可点击链接。
` + ['', '', '代码 '+tick+'$not_math$'+tick+'。', '', tick.repeat(3)+'text',
       String.raw`\(literal_code\)`, tick.repeat(3)].join('\n');
      const draftFile = path.join(process.env.TEST_DATA_DIR,'terminal draft.md');
      fs.writeFileSync(draftFile,tex);
      const result = execFileSync(path.join(__dirname,'../.venv/bin/python'),[
        path.join(__dirname,'../research_cli.py'),'--data-dir',process.env.TEST_DATA_DIR,'--port','8766',
        'import',draftFile,'--kind','paper','--project',projectId
      ],{encoding:'utf8'});
      const draftUrl = result.match(/http:\/\/127\.0\.0\.1:8766\/imports\/[a-f0-9]{32}/)[0];
      await page.goto(`${base}/imports`);
      assert.equal(await page.getByRole('link',{name:/终端导入的表示学习讨论/}).count(),1);
      await page.goto(draftUrl);
      await page.locator('.import-preview .katex').first().waitFor();
      assert.ok(await page.locator('.import-preview .katex').count()>=9);
      assert.equal(await page.locator('.import-preview .katex-display').count(),5);
      assert.equal(await page.locator('.import-preview .math-fallback').count(),1);
      assert.equal(await page.locator('.import-preview .katex .mtable').count()>=2,true);
      assert.equal(await page.locator('.import-preview pre .katex,.import-preview code .katex').count(),0);
      assert.match(await page.locator('.import-preview .math-fallback').textContent(),/unsupportedmacro/);
      assert.equal(await page.locator('.import-preview .arithmatex a').count(),0);
      assert.equal(await page.locator('#project_id').inputValue(),projectId);
      await page.evaluate(()=>document.fonts.ready);
      await page.screenshot({path:path.join(output,'terminal-import-formulas-desktop.png'),fullPage:true});
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
      await page.screenshot({path:path.join(output,'terminal-import-formulas-mobile.png'),fullPage:true});
      await page.locator('#import-kind').selectOption('writing');
      await page.getByRole('button',{name:'切换类型',exact:true}).click();
      assert.equal(await page.locator('form.editor-form').getAttribute('action'),'/new/writing');
      await page.locator('#title').fill('审核后的公式讨论');
      await page.getByRole('button',{name:'保存草稿',exact:true}).click();
      const savedUrl = page.url();
      await page.getByRole('heading',{name:'审核后的公式讨论',exact:true}).waitFor();
      await page.waitForFunction(()=>document.querySelectorAll('.prose .katex').length>=9);
      assert.ok(await page.locator('.prose .katex').count()>=9);
      await page.goto(draftUrl);
      assert.equal(page.url(),savedUrl);
      // Also exercise webpage upload; preview alone must not create a record.
      await page.goto(`${base}/imports`);
      await page.locator('#import-file').setInputFiles({name:'网页草稿.md',mimeType:'text/markdown',buffer:Buffer.from('# 网页上传待审核\n\n公式 $a=b$。')});
      await page.getByRole('button',{name:'预览并审核草稿',exact:true}).click();
      await page.locator('.import-preview .katex').waitFor();
      await page.goto(`${base}/search?q=网页上传待审核`);
      assert.equal(await page.locator('.search-results > a').count(),0);
    }
    if (process.env.TEST_ASSISTANT==='1') {
      await page.setViewportSize({width:1440,height:1000});
      await page.goto(`${base}/learning`);
      assert.equal(await page.locator('[data-custom-model]').inputValue(),'browser-test-model');
      assert.equal(await page.locator('[data-effort-select]').inputValue(),'high');
      await page.locator('[data-model-reset]').click();
      assert.equal(await page.locator('[data-model-select]').inputValue(),'');
      assert.equal(await page.locator('[data-effort-select]').inputValue(),'');
      assert.equal(await page.locator('[data-custom-model]').isDisabled(),true);
      await page.locator('[data-model-select]').selectOption('gpt-6.1-sol');
      await page.locator('[data-effort-select]').selectOption('medium');
      await page.getByRole('button',{name:'ACT 框架',exact:true}).click();
      assert.match(await page.locator('#learning-topic').inputValue(),/CVAE/);
      await page.locator('#learning-project').selectOption(projectId);
      await page.getByRole('button',{name:'查资料并整理',exact:true}).click();
      await page.locator('[data-learning-progress].is-running').waitFor();
      await page.waitForFunction(()=>document.querySelector('[data-learning-request-settings]').textContent.includes('gpt-6.1-sol'));
      assert.match(await page.locator('[data-learning-request-settings]').textContent(),/medium/);
      await page.waitForFunction(()=>document.querySelector('[data-learning-events]').textContent.includes('已找到原论文'));
      assert.equal(await page.locator('[data-learning-sources] a').count(),1);
      await page.waitForFunction(()=>document.querySelector('[data-learning-clock]').textContent!=='00:00');
      const pendingUrl=page.url();
      assert.match(pendingUrl,/learning\?job=/);
      await page.screenshot({path:path.join(output,'learning-progress-pending.png'),fullPage:true});
      await page.reload();
      assert.equal(await page.locator('[data-model-select]').inputValue(),'gpt-6.1-sol');
      assert.equal(await page.locator('[data-effort-select]').inputValue(),'medium');
      await page.locator('[data-learning-progress].is-running').waitFor();
      await page.locator('[data-learning-status]').getByRole('link',{name:'审核并保存知识卡',exact:true}).waitFor();
      assert.equal(await page.locator('[data-learning-progress].is-running').count(),0);
      assert.equal(await page.locator('[data-learning-jobs] [data-job-status]').first().textContent(),'草稿已生成，待审核');
      assert.match(await page.locator('[data-learning-events]').textContent(),/检查检索记录/);
      await page.locator('.learning-output summary').click();
      await page.locator('[data-learning-output] .katex').first().waitFor();
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
      await page.screenshot({path:path.join(output,'learning-progress-mobile.png'),fullPage:true});
      await page.setViewportSize({width:1440,height:1000});
      await page.screenshot({path:path.join(output,'learning-topic-desktop.png'),fullPage:true});
      await page.locator('[data-learning-status]').getByRole('link',{name:'审核并保存知识卡',exact:true}).click();
      await page.locator('.import-preview .katex').first().waitFor();
      assert.equal(await page.locator('#knowledge_category').inputValue(),'模型架构');
      assert.equal(await page.locator('#project_id').inputValue(),projectId);
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
      await page.screenshot({path:path.join(output,'knowledge-review-mobile.png'),fullPage:true});
      await page.locator('#title').fill('审核后的 ACT 知识卡');
      await page.getByRole('button',{name:'保存知识卡',exact:true}).click();
      await page.getByRole('heading',{name:'审核后的 ACT 知识卡',exact:true}).waitFor();
      await page.locator('.prose .katex').first().waitFor();
      await page.goto(`${base}/library/knowledge?category=模型架构&q=ACT`);
      assert.equal(await page.locator('.record-card').count(),1);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
      await page.screenshot({path:path.join(output,'knowledge-library-mobile.png'),fullPage:true});
      await page.goto(`${base}/learning`);
      assert.equal(await page.getByRole('link',{name:'审核知识卡',exact:true}).count(),1);
    }
    assert.deepEqual(errors,[]);
    assert.deepEqual(externalRequests,[],'Math must not request a CDN or external service.');
    console.log('Browser checks passed: owner login, records, attachments, exports, GitHub settings/login (mocked), mobile routes, navigation, no console errors.' + (process.env.TEST_ASSISTANT==='1' ? ' Assistant: inline paper-only discussion, PDF reading/status (fake reader), Codex login check (fake CLI), multi-turn/reload, review/save and local formulas. Learning: topic → async draft → review/edit → knowledge card, formulas, classification/search, project and task history (fake search/CLI).' : '') + (process.env.TEST_DATA_DIR ? ' Terminal CLI → inbox → preview → type/project → save, replay protection, upload, mobile formulas, invalid TeX fallback, no math in code, no trusted links.' : '') + ' No external requests or GitHub operations.');
  } finally {
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
