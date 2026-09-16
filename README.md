# opencode-anywhere

集中管理 opencode 历史会话，并把**勾选的会话**导出成独立会话包，经 GitHub 私库（HTTPS）在多台 Windows 电脑之间增量同步。项目代码请照常用 Git 自行同步。

## 功能

- 在任意目录查看全部 opencode 历史会话
- 按标题、目录和会话状态搜索
- 快速进入会话，在对应工作目录启动 `opencode --session <id>`
- 删除会话及其关联消息、数据块、事件和子会话
- 勾选任意会话导出成独立会话包上传到 GitHub 私库，对端按包并入（不影响其它会话）
- 上传前显示会话包体积与压缩后估算，超限会拒绝
- 浏览工作目录、打开文件和在资源管理器中定位

## 使用步骤

### 第 1 步：A 机（第一台电脑）准备

1. 按[环境要求](#环境要求)安装 Python、opencode、Git 和 `ttkbootstrap`
2. 在 GitHub 建一个**私有**空仓库（例如 `opencode-sync`）
3. 建一个 fine-grained PAT，只授权这一个仓库（GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens）：

   - **Repository access** → `Only select repositories` → 勾选刚才建的私库
   - **Repository permissions** → `Contents` 设为 **Read and write**（上传必需）
   - `Metadata` 为 **Read-only**，是 GitHub 强制自带的，不用改
   - 其它权限（Actions、Issues、Pull requests…）都不用开

   只给 `Contents: Read-only` 的话可以下载会话包，但上传会报 403
4. 启动 GUI：

   ```powershell
   tools\ocp-gui.cmd
   ```

5. 进入「会话同步」页，填入私库地址和 PAT，点 **保存并准备仓库**

   地址默认按 **HTTPS** 方式访问，以下写法都可以，会自动补全为 `https://github.com/...`：

   ```text
   you/opencode-sync
   github.com/you/opencode-sync
   https://github.com/you/opencode-sync.git
   ```

   （如需走 SSH，直接填 `git@github.com:you/opencode-sync.git`，此时 PAT 可留空。）

6. 填入后 A 机即可正常使用：会话管理、目录浏览等功能立即生效

### 第 2 步：B 机（第二台电脑）环境安装

1. 把整个 `tools` 文件夹复制到 B 机任意目录
2. 在 B 机以**管理员**身份打开 PowerShell，进入 `tools` 目录后执行：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\install-remote.ps1
   ```

   脚本只负责**装环境**：安装缺失的 Git/Node.js/Python、安装 `opencode-ai` 和 `ttkbootstrap`、加桌面快捷方式、把 `tools` 加入 PATH。会话同步全部在 GUI 里完成

3. 首次在 B 机使用 opencode 时，请自行登录 opencode（会话包只含会话数据，不含登录凭据）

### 第 3 步：B 机配置同步

1. 打开 B 机 GUI（`ocp-gui`），进入「会话同步」页
2. 填入**同一个**私库地址和 PAT，点 **保存并准备仓库**

### 第 4 步：日常同步流程

方向 A（A 机 → B 机）：

1. **A 机上传**：到「会话管理」勾选要同步的会话（可多选，支持搜索/筛选后全选），点 **上传所选会话**

   确认框会显示会话数、子会话数、本地数据量。上传时自动执行：导出会话包（含子会话、消息、数据块、事件）→ 压缩后体积守门 → commit → push。**可以随时上传，不必退出 opencode**

   会话包按 `bundles/<时间>-<主机名>.db` 命名，每次上传生成一个新包，因此可以多次增量上传

2. **B 机并入**：在「会话同步」页点 **刷新远端会话包**，列表会显示每个包的来源机器、上传时间、会话数、包大小、本机缺少的会话数；选中要并入的包（可多选），点 **下载并并入所选**

   并入时**只覆盖同 ID 的会话**，其它会话和本地独有会话完全不受影响。并入前请先退出本机全部 opencode 窗口

3. **继续工作**：到「会话管理」按 F5 刷新，选中会话点「进入会话」即可续接

> 不再要求"同一时间只在一台电脑活跃"：同步是**按会话合并**，不是整库覆盖。
> 但同一个会话如果在两台电脑上都有新消息，后并入的一方会覆盖先前的版本。
>
> **注意**：两台电脑的项目路径最好一致（例如都是 `D:\PythonProjects\claudeproject`），否则会话在另一台续接时要先到对应目录把项目代码 clone/pull 回来。

### 第 5 步（可选）：日常维护

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
   └─ install-remote.ps1  # B 机环境安装
```

## 环境要求

- Windows 10/11
- Python 3.10 或更高版本（仅开发机需要；打包后的 EXE 自带运行时）
- opencode
- Git（会话同步与 opencode 的部分撤销/恢复能力都依赖 Git）
- Python 包：`ttkbootstrap`
- 一个 GitHub 私库（同步会话包）

安装 Python 依赖：

```powershell
python -m pip install ttkbootstrap
```

如果没有 Python，可以先使用官方安装包或 winget：

```powershell
winget install --id Python.Python.3.12 -e
```

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
- **上传所选会话**：把勾选的会话（含子会话）导出成会话包并推送到私库
- **删除会话**：删除前显示关联数据量并要求二次确认

### 会话同步

- 配置 GitHub 私库地址（默认 HTTPS，填 `owner/repo` 会自动补全）+ PAT，一键 clone 到本地同步工作区；克隆前先用 GitHub API 预检权限，权限不足直接给出中文原因
- 同步工作区状态一目了然：`就绪` / `未准备`（附上次失败原因）/ `未配置`
- 本地库体积统计：库/WAL 大小、压缩后估算、会话/消息/数据块数量
- **刷新远端会话包**：拉取远端列表（上传时间、来源机器、会话数、包大小、本机缺少数）
- **下载并并入所选**：把选中会话包里的会话并入本地库；只覆盖同 ID 会话，其它会话不受影响
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
- **不包含**：登录凭据（`account`/`credential` 等）、项目代码、磁盘上的 `tool-output/` 附件。B 机需要自行登录 opencode，项目代码请用 Git 同步

### 安全须知

- **务必使用私有仓库**：会话内容（提示词、代码片段、工具输出）会明文存储在 GitHub 私库中
- 建议使用 fine-grained PAT，并严格限制到这一个仓库：`Repository access` 只勾该仓库，权限只需 `Contents: Read and write`（`Metadata: Read-only` 自带）。PAT 只保存在本机 `%USERPROFILE%\.local\share\opencode\ocp-sync.json`
- 使用 HTTPS + PAT 时，点 **保存并准备仓库** 会先调用 GitHub API 预检权限，权限不足会在克隆前直接提示原因，并列出该 PAT 当前能看到的仓库
- 也可以把 PAT 留空：此时走本机 git 凭据（如 Git Credential Manager 或 SSH），前提是这台机器已能访问该私库
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

- 项目代码不进会话同步流程：有远端仓库的 Git 项目在 B 机使用 `git clone`，日常 `git pull/push`
- 会话同步只用 GUI 的「上传所选会话 / 下载并并入所选」，不要手动往同步工作区提交其他文件

## B 机初始化

1. 把 `tools` 文件夹复制到 B 机。
2. 使用管理员 PowerShell 运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-remote.ps1
```

脚本会尝试完成以下工作：

- 安装缺失的 Git、Node.js 和 Python
- 安装 `opencode-ai` 和 `ttkbootstrap`
- 创建 B 机桌面快捷方式
- 将工具目录加入 PATH

3. 打开 B 机 GUI「会话同步」页，填入与 A 机相同的私库地址和 PAT，点 **保存并准备仓库**。
4. 点 **刷新远端会话包**，选中要同步的包，点 **下载并并入所选**，然后到「会话管理」按 F5 查看。

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

GUI 冒烟测试：

```powershell
python tools\ocp-gui.py smoke
```

## 常见问题

### 点击删除没有反应

确认当前运行的是最新的 `tools\ocp-gui.py`。中文 Windows 下不能依赖 ttkbootstrap 对话框返回的英文 `Yes` 字符串；当前版本已经改用标准 `tkinter.messagebox` 并返回布尔值。

### 打开 GUI 后不断弹出 CMD

当前版本已经给 WMI、`tasklist` 和 git 子进程增加 `CREATE_NO_WINDOW`。如果仍然弹出，确认启动的是项目目录下最新的 `ocp-gui.cmd`，而不是旧的 `%USERPROFILE%\bin\ocp-gui.cmd`。

### B 机看不到历史会话

依次检查：

1. B 机是否已在「会话同步」页配置同一私库并点过 **刷新远端会话包**，再选中包点 **下载并并入所选**
2. 并入完成后到「会话管理」按 F5 刷新
3. B 机的 opencode 是否已安装，并且已登录（会话包不含登录凭据）
4. 会话目录不存在时，需要先在 B 机把项目代码 clone/pull 到同一路径
5. 是否启动的是项目 `tools` 目录中的 GUI

### 保存并准备仓库失败

先在「会话同步」页看 **同步工作区** 这一行，它会显示状态和上次失败原因：

- `未配置`：还没填私库地址和 PAT，或没点过 **保存并准备仓库**
- `未准备`：克隆没成功。常见提示与处理：

| 提示 | 原因 | 处理 |
| --- | --- | --- |
| `PAT 访问不到 owner/repo (404)` | token 的 Repository access 没勾这个仓库、地址写错、或仓库不存在 | 到 token 设置页勾上该仓库（见[第 1 步](#第-1-步a-机第一台电脑准备)），或核对地址。提示里会列出该 PAT 当前能看到的仓库，可直接对比 |
| `PAT 对 owner/repo 只有只读权限(403)` | `Contents` 给的是 Read-only | 改为 `Read and write` |
| `PAT 无效或已过期(401)` | token 被吊销或过期 | 重新生成后粘贴 |
| `git 需要交互式凭证但已禁用` | 无 PAT 且本机没有可用 git 凭据 | 改用 HTTPS+PAT，或先行配置 SSH/凭据管理器 |

失败时会自动清理残留的空工作区（`%LOCALAPPDATA%\opencode-git-sync`），修正权限后直接再点一次 **保存并准备仓库** 即可，不需要手动删目录。

如果只是想临时绕过 PAT，把 PAT 框清空再点保存，会走本机 git 凭据。

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
