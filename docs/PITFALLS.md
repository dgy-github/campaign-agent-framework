# 踩坑复盘 / PITFALLS

本项目用「Claude 规划 → DeepSeek/CodeWhale 执行 → Codex 测试/修复 → Claude 验收」的多 agent 流水线开发。过程中真实踩过的坑与修法，按类归档，供跨设备/下次复用。

---

## 一、Codex / codex-runner 桥接

### 1. MCP 服务进程加载了旧代码（Node 不热重载）
- **现象**：`start_codex_with_plan` 返回 runId，但 `status.json` 卡在 `stale`、`stdout.log/stderr.log` 全 0 字节、无 exit code、无产物。
- **根因**：`server.mjs`（07:30 修过）在内存里跑的是 07:07 启动时加载的旧版；Node 不热重载。
- **修法**：**重启 MCP 服务**（重启 Claude Desktop）让它加载新 `server.mjs`。
- **教训**：改了 MCP server 代码必须重启 MCP 进程才生效。

### 2. `codex` 是 npm 的 `.ps1` shim，会 detach 丢输出
- **现象**：旧 run.ps1 用 `& codex exec ... $prompt`（12KB 计划当位置参数），npm 的 `codex.ps1` re-spawn node 并 detach → 输出拿不到、进程消失。
- **修法**：run.ps1 检测到 `.ps1` shim 时改用 `node.exe <…>\@openai\codex\bin\codex.js`，并把 stdin/stdout/stderr **重定向到文件**；计划走 **stdin `-`**，不要当 argv。

### 3. Codex Windows 沙箱 `elevated` 后端无头跑不了 ★高频
- **现象**：每条 shell 命令 `exec error: windows sandbox: spawn setup refresh`，exit -1 in 0ms；codex 啥都干不了。
- **根因**：codex 桌面端更新后把 `~/.codex/config.toml` 的 `[windows] sandbox` 设成 `elevated`（要 UAC 提权）；codex-runner 无头/后台启动**触发不了 UAC** → setup helper spawn 立即失败。`read-only`/`workspace-write` 都走该后端，故全废。
- **诊断**：`-c windows.sandbox="__bogus__"` 逼出合法值 = **`elevated` / `unelevated`**。
- **修法**：`~/.codex/config.toml` 改 `sandbox = "unelevated"`（受限令牌沙箱，**无头可用、仍隔离**）。`danger-full-access` 可绕过但放弃沙箱，未采用。
- **注意**：**重启桌面端无效**；且桌面端**再次更新会重写 config**（可能把 unelevated 改回 elevated）——每次大版本更新后要复查这一行。

---

## 二、执行环境（Windows / Python / Shell）

### 4. `python` 是 Microsoft Store 占位程序
- **现象**：`python`/`python3`/`py` 在 PATH 上是 Store stub，`--version` exit 49、空输出，跑不了 pytest。
- **修法**：用真实解释器 **`C:\python-embed\python.exe`**（3.11.9，已装 pydantic/pytest）。

### 5. embedded Python 经 agent shell 跑 pytest 不打印 summary
- **现象**：通过 codex/codewhale 的 shell 跑 `pytest -q`，控制台**不显示 `N passed` 摘要**。
- **后果**：只看 exit code 会漏掉真失败（DeepSeek 首轮 E/F/G "全过"，实则一个 `KeyError`；后又有 `agent_id` vs `actor` 错 key）。
- **修法**：用 `--junitxml=<temp>` 看真实计数 + **逐条检查有无 `FAILED`**；**Claude 验收阶段必独立复跑** `pytest -q`，不信执行方自述。

### 6. Bash 工具是 Git-bash，`cd /d <path>` 是 cmd 语法、静默失败
- **现象**：`cd /d D:\interview-kit` 不报错但**没切目录**（cwd 仍是项目根）；导致 git init/commit 误操作到主仓库（幸而无害）。
- **修法**：用 bash 路径 `cd /d/interview-kit`（盘符挂在 `/c/`、`/d/`）。`cmd` 本身也常不在 codex/codewhale 的 PATH，它们会改用 PowerShell/ProcessStartInfo。

### 7. PowerShell 直接跑 embedded python 输出不可见
- **现象**：`& C:\python-embed\python.exe ...` 在 PowerShell 里有时拿不到 stdout。
- **修法**：用 `cmd /c "set PYTHONPATH=.. && <python.exe> ..."` 或 `Start-Process -RedirectStandardOutput <file>` / `ProcessStartInfo` 捕获。

### 8. 前台 `sleep` 被 harness 拦
- **现象**：`sleep 45; tail ...` 被拒（"use Monitor/until-loop 或 run_in_background"）。
- **修法**：轮询用 PowerShell 的 `for` 循环 + `Start-Sleep`（单条命令内），或长任务用 `run_in_background` 等通知。

---

