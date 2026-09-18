# opencode-anywhere

集中管理 opencode 历史会话，并把**勾选的会话**导出成独立会话包，经 GitHub 私库（HTTPS）在多台 Windows 电脑之间增量同步。项目代码请照常用 Git 自行同步。

## 功能

- 在任意目录查看全部 opencode 历史会话
- 按标题、目录和会话状态搜索
- 快速进入会话，在对应工作目录启动 `opencode --session <id>`
- 查看会话详情：全部提问与对应回答，按 Markdown 渲染（标题/代码块/表格对齐/列表/可点击链接），可搜索、复制、导出 md
- 删除会话及其关联消息、数据块、事件和子会话
- 勾选任意会话导出成独立会话包上传到 GitHub 私库，对端按包并入（不影响其它会话）
- 上传前显示会话包体积与压缩后估算，超限会拒绝
- 浏览工作目录、打开文件和在资源管理器中定位

## 使用步骤

### 1. 准备（每台电脑一次）

需要 **opencode**（会话本体）、**Git**（会话包同步）、一个 **GitHub 私有仓库**。
全新机器可用脚本把缺的组件（含 opencode 依赖的 Node.js）一次装齐，以**管理员**身份打开
PowerShell，进入 `tools` 目录后执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-remote.ps1
```

然后在 GitHub 建一个**私有**空仓库（例如 `opencode-sync`），并建一个 fine-grained PAT
（GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens）：

- **Repository access** → `Only select repositories` → 勾选该私库
- **Repository permissions** → `Contents` 设为 **Read and write**（上传必需）
- `Metadata` 为 **Read-only**，是 GitHub 强制自带的，不用改
- 其它权限（Actions、Issues、Pull requests…）都不用开

只给 `Contents: Read-only` 的话可以下载会话包，但上传会报 403。

### 2. 配置同步（每台电脑一次）

1. 启动 GUI（见[启动 GUI](#启动-gui)）
2. 进入「会话同步」页，填入私库地址和 PAT，点 **保存并准备仓库**

   地址默认按 **HTTPS** 方式访问，以下写法都可以，会自动补全为 `https://github.com/...`：

   ```text
   you/opencode-sync
   github.com/you/opencode-sync
   https://github.com/you/opencode-sync.git
   ```

   （如需走 SSH，直接填 `git@github.com:you/opencode-sync.git`，此时 PAT 可留空。）

3. 每台电脑都填**同一个**私库地址和 PAT
4. 首次使用 opencode 时自行登录 opencode（会话包只含会话数据，不含登录凭据）

### 3. 日常同步流程

以「A 机 → B 机」为例：

1. **A 机上传**：到「会话管理」勾选要同步的会话（可多选，支持搜索/筛选后全选），点 **上传所选会话**

   确认框会显示会话数、子会话数、本地数据量。上传时自动执行：导出会话包（含子会话、消息、数据块、事件）→ 压缩后体积守门 → commit → push。**可以随时上传，不必退出 opencode**

   会话包按 `bundles/<时间>-<主机名>.db` 命名，每次上传生成一个新包，因此可以多次增量上传

2. **B 机并入**：在「会话同步」页点 **刷新远端会话包**，列表会显示每个包的来源机器、上传时间、会话数、包大小、本机缺少的会话数；选中要并入的包（可多选），点 **下载并并入所选**

   「本机缺」是**该包里本机还没有的会话数**：大于 0 才值得并入（通常是另一台电脑上传的包）；本机自己上传的包恒为 0，因为那些会话本来就在本机。列表里绿色行表示本机缺，灰色为本机已有；勾选「只看本机缺的包」可过滤

   并入时**只覆盖同 ID 的会话**，其它会话和本地独有会话完全不受影响。并入前请先退出本机全部 opencode 窗口

3. **继续工作**：到「会话管理」按 F5 刷新，选中会话点「进入会话」即可续接

> 不再要求"同一时间只在一台电脑活跃"：同步是**按会话合并**，不是整库覆盖。
> 但同一个会话如果在两台电脑上都有新消息，后并入的一方会覆盖先前的版本。
>
> **注意**：两台电脑的项目路径最好一致（例如都是 `D:\PythonProjects\claudeproject`），否则会话在另一台续接时要先到对应目录把项目代码 clone/pull 回来。

### 4. 日常维护（可选）

