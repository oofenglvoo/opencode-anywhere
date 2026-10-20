import type { Plugin } from "@opencode-ai/plugin"
import { appendFileSync, readFileSync } from "node:fs"
import { tmpdir, homedir } from "node:os"
import { join } from "node:path"

const SUMMARY_LIMIT = 80
const QUESTION_DEDUPE_MS = 4000
const ERROR_SUPPRESS_MS = 5000
const SESSION_TITLE_LIMIT = 30
const TITLE_CACHE_MS = 10000
const PROTOCOL = "ocp-session"
const LAUNCHER_FILE = join(homedir(), ".local", "share", "opencode", "ocp-launcher.json")
const PROTOCOL_FILE = join(homedir(), ".local", "share", "opencode", "ocp-protocol.json")
const ACTION_LABEL = "回到会话"

// 点击通知 -> 交给 GUI 的 open-session 子命令: 有窗口就聚焦, 没有就新开.
const TOAST_PS = `$ErrorActionPreference = 'SilentlyContinue'
$title = $env:OC_NOTIFY_TITLE
$text = $env:OC_NOTIFY_TEXT
$launch = $env:OC_NOTIFY_LAUNCH
if (-not $title) { $title = 'OpenCode' }
if ($null -eq $text) { $text = '' }
$shown = $false
try {
  [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
  [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
  $template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
  $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
  $nodes = $xml.GetElementsByTagName('text')
  [void]$nodes.Item(0).AppendChild($xml.CreateTextNode($title))
  [void]$nodes.Item(1).AppendChild($xml.CreateTextNode($text))
  if ($launch) {
    # 用 SetAttribute 而不是拼字符串, 避免会话标题里的 & < > 破坏 XML
    $toastNode = $xml.SelectSingleNode('/toast')
    [void]$toastNode.SetAttribute('activationType', 'protocol')
    [void]$toastNode.SetAttribute('launch', $launch)
    $doc = $xml.CreateElement('actions')
    $action = $xml.CreateElement('action')
    [void]$action.SetAttribute('content', $env:OC_NOTIFY_ACTION)
    [void]$action.SetAttribute('arguments', $launch)
    [void]$action.SetAttribute('activationType', 'protocol')
    [void]$doc.AppendChild($action)
    [void]$toastNode.AppendChild($doc)
  }
  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
  $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('OpenCode')
  $notifier.Show($toast)
  $shown = $true
} catch {
  $shown = $false
}
if (-not $shown) {
  try {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    Add-Type -AssemblyName System.Drawing | Out-Null
    $ni = New-Object System.Windows.Forms.NotifyIcon
    $ni.Icon = [System.Drawing.SystemIcons]::Application
    $ni.BalloonTipIcon = 'Info'
    $ni.BalloonTipTitle = $title
    $ni.BalloonTipText = $text
    $ni.Visible = $true
    $ni.ShowBalloonTip(10000)
    Start-Sleep -Seconds 10
    $ni.Visible = $false
    $ni.Dispose()
  } catch {
  }
}
`

const debugLog = (msg: string) => {
  if (!process.env.OC_NOTIFY_DEBUG) return
  try {
    appendFileSync(join(tmpdir(), "opencode-notify.log"), `[${new Date().toISOString()}] ${msg}\n`)
  } catch {}
}

const encodePowershell = (script: string) => Buffer.from(script, "utf16le").toString("base64")

const readJSON = (path: string): any => {
  try {
    return JSON.parse(readFileSync(path, "utf8"))
  } catch {
    return null
  }
}

// 把 ocp-session: 协议注册到本机 HKCU(由 GUI 的 register-protocol 子命令完成)。
// GUI 每次启动都会刷新 ocp-launcher.json; 这里比对 ocp-protocol.json 里记录的命令,
// 不一致就静默重注册一次, 这样项目移动/换 Python 之后点击通知依然有效。
const ensureProtocol = () => {
  try {
    const launcher = readJSON(LAUNCHER_FILE)
    const argv: string[] = Array.isArray(launcher?.argv) ? launcher.argv : []
    if (!argv.length) return
    const recorded = readJSON(PROTOCOL_FILE)
    const want = [...argv, "open-session", "%1"].map((a) => `"${a}"`).join(" ")
    if (recorded?.command === want) return
    const B = (globalThis as any).Bun
    if (B && typeof B.spawn === "function") {
      const proc = B.spawn([argv[0], ...argv.slice(1), "register-protocol"], {
        stdin: "ignore",
        stdout: "ignore",
        stderr: "ignore",
        windowsHide: true,
      })
      if (proc && proc.exited) proc.exited.catch(() => {})
      debugLog(`registering protocol via bun: ${argv[0]}`)
      return
    }
    import("node:child_process")
      .then(({ spawn }) => {
        const child = spawn(argv[0], [...argv.slice(1), "register-protocol"], {
          stdio: "ignore",
          windowsHide: true,
        })
        child.on("error", (err: any) => debugLog(`register spawn error: ${err?.message ?? err}`))
        child.unref()
        debugLog(`registering protocol via node: ${argv[0]}`)
      })
      .catch((err: any) => debugLog(`register import failed: ${err?.message ?? err}`))
  } catch (err: any) {
    debugLog(`register protocol failed: ${err?.message ?? err}`)
  }
}