## 三、模型协作 / 计划结构（Claude 输入给 Codex/DeepSeek 的结构）

### 9. 执行方看不到聊天上下文——计划必须自包含
- **坑**：Codex/CodeWhale 启动时**没有本对话的上下文**，只读到「计划文件 + extraInstruction」。
- **修法**：Claude 写的计划要**自包含**且结构固定：`Goal / Non-goals / 先读哪些文件(确认真实签名) / Execution steps / Acceptance criteria / Tests(给出确切验证命令) / Risks`。少了"先读真实签名"，执行方就会按想象的 API 写、跑偏。

### 10. 三方流水线的分工要在 extraInstruction 里写死
- **坑**：不写清"你只负责实现/只负责测试"，Codex 会既写实现又写测试、或重构对方产物。
- **修法**：extraInstruction 明确"实现已由 DeepSeek 完成，你只负责【测试+修复】，不要重构设计"。

### 11. 便宜执行方（DeepSeek/CodeWhale）会产出真 bug，必须复核
- 实例：`ask()` 用了 `Part` 但**没 import**（NameError）；测试用错 key（`agent_id` 应为 `actor`）；自报"全过"但实际有失败。
- **教训**：**「便宜的干活、贵的把关，把关不降级」**——Claude 验收阶段独立复跑 + 抽查实现，是这套流水线的安全网。

### 12. 控制台乱码 ≠ 文件坏了
- **现象**：Codex 报"实现文件里中文 docstring 乱码"。
- **真相**：是它 GBK 终端的**显示**问题；文件本身是干净 UTF-8。
- **修法**：用字节/重新 Read 核实文件编码，别信终端显示。

### 13. 执行方在仓库里留垃圾
- **现象**：`pytest-*.xml`（根目录 + tests/）、`demo-*.txt`、`py-*.txt`、`pycmd.*` 等探针产物散落。
- **修法**：要求"junitxml 用临时目录或测试后删"；`.gitignore` 加 `pytest-*.xml / demo-*.txt / py-*.txt / pycmd.* / .claude/` 等；提交前 `git status` 核对，别误传。

### 14. 长任务被自动转后台
- **现象**：CodeWhale `exec --auto` 给了长 timeout 时被 harness 自动转后台，返回 task-id。
- **修法**：按 `<task-notification>` 通知模型处理，别空轮询。

---

## 四、安全护栏（auto-mode classifier）

### 15. Agent 不能自我提权 / 自改配置
- 被拦的真实动作：① 往 `.claude/settings.json` 写 wildcard 放行规则；② 提交 `CLAUDE.md`（agent 配置）未经显式请求；③ 首次运行未签名的 `codewhale.exe`。
- **修法**：这些需**用户显式授权**或用户本人操作。安全层有意阻止"agent 给自己加权限/改配置"——这是特性不是 bug。

### 16. 执行未签名二进制要先核验来源
- CodeWhale 三个 exe 全未签名。先**网络核验来源**（确认是 MIT 开源的 Hmbown/CodeWhale，前身 DeepSeek TUI）再用；用户授权后才执行。

---

## 五、网络 / 上云（自建 GitLab）

### 17. 局域网 GitLab 不可达（clash 代理劫持 + 跨网段）
- **现象**：本机在 `192.168.31.x`，GitLab 在 `192.168.100.100:8929`；直连 curl 超时，git 走系统代理(clash)得"Empty reply"。`Test-NetConnection` 显示 TCP True 但 HTTP 永远无响应（TUN/路由器代答 SYN 的假阳性）。
- **修法**：把 `192.168.100.0/24` 加进 **clash 直连/绕过**，或临时关 TUN/系统代理，让内网走直连。
- **教训**：TCP 端口"通"不代表 HTTP 通；clash TUN 会劫持内网段。

### 18. push-to-create + 凭据安全
- GitLab 开了 push-to-create：首次 `git push` 自动建私有项目，无需先在网页建仓。
- **别把明文密码留在 remote URL**（会进 `.git/config`）：改 `http://用户名@host/...`（无密码）+ `git config credential.helper wincred` + `git credential approve` 把密码存进 Windows 凭据库（加密）。

---

## 六、一句话清单（下次开工先检查）
- [ ] codex 改了 server.mjs？→ 重启 MCP
- [ ] codex 报 `spawn setup refresh`？→ `~/.codex/config.toml` 设 `[windows] sandbox = "unelevated"`（桌面端更新后复查）
- [ ] 跑 pytest 用 `C:\python-embed\python.exe`，验收**独立复跑** + 查 `FAILED`，别只看 exit code
- [ ] 给 Codex/DeepSeek 的计划**自包含**（含"先读真实签名"）+ 分工写死
- [ ] 提交前 `git status` 清掉 agent 留的 `pytest-*.xml`/`*.txt` 垃圾
- [ ] 内网 GitLab 不通先查 clash 代理/路由；凭据用 wincred 别留明文