- **刷新远端会话包**：查看远端有哪些会话包、是否已有同名会话
- **刷新统计**：本地库体积、WAL、压缩后估算、会话/消息/数据块数量
- **压缩数据库(VACUUM)**：删除会话后回收空闲页；需先退出全部 opencode，约需两倍磁盘空间
- 体积守门：单个会话包压缩后超过 50MB 预警、超过 95MB 拒绝上传（GitHub 单文件硬限 100MB）。超大旧会话请先在「会话管理」删除再同步
- 会话管理页「大小」列显示每个会话的占用（含子会话的 message/part/event 数据），可据此找出大会话
- 修改 `tools\ocp-gui.py` 后，运行 `tools\build-exe.ps1` 重新打包更新 EXE

## 目录结构

```text
opencode会话和文件同步/
├─ README.md
├─ .gitignore
└─ tools/
   ├─ app.ico
   ├─ ocp-gui.py          # GUI 主程序
   ├─ ocp-gui.cmd         # GUI 启动器
   ├─ ocp.py              # 终端会话选择器
   ├─ ocp.cmd
   ├─ install-remote.ps1  # 新机器一键装环境
   └─ build-exe.ps1       # 打包独立 EXE
```

## 环境要求

- Windows 10/11
- opencode
- Git（会话包同步依赖）
- 一个 GitHub 私有仓库（存放会话包）

以上组件缺哪补哪即可，`tools\install-remote.ps1` 会一次装齐（含 opencode 依赖的 Node.js）。

仅当从源码运行或自行打包 EXE 时，才需要 Python 3.10+：

```powershell
python -m pip install ttkbootstrap pyinstaller
```

`ttkbootstrap` 可选，装了才有主题皮肤，缺失时自动退回原生 ttk 控件。

## 启动 GUI

在新开的 PowerShell 或 CMD 中执行：

```powershell
tools\ocp-gui.cmd
```

也可以直接双击桌面快捷方式，或者执行：

```powershell
pythonw tools\ocp-gui.py
```

如果希望从任意目录启动，把 `tools` 目录加入用户 PATH。新开终端后即可使用：

```powershell
ocp-gui
```

`tools\ocp-gui.cmd` 会优先启动已打包的 `dist\opencode-anywhere.exe`；若不存在则回退到 `pythonw tools\ocp-gui.py`。

## 主题与窗口记忆

- 页面右上角有 **☀ 浅色 / ☾ 深色** 切换按钮，点击后保存偏好并重启，全部控件（含原生 Tk、ttk、Treeview、侧栏、菜单）一起换色。
- 主题偏好保存到 `%USERPROFILE%\.local\share\opencode\ocp-gui-theme.json`。
- 程序退出/移动/缩放后，窗口尺寸与位置会记录到 `%USERPROFILE%\.local\share\opencode\ocp-gui-window.json`，下次启动自动恢复。
- 启动后按各页面工具栏的实际所需宽度做一次自适应：若恢复的窗口过小，会自动扩大到能完整显示所有按钮，并限制在屏幕范围内。

## 打包为独立 EXE

独立 exe 能最稳定地保证任务栏与窗口左上角显示应用图标（而不是 Python 图标）。

```powershell
# 首次需安装打包工具
python -m pip install pyinstaller

# 一键打包（ttkbootstrap 依赖 Pillow，必须打入；只排除 numpy/pandas 等真正无用的重依赖）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build-exe.ps1
```

产物：`dist\opencode-anywhere.exe`（约 20MB，已内嵌 `tools\app.ico`）。`dist/`、`build/`、`*.spec` 已在 `.gitignore` 中忽略，不入库。修改 `tools\ocp-gui.py` 后重新运行该脚本即可更新 exe。

GUI 包含三个页面：

### 会话管理

