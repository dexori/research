# 研序 · Research Hub

一个在当前使用的电脑上运行的私人科研工作台。通过浏览器管理文献、实验、数据位置、研究想法和论文草稿，使用 GitHub 私有仓库同步资料。完成上传后，原来的电脑可以关机。

## 开始使用

需要 Python 3.10 或更新版本及 Git。

```bash
cd /home/z/research-hub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

打开 http://127.0.0.1:8765 ，首次访问设置至少 10 个字符的密码。默认仅本机访问，使用 Waitress 运行，不开启调试模式。

## 连接 GitHub，换电脑继续使用

1. 在 GitHub 创建一个自己的**私有空仓库**，例如 `research-hub`，不要预填 README 或其他文件。
2. 在当前电脑配置 Git 的 SSH 密钥或 HTTPS 凭据管理器，保证可以读写该仓库。平台不会要求你把令牌填入网页或仓库地址。
3. 打开工作台的「GitHub 同步」，填写仓库地址及分支（通常为 `main`），保存设置。
4. 点击「同步到 GitHub」，成功后代码、笔记和附件就在仓库中，可以关闭当前电脑。

在另一台电脑上，先安装 Python 和 Git，配置自己的 GitHub 访问凭据，然后克隆同一仓库：

```bash
git clone git@github.com:你的用户名/research-hub.git
cd research-hub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

打开 http://127.0.0.1:8765 ，在新电脑设置本机访问密码。平台会自动从仓库恢复笔记、项目、实验记录与附件。原电脑无需在线。

Windows PowerShell 可使用：

```bash
git clone git@github.com:你的用户名/research-hub.git
cd research-hub
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

两边每天使用的流程是：**开始时同步 → 网页编辑并保存 → 离开前再次同步**。看到同步成功再关机。网页保存立即写入本机文件，但不会自动联网上传；离线可工作，联网后重试同步。

已经克隆过的电脑也可以手动拉取：

```bash
git pull --ff-only
.venv/bin/python app.py
```

手动拉取后，刷新网页即可载入新内容。存在本机未上传的修改时，优先使用平台同步按钮，它会先保留本机提交，再合并远端变化。请勿直接用强制覆盖或重置工作区的方式更新资料。

不同记录的修改可以合并；同一份笔记产生冲突时，页面会显示两个版本。可选择「保留两份」，另一个版本会成为新记录，包括其附件。处理完冲突后再次同步上传。临时的原版本副本还会留在本机 `data/sync-conflicts/`。

## 已实现

- 文献：作者、会议/年份、链接、BibTeX、Markdown 阅读笔记、PDF 附件。
- 项目：研究问题、假设、计划，关联文献、实验、数据、想法和草稿。
- 实验：模型、数据集、随机种子、主指标名称与数值、参数、代码版本、WandB/MLflow 链接。
- 数据与模型：存储路径、版本、外部链接、使用记录。
- 想法和论文草稿：Markdown 编辑、保存、排版与导出。
- 全局全文搜索、分类搜索、按项目和状态筛选、标签、归档和恢复。
- 文件上传下载（每次请求最多 64 MB）；大数据和权重通过路径或链接管理。
- Markdown 资料文件与附件持久化；本机 SQLite 提供检索和页面数据，拉取后自动恢复。
- 网页 GitHub 同步：本机提交、拉取、合并和推送，显示待上传修改并支持冲突保留两份。
- 编辑时检查本机记录版本，避免旧页面覆盖刚同步的新内容。
- Markdown 导出、实验 CSV 导出、包含数据库和所有附件的完整 ZIP 备份。
- 密码登录、CSRF 防护、Markdown HTML 清理、附件强制下载。

## 存储、迁移与 GitHub

同步资料保存在工作区：

```text
research-hub/
├── app.py、templates/、static/       # 网页应用代码
├── workspace/
│   ├── records/<记录ID>.md          # 正文 + 可恢复的元信息
│   └── attachments/<附件ID>/        # PDF、图片、CSV 等原始文件
└── data/                            # 仅本机：数据库、访问密码、会话密钥
```

`workspace/` 与应用代码一起进入 GitHub 私有仓库。每条记录都是独立 Markdown 文件，前面的 JSON 元信息保存标题、类型、项目关联、标签和附件信息，后面是可编辑的 Markdown 正文。请保留记录 ID 和元信息格式，直接删除文件不等同于归档；建议在网页归档资料。

本机数据保存在 `data/research.sqlite3` 和 `data/uploads/`，`data/session.key` 是登录签名密钥。`data/` 被 Git 忽略；密码、登录密钥和运行中的 SQLite 不会上传。每台电脑使用自己的本机访问密码。可用 `RESEARCH_DATA_DIR=/绝对路径` 更换本机缓存位置。

附件在仓库中使用稳定 ID 和通用文件名，原始文件名保存在笔记元信息中，下载时恢复原名。记录使用 LF 换行，附件禁止 Git 换行转换，以支持 Linux、macOS 和 Windows 之间切换。

完整备份包含 `research.sqlite3`、`records.json`、分类 Markdown 和 `attachments/`。恢复时先停止服务，准备一个**新的空数据目录**，复制备份中的 `research.sqlite3`，将 `attachments/` 改名为 `uploads/`，设置 `RESEARCH_DATA_DIR` 指向新目录后启动。原访问密码继续有效，新的会话密钥会自动生成。

第一次启动新版本会将已有本机笔记和附件迁移到 `workspace/`，保留原本的数据。完整备份仍可用于额外存档；新电脑正常使用只需克隆或拉取 Git 仓库。不要让两个实例同时操作同一个 SQLite 文件。

当前同步为网页按钮触发，不是每次保存自动上传。尚未接入 Zotero、DVC、训练作业监控、MLflow/WandB API 或 Overleaf；已有服务可以通过链接关联。论文 PDF、CSV 和小图片随仓库同步，每次附件上传上限 64 MB；大型数据集和权重通过存储路径或链接管理，没有配置 Git LFS。

## 运行与验证

```bash
# 修改监听端口
.venv/bin/python app.py --port 8766

# 修改密码，同时撤销现有登录
.venv/bin/python app.py --set-password

# 功能和访问保护测试
.venv/bin/python -m unittest discover -s tests -v
```

默认仅本机访问，符合「当前电脑打开网页、GitHub 同步资料」的工作方式。如果以后要在可信局域网共享，可先使用 `--set-password` 设置密码，再运行 `app.py --host 0.0.0.0`。服务器运行示例见 `research-hub.service.example`。此项目不会自行安装系统服务或修改防火墙。