const sessionLaunchUri = (sessionID?: string): string =>
  sessionID ? `${PROTOCOL}:${sessionID}` : ""

const spawnNotify = async (title: string, text: string, launch?: string) => {
  const encoded = encodePowershell(TOAST_PS)
  const argv = [
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-EncodedCommand",
    encoded,
  ]
  const env: Record<string, string> = { ...process.env, OC_NOTIFY_TITLE: title, OC_NOTIFY_TEXT: text }
  if (launch) {
    env.OC_NOTIFY_LAUNCH = launch
    env.OC_NOTIFY_ACTION = ACTION_LABEL
  }
  try {
    const B = (globalThis as any).Bun
    if (B && typeof B.spawn === "function") {
      const proc = B.spawn(argv, {
        env,
        stdin: "ignore",
        stdout: "ignore",
        stderr: "ignore",
        // 控制台子系统的 powershell.exe 在没有可继承控制台时会拿到一个可见的
        // 空白窗口；windowsHide 才真正抑制它（stdio 重定向不够）。
        windowsHide: true,
      })
      if (proc && proc.exited) proc.exited.catch(() => {})
      debugLog(`spawned via bun pid=${proc?.pid ?? "?"} title=${title}`)
      return
    }
    const { spawn } = await import("node:child_process")
    const child = spawn(argv[0], argv.slice(1), {
      env: env as any,
      stdio: "ignore",
      windowsHide: true,
    })
    child.on("error", (err: any) => debugLog(`node spawn error: ${err?.message ?? err}`))
    child.unref()
    debugLog(`spawned via node pid=${child.pid ?? "?"} title=${title}`)
  } catch (err: any) {
    debugLog(`spawn failed: ${err?.message ?? err}`)
  }
}