- **搜索**：匹配会话标题和工作目录
- **目录**：只显示指定工作目录的会话
- **状态**：筛选 `空闲`、`近期活跃` 或 `运行中`
- **大小**：每个会话的占用估算（含子会话的 message/part/event 数据），底部状态栏显示会话合计
- **进入会话**：在原始工作目录启动新的 opencode 控制台
- **打开目录**：在资源管理器中打开会话工作目录
- **查看会话详情**：顶部显示该会话的目录/消息数/占用/更新时间，下面列出全部提问和对应回答（一次只能看一个会话）。列表里提问和回答都单行截断（已去掉 Markdown 标记），选中一行会在下方按 Markdown 排版显示完整问答，双击开大窗看全文；搜索框同时匹配提问和回答正文，可复制提问/回答/问答（复制到的是原始 Markdown），或导出成 `.md`（按当前筛选）
  - 回答取的是该回合**最后一步**（`finish=stop`）的文字。opencode 一个提问会派生多个步骤，中间步骤的文字是"先检查一下配置"这类过程旁白，不算回答，因此不会混进来
  - 回合没跑完（会话被中断或还在运行）的提问没有最终回答，会标注 `未完成` 并回退显示过程文字；整回合一个字都没有的标注 `无内容`
  - 内容渲染是内置的轻量 Markdown 排版（不依赖第三方库）：标题、粗体/斜体、行内代码、代码块、表格按列宽对齐（`---:` 右对齐，中文按两格宽）、有序/无序列表、引用、分割线，`[文字](链接)` 可点击
- **上传所选会话**：把勾选的会话（含子会话）导出成会话包并推送到私库
- **删除会话**：删除前显示关联数据量并要求二次确认

### 会话同步

- 配置 GitHub 私库地址（默认 HTTPS，填 `owner/repo` 会自动补全）+ PAT，一键 clone 到本地同步工作区；克隆前先用 GitHub API 预检权限，权限不足直接给出中文原因
- 同步工作区状态一目了然：`就绪` / `未准备`（附上次失败原因）/ `未配置`
- 本地库体积统计：库/WAL 大小、压缩后估算、会话/消息/数据块数量
- **刷新远端会话包**：拉取远端列表（上传时间、来源机器、会话数、包大小、本机缺少数）；绿色行=本机缺少（值得并入），灰色=本机已有，可用「只看本机缺的包」过滤
- **下载并并入所选**：把选中会话包里的会话并入本地库；只覆盖同 ID 会话，其它会话不受影响。若所选包本机已全部拥有，会先提示确认（并入不会新增内容）
- **压缩数据库(VACUUM)**：删除会话后回收空闲页，缩小库文件
- 体积守门：单个会话包按压缩后估算，>50MB 预警，>95MB 拒绝上传

### 目录浏览

- 浏览工作目录的文件内容
- 双击进入目录或打开文件
- 右键在资源管理器中定位
- 复制完整路径
- 橙色表示历史同步冲突文件，红色表示同步临时残留

## 会话删除判定

程序不会因为任意一个 opencode 进程存在就禁止删除全部会话。

### 运行中

程序通过 WMI 检查 `opencode.exe` 的命令行参数。如果命令行中包含：

```text
--session ses_xxx
-s ses_xxx
```

则精确识别该会话为 `运行中`，该会话不能删除。

### 近期活跃

对于直接执行 `opencode`、没有在命令行中明确写出会话 ID 的情况，程序会检查该会话及其子会话最近是否有消息或数据块活动：

- 5 分钟内有活动：标记为 `近期活跃`
- 超过 5 分钟无活动：标记为 `空闲`

删除 `近期活跃` 会话时会额外要求确认。建议确认相关 opencode 窗口已经退出。

### 删除范围

删除顶层会话时，会同时删除其子会话，以及关联的：

- `message`
- `part`
- `todo`
- `session_share`
- `session_input`
- `session_message`
- `session_context_epoch`
- `event`
- `event_sequence`

删除操作不可恢复。程序会在同一事务中执行数据库删除，失败时回滚。

## 多机共享方案

同步不再整库覆盖，而是**按会话导出成独立会话包**，通过 GitHub 私库中转。

### 工作方式

- 上传：勾选会话 → 导出会话包 → 压缩后体积守门 → `commit`/`push` 到私库 `bundles/` 目录
- 会话包是一个独立的 SQLite 文件，只包含所选会话（连同子会话）及其关联数据：
  `session`（含父会话）、`message`、`part`、`todo`、`session_share`、`session_input`、
  `session_message`、`session_context_epoch`、`event`、`event_sequence`，
  以及这些会话依赖的 `project` / `project_directory` / `workspace` 元数据
- 包内附 `ocp_manifest` 表，记录来源主机、上传时间和会话清单（标题/目录/消息数/数据量）
- 下载：拉取远端 → 列出会话包 → 选中后**并入本地库**。并入时先删除本地同 ID 会话的关联行，再以包内数据覆盖，整个过程在同一事务内完成，失败自动回滚；本地其它会话不受影响
- 本地同步工作区：`%LOCALAPPDATA%\opencode-git-sync`（私库的 clone）
- **不包含**：登录凭据（`account`/`credential` 等）、项目代码、磁盘上的 `tool-output/` 附件。另一台电脑需要自行登录 opencode，项目代码请用 Git 同步

