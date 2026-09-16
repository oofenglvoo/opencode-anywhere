# opencode-anywhere

集中管理 opencode 历史会话，并通过 Syncthing 在多台 Windows 电脑之间共享会话数据、配置和工作目录。

## 功能

- 在任意目录查看全部 opencode 历史会话
- 按标题、目录和会话状态搜索
- 快速进入会话，在对应工作目录启动 `opencode --session <id>`
- 删除会话及其关联消息、数据块、事件和子会话
- 查看 Syncthing 共享目录状态和待同步大小
- 手动触发同步、暂停/恢复共享目录
- 编辑 Syncthing 忽略规则
- 浏览共享目录、打开文件和在资源管理器中定位
- 在多台 Windows 电脑之间安全切换活跃工作机

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
   ├─ oc-sync.ps1         # 同步切换逻辑
   ├─ oc-out.cmd
   ├─ oc-in.cmd
   ├─ oc-status.cmd
   ├─ oc-pair.ps1         # A 机配对 B 机
   ├─ oc-pair.cmd
   └─ install-remote.ps1  # B 机一键初始化
```

## 环境要求

- Windows 10/11
- Python 3.10 或更高版本
- opencode
- Syncthing 2.x
- Git（opencode 的部分撤销/恢复能力依赖 Git）
- Python 包：`ttkbootstrap`

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
- 启动后按各页面工具栏的实际所需宽度做一次自适应：若恢复的窗口过小，会自动扩大到能完整显示所有按钮（含“删除会话”和同步页“待传/暂停”列），并限制在屏幕范围内。

## 打包为独立 EXE

独立 exe 能最稳定地保证任务栏与窗口左上角显示应用图标（而不是 Python 图标）。

```powershell
# 首次需安装打包工具
python -m pip install pyinstaller

# 一键打包（ttkbootstrap 依赖 Pillow，必须打入；只排除 numpy/pandas 等真正无用的重依赖）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build-exe.ps1
```

产物：`dist\opencode-anywhere.exe`（约 13MB，已内嵌 `tools\app.ico`）。`dist/`、`build/`、`*.spec` 已在 `.gitignore` 中忽略，不入库。修改 `tools\ocp-gui.py` 后重新运行该脚本即可更新 exe。

GUI 包含三个页面：

### 会话管理

- **搜索**：匹配会话标题和工作目录
- **目录**：只显示指定工作目录的会话
- **状态**：筛选 `空闲`、`近期活跃` 或 `运行中`
- **进入会话**：在原始工作目录启动新的 opencode 控制台
- **打开目录**：在资源管理器中打开会话工作目录
- **删除会话**：删除前显示关联数据量并要求二次确认

### 共享同步

- 查看 Syncthing 是否运行
- 查看每个共享目录的同步状态和待传大小
- 立即扫描某个共享目录
- 暂停或恢复某个共享目录
- 编辑该目录的 `.stignore` 规则
- 执行换机前的收尾和新机接管流程

### 目录浏览

- 浏览三个共享目录的文件内容
- 双击进入目录或打开文件
- 右键在资源管理器中定位
- 复制完整路径
- 橙色表示 Syncthing 冲突文件，红色表示传输临时文件

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

本项目使用 Syncthing，而不是直接让两台电脑同时打开同一个 SQLite 数据库。

### 推荐约束

- 同一时间只在一台电脑上使用 opencode
- 换机前必须先退出旧电脑上的全部 opencode
- 旧电脑执行 `oc-out` 或 GUI 中的“本机收尾”
- 新电脑执行 `oc-in` 或 GUI 中的“接管本机”
- 两台电脑的项目路径保持一致，例如都使用：

```text
D:\PythonProjects\claudeproject
```

### 同步目录

| 名称 | 默认路径 | 内容 |
|---|---|---|
| `opencode-data` | `%USERPROFILE%\.local\share\opencode` | 会话数据库、快照和计划 |
| `opencode-config` | `%USERPROFILE%\.config\opencode` | opencode 配置、插件和 skills |
| `claudeproject` | `D:\PythonProjects\claudeproject` | 非 Git 工作目录和本项目 |

会话数据库中可能包含模型登录凭据。只应把 Syncthing 设备共享给可信电脑，并尽量使用局域网直连或可信的自建 relay。

### 命令行切换流程

旧电脑：

```powershell
oc-out
```

新电脑：

```powershell
oc-in
ocp
```

查看同步状态：

```powershell
oc-status
```

## B 机初始化

1. 把 `tools` 文件夹复制到 B 机。
2. 使用管理员 PowerShell 运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-remote.ps1
```

脚本会尝试完成以下工作：

- 安装缺失的 Git、Node.js、Python 和 Syncthing
- 安装 `opencode-ai`
- 安装 `ttkbootstrap`
- 初始化并启动 Syncthing
- 创建三个同步目录
- 添加 A 机设备
- 创建 B 机桌面快捷方式
- 将工具目录加入 PATH

3. 记下脚本输出的 B 机 Device ID。
4. 回到 A 机执行：

```powershell
oc-pair <B机DeviceID>
```

5. 等待 Syncthing 完成首次同步。
6. B 机执行：

```powershell
oc-in
ocp-gui
```

## Git 项目与 Syncthing 项目

建议：

- 有远端仓库的 Git 项目：在 B 机使用 `git clone`，日常使用 `git pull/push`
- 没有远端仓库、需要随目录同步的项目：放在 `claudeproject` 的 Syncthing 同步范围内
- 不要让同一个 Git 项目同时被 Git 操作和另一台电脑的 Syncthing 写入

当前 `claudeproject/.stignore` 已将有 Git 远端的项目排除，只同步没有远端的 `okx预测市场` 等目录。

## 自检与测试

GUI 主程序支持无界面自检：

```powershell
python tools\ocp-gui.py selftest
```

自检包括：

- 会话数据库读取
- SQLite schema 副本中的级联删除
- Syncthing REST 连接
- 忽略规则读取
- 活跃会话检测
- opencode 可执行文件检测

GUI 冒烟测试：

```powershell
python tools\ocp-gui.py smoke
```

## 常见问题

### 点击删除没有反应

确认当前运行的是最新的 `tools\ocp-gui.py`。中文 Windows 下不能依赖 ttkbootstrap 对话框返回的英文 `Yes` 字符串；当前版本已经改用标准 `tkinter.messagebox` 并返回布尔值。

### 打开 GUI 后不断弹出 CMD

当前版本已经给 WMI 和 `tasklist` 子进程增加 `CREATE_NO_WINDOW`。如果仍然弹出，确认启动的是项目目录下最新的 `ocp-gui.cmd`，而不是旧的 `%USERPROFILE%\bin\ocp-gui.cmd`。

### B 机看不到历史会话

依次检查：

1. Syncthing 三个目录是否已完成同步
2. B 机的 opencode 是否已安装
3. B 机项目路径是否仍为 `D:\PythonProjects\claudeproject`
4. 是否先执行了 `oc-in`
5. 是否启动的是项目 `tools` 目录中的 GUI

### 同步出现冲突

立即关闭两台电脑上的 opencode，保留 Syncthing 生成的冲突文件，然后确认哪一份是最新内容。不要在两台电脑同时继续写入会话数据库。

## 许可证

当前仓库未单独声明许可证。如需公开发布，请根据使用的代码、图标和依赖补充合适的许可证文件。