const summarize = (raw: string, limit = SUMMARY_LIMIT): string => {
  let text = String(raw ?? "")
  text = text.replace(/```[\s\S]*?```/g, " ")
  text = text.replace(/`([^`\n]*)`/g, "$1")
  text = text.replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
  text = text.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
  text = text.replace(/^\s{0,3}#{1,6}\s+/gm, "")
  text = text.replace(/^\s{0,3}>\s?/gm, "")
  text = text.replace(/^\s*[-*+]\s+/gm, "")
  text = text.replace(/^\s*\d+\.\s+/gm, "")
  text = text.replace(/(\*\*|__)(.+?)\1/g, "$2")
  text = text.replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, "$1$2")
  text = text.replace(/~~(.+?)~~/g, "$1")
  text = text.replace(/\s+/g, " ").trim()
  if (text.length <= limit) return text
  return `${text.slice(0, Math.max(1, limit - 1)).trimEnd()}…`
}

const plugin: Plugin = async (input) => {
  const { client } = input as any

  // 首次加载时确保 ocp-session: 协议指向本机 GUI(幂等, 失败不影响通知)
  ensureProtocol()

  const lastText = new Map<string, string>()
  const recentErrorAt = new Map<string, number>()
  const questionAt = new Map<string, number>()
  const titleCache = new Map<string, { value: string; at: number }>()

  const cleanLabel = (raw: unknown) => {
    const t = String(raw ?? "").replace(/\s+/g, " ").trim()
    if (!t) return ""
    if (t.length <= SESSION_TITLE_LIMIT) return t
    return `${t.slice(0, SESSION_TITLE_LIMIT - 1).trimEnd()}…`
  }

  const setTitleCache = (sessionID: string, value: string) => {
    titleCache.set(sessionID, { value, at: Date.now() })
    if (titleCache.size > 300) {
      const cutoff = Date.now() - 60000
      for (const [k, v] of titleCache) if (v.at < cutoff) titleCache.delete(k)
    }
  }

  const resolveSessionTitle = async (sessionID: string): Promise<string> => {
    const hit = titleCache.get(sessionID)
    if (hit && Date.now() - hit.at < TITLE_CACHE_MS) return hit.value
    let value = ""
    try {
      const res: any = await client.session.get({ path: { id: sessionID } })
      const sess = res?.data ?? res
      value = cleanLabel(sess?.title)
    } catch (err: any) {
      debugLog(`session title failed: ${err?.message ?? err}`)
    }
    setTitleCache(sessionID, value)
    return value
  }

  const buildTitle = async (sessionID?: string) => {
    if (!sessionID) return "OpenCode"
    const label = await resolveSessionTitle(sessionID)
    return label ? `OpenCode · ${label}` : "OpenCode"
  }

  const notify = async (status: string, body: string, sessionID?: string) => {
    try {
      const clean = summarize(body)
      const text = clean ? `${status}：${clean}` : status
      const title = await buildTitle(sessionID)
      const launch = sessionLaunchUri(sessionID)
      debugLog(`notify [${title}] ${text} launch=${launch || "-"}`)
      void spawnNotify(title, text, launch)
    } catch (err: any) {
      debugLog(`notify failed: ${err?.message ?? err}`)
    }
  }

  const claimQuestion = (sessionID: string) => {
    const now = Date.now()
    const prev = questionAt.get(sessionID)
    if (prev !== undefined && now - prev < QUESTION_DEDUPE_MS) return false
    questionAt.set(sessionID, now)
    if (questionAt.size > 500) {
      const cutoff = now - 60000
      for (const [k, v] of questionAt) if (v < cutoff) questionAt.delete(k)
    }
    return true
  }

  const firstQuestionBody = (questions: any[]) => {
    const q = Array.isArray(questions) ? questions[0] : undefined
    if (!q) return ""
    const question = String(q.question ?? "").trim()
    const header = String(q.header ?? "").trim()
    if (!question && !header) return ""
    return header && question ? `${header}：${question}` : header || question
  }

  const extractQuestions = (props: any): any[] => {
    if (Array.isArray(props?.questions)) return props.questions
    if (Array.isArray(props?.data?.questions)) return props.data.questions
    return []
  }

  const fetchLastAssistantText = async (sessionID: string): Promise<string> => {
    const res: any = await client.session.messages({ path: { id: sessionID } })
    const list: any[] = Array.isArray(res) ? res : (res?.data ?? [])
    let best: { created: number; text: string } | null = null
    for (const item of list) {
      const info = item?.info
      if (!info || info.role !== "assistant" || info.error) continue
      const created = Number(info?.time?.created ?? 0)
      if (best && created < best.created) continue
      const parts: any[] = Array.isArray(item?.parts) ? item.parts : []
      const text = parts
        .filter(
          (p: any) =>
            p?.type === "text" && !p?.synthetic && !p?.ignored && typeof p?.text === "string" && p.text.trim(),
        )
        .map((p: any) => p.text)
        .join("\n")
      if (text.trim()) best = { created, text }
    }
    return best?.text ?? ""
  }

  return {
    event: async ({ event }: { event: any }) => {
      try {
        const type = String(event?.type ?? "")
        const props = event?.properties ?? {}
        const sessionID = props?.sessionID ? String(props.sessionID) : ""

        if (type === "session.error") {
          const err = props?.error
          if (!err) return
          const errName = String(err?.name ?? "")
          if (errName === "MessageAbortedError") return
          if (sessionID) {
            recentErrorAt.set(sessionID, Date.now())
            if (recentErrorAt.size > 300) {
              const cutoff = Date.now() - 60000
              for (const [k, v] of recentErrorAt) if (v < cutoff) recentErrorAt.delete(k)
            }
          }
          const msg = err?.data?.message ?? err?.message ?? errName ?? "未知错误"
          await notify("发生错误", String(msg), sessionID || undefined)
          return
        }

        if (type === "question.asked" || type === "question.v2.asked") {
          if (!sessionID) return
          if (!claimQuestion(sessionID)) return
          const body = firstQuestionBody(extractQuestions(props)) || "请在 OpenCode 窗口中作答"
          await notify("弹出选择", body, sessionID)
          return
        }

        if (type === "session.idle") {
          if (!sessionID) return
          const errAt = recentErrorAt.get(sessionID)
          if (errAt !== undefined) {
            recentErrorAt.delete(sessionID)
            if (Date.now() - errAt < ERROR_SUPPRESS_MS) return
          }
          try {
            const sres: any = await client.session.get({ path: { id: sessionID } })
            const sess = sres?.data ?? sres
            if (sess?.parentID) return
            setTitleCache(sessionID, cleanLabel(sess?.title))
          } catch {}
          let text = lastText.get(sessionID) ?? ""
          if (!text.trim()) {
            try {
              text = await fetchLastAssistantText(sessionID)
            } catch (err: any) {
              debugLog(`fetch messages failed: ${err?.message ?? err}`)
            }
          }
          await notify("回答已完成", text.trim() || "请回到 OpenCode 窗口查看结果", sessionID)
          if (lastText.size > 300) {
            const oldest = lastText.keys().next().value
            if (oldest !== undefined) lastText.delete(oldest)
          }
          return
        }
      } catch (err: any) {
        debugLog(`event handler error: ${err?.message ?? err}`)
      }
    },

    "tool.execute.before": async (toolInput: any, toolOutput: any) => {
      try {
        if (String(toolInput?.tool ?? "") !== "question") return
        const sessionID = toolInput?.sessionID ? String(toolInput.sessionID) : ""
        if (!sessionID) return
        if (!claimQuestion(sessionID)) return
        const args = toolOutput?.args ?? {}
        const questions = Array.isArray(args.questions) ? args.questions : (args?.data?.questions ?? [])
        const body = firstQuestionBody(questions) || "请在 OpenCode 窗口中作答"
        await notify("弹出选择", body, sessionID)
      } catch (err: any) {
        debugLog(`tool hook error: ${err?.message ?? err}`)
      }
    },

    "experimental.text.complete": async (textInput: any, textOutput: any) => {
      try {
        const sessionID = textInput?.sessionID ? String(textInput.sessionID) : ""
        const text = String(textOutput?.text ?? "").trim()
        if (!sessionID || !text) return
        lastText.set(sessionID, text)
      } catch {}
    },
  }
}

export default plugin