### 安全须知

- **务必使用私有仓库**：会话内容（提示词、代码片段、工具输出）会明文存储在 GitHub 私库中
- 建议使用 fine-grained PAT，并严格限制到这一个仓库：`Repository access` 只勾该仓库，权限只需 `Contents: Read and write`（`Metadata: Read-only` 自带）。PAT 只保存在本机 `%USERPROFILE%\.local\share\opencode\ocp-sync.json`
- 使用 HTTPS + PAT 时，点 **保存并准备仓库** 会先调用 GitHub API 预检权限，权限不足会在克隆前直接提示原因，并列出该 PAT 当前能看到的仓库
- 也可以把 PAT 留空：此时走本机 git 凭据（如 Git Credential Manager 或 SSH），前提是这台机器已能访问该私库；填了 PAT 则 GUI 把 PAT 直接写进 git 地址，不经过凭据管理器，也就不会弹登录框
- 地址默认按 HTTPS 补全；若填 `git@...` 则走本机 git 凭据（SSH）
- 仓库会随每次上传保留历史提交，体积持续增长；可在 GitHub 上定期清理历史或压缩仓库

### 推荐约束

- 同一个会话不要在两端同时继续对话，后并入的一方会覆盖先前的版本
- 并入前先退出本机全部 opencode 窗口
- 两台电脑的项目路径最好保持一致，例如都使用：

```text
D:\PythonProjects\claudeproject
```

## Git 项目与会话同步

建议：

- 项目代码不进会话同步流程：有远端仓库的 Git 项目在另一台电脑使用 `git clone`，日常 `git pull/push`
- 会话同步只用 GUI 的「上传所选会话 / 下载并并入所选」，不要手动往同步工作区提交其他文件

## 自检与测试

GUI 主程序支持无界面自检：

```powershell
python tools\ocp-gui.py selftest
```

自检包括：

- 会话数据库读取
- SQLite schema 副本中的级联删除
- 会话库体积统计与同步配置状态
- 活跃会话检测
- opencode 可执行文件检测
- 会话包导出/并入往返（在真实 schema 的临时库上验证：子会话随包导出、按包覆盖同名会话、重复并入幂等、本地独有会话保留）
- 私库地址规范化与错误提示（`owner/repo` 补全为 HTTPS、`git@` 识别、各类 git 报错转中文可执行提示）
- 后台线程结果回传（异常/成功/缺 `on_err` 兜底）
- 会话包标记（`new`/`own`/`old`）
- 有 PAT 时禁用 git 凭据助手
- 提问与回答提取（最终回答、未完成回退、无内容、无 assistant 四种情形，子会话不混入，md 导出结构）
- Markdown 解析与渲染（标题/段落/代码块/表格右对齐/有序无序列表/引用/分割线/未闭合容错，并在真实 Tk 控件上验证渲染不抛异常且 tag 正确应用）

GUI 冒烟测试：

```powershell
python tools\ocp-gui.py smoke
```

## 常见问题

### 点击删除没有反应

确认当前运行的是最新的 `tools\ocp-gui.py`。中文 Windows 下不能依赖 ttkbootstrap 对话框返回的英文 `Yes` 字符串；当前版本已经改用标准 `tkinter.messagebox` 并返回布尔值。

### 打开 GUI 后不断弹出 CMD

当前版本已经给 WMI、`tasklist` 和 git 子进程增加 `CREATE_NO_WINDOW`。如果仍然弹出，确认启动的是项目目录下最新的 `ocp-gui.cmd`，而不是旧的 `%USERPROFILE%\bin\ocp-gui.cmd`。

### 另一台电脑看不到历史会话

依次检查：

1. 是否已在「会话同步」页配置同一私库并点过 **刷新远端会话包**，再选中包点 **下载并并入所选**
2. 并入完成后到「会话管理」按 F5 刷新
3. 该机的 opencode 是否已安装，并且已登录（会话包不含登录凭据）
4. 会话目录不存在时，需要先在该机把项目代码 clone/pull 到同一路径
5. 是否启动的是项目 `tools` 目录中的 GUI

### 保存并准备仓库失败

先在「会话同步」页看 **同步工作区** 这一行，它会显示状态和上次失败原因：

- `未配置`：还没填私库地址和 PAT，或没点过 **保存并准备仓库**
- `未准备`：克隆没成功。常见提示与处理：

| 提示 | 原因 | 处理 |
| --- | --- | --- |
| `PAT 访问不到 owner/repo (404)` | token 的 Repository access 没勾这个仓库、地址写错、或仓库不存在 | 到 token 设置页勾上该仓库（见[准备](#1-准备每台电脑一次)），或核对地址。提示里会列出该 PAT 当前能看到的仓库，可直接对比 |
| `PAT 对 owner/repo 只有只读权限(403)` | `Contents` 给的是 Read-only | 改为 `Read and write` |
| `PAT 无效或已过期(401)` | token 被吊销或过期 | 重新生成后粘贴 |
| `git 需要交互式凭证但已禁用` | 无 PAT 且本机没有可用 git 凭据 | 改用 HTTPS+PAT，或先行配置 SSH/凭据管理器 |

失败时会自动清理残留的空工作区（`%LOCALAPPDATA%\opencode-git-sync`），修正权限后直接再点一次 **保存并准备仓库** 即可，不需要手动删目录。

如果只是想临时绕过 PAT，把 PAT 框清空再点保存，会走本机 git 凭据。

### 推送时反复要求「选择登录方式」（通常是换过 PAT 之后）

那是 **Git Credential Manager (GCM)** 的弹窗：它拿缓存里**旧的、已失效的凭据**去认证，被 GitHub 拒绝后就每次都重新问一遍。按下面处理：

1. 先清掉失效的凭据（`oofenglvoo` 换成你的账号名，`x-access-token` 那条是 GUI 会话同步留下的，可一并清掉）：

   ```powershell
   "protocol=https`nhost=github.com`nusername=oofenglvoo`n`n" | git credential reject
   git credential-manager github list          # 看还剩哪些
   ```

2. 重新登录一次，把有效凭据写回凭据管理器：

   ```powershell
   git credential-manager github login --browser --username oofenglvoo --force
   ```

   - 浏览器会打开一个 `http://127.0.0.1:<端口>` 的**本地回调页**，这是正常的；该页面只在登录命令**运行期间**有效，所以授权完成前别关命令行窗口
   - 提示 `Account '...' already has credentials` 就是缺 `--force`
   - 浏览器不方便时改用设备码，再把打印出来的码填到 https://github.com/login/device ：

     ```powershell
     git credential-manager github login --device --username oofenglvoo --force
     ```

3. 验证：到项目里 `git push`，不再弹窗即可。想不动仓库地验证，用 `git push --dry-run`（注意：分支已是最新时会走匿名读取、看不出认证问题）。

凭据管理器里如果同时存在两条 github.com 记录（例如 `oofenglvoo` 和 `x-access-token`），GCM 每次都会弹窗让你选**用哪个账号**。`x-access-token` 是 git 把地址里的 PAT 通过 `credential approve` 存进凭据管理器留下的（旧版本会这样），清掉它即可：

```powershell
"protocol=https`nhost=github.com`nusername=x-access-token`n`n" | git credential reject
git credential-manager github list        # 确认只剩 oofenglvoo
```

现在 GUI 在有 PAT 时会用 `-c credential.helper=` 调 git，**既不查也不存**凭据管理器，所以不会再自动攒出这种记录；只有把 PAT 留空、回落到本机凭据时才可能弹。

### 上传失败

- 提示克隆/push 失败：检查网络、私库地址、PAT 权限（需要对该仓库 `Contents: Read and write`）
- 提示 `未就绪`：说明私库还没准备好，按上一条处理；提示里会带上上次失败原因
- 提示会话包体积超限：单个包压缩后超过 95MB。到「会话管理」按「大小」列找出大会话，减少勾选范围，或删除过大的旧会话后再上传
- 看不到远端会话包：确认上传后点过 **刷新远端会话包**；远端为空说明还没成功上传过

### 并入后发现会话内容不是最新

同一会话在两台电脑都有改动时，以**最后并入**的一方为准。核实后重新上传/并入需要的版本即可。
并入前会先删除本地同 ID 会话再写入包内数据，因此不会产生重复会话；如仍需回退，可用 `%USERPROFILE%\.local\share\opencode\opencode.db.bak-<时间>` 之类的历史备份找回。

## 许可证

当前仓库未单独声明许可证。如需公开发布，请根据使用的代码、图标和依赖补充合适的许可证文件。
