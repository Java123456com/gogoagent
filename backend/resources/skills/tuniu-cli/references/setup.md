# 途牛 CLI 环境准备与版本维护

> 本文档为 tuniu-cli 技能的**环境/安装/版本维护**参考，仅在首次使用或遇到 CLI 不可用/版本过低时按需加载。

## 运行环境要求

**运行环境必须安装 Node.js 18+ 与 tuniu-cli**，否则无法调用服务。

### 项目级安装（唯一入口）

在 **gogo-agent 项目根目录**执行以下命令，将 CLI 安装到本项目的 `node_modules`，不要安装到全局：

```powershell
npm install
```

项目级 `package.json` 已声明 `tuniu-cli`。Python 执行器会自动将 `node_modules/.bin` 加入 Skill 子进程 PATH。

安装后可在项目根目录验证：

```powershell
Get-ChildItem node_modules\.bin\tuniu*
tuniu --version
```

### 按脚本结果处置

若命令仍不可用，先确认当前命令行位于项目根目录并重新执行 `npm install`：

| 退出码 | 输出 | 处置 |
| --- | --- | --- |
| 情况 | 处置 |
| --- | --- |
| Node.js 低于 18 | 安装/升级 Node.js 18+ 后重试 |
| npm 安装失败 | 检查网络和项目目录权限，不要改成全局安装 |
| `tuniu` 不在当前终端 PATH | 确认项目级 `node_modules/.bin/tuniu.cmd` 存在；Python Agent 会自动注入该目录 |

### 调用形式约束（重要）

- 必须以**裸命令** `tuniu` 开头，例如 `tuniu call <server> <tool> -a '<JSON>'`。
- **禁止**使用绝对路径（如 `/Users/xxx/.npm-global/bin/tuniu`）——不在命令白名单内，会被安全校验拒绝。
- **禁止**使用 `npx tuniu-cli ...` —— 会绕过 Agent 的 API Key 注入，导致 `108 ApiKeyRequiredError`。
- 一条命令中不要出现 `&&`、`||`、`|`、`;` 等分隔符，需分多次执行。

> 脚本内 `MIN_CLI_VERSION` 需与 SKILL.md 头部的 `minCliVersion` 保持一致，升级要求时两处同步修改。

## Skill 版本与更新说明

`tuniu-cli` 提供 **skill** 子命令，用于维护本助手在各 AI Agent 目录下的安装与版本查看，与业务调用（`tuniu call`）相互独立。

### CLI 与 Skill 兼容性

本 skill 依赖 `tuniu-cli` 版本不低于 SKILL.md 头部声明的 `minCliVersion`。使用时必须遵循：

1. 若 `tuniu --version` 低于 `minCliVersion`，执行 `bash scripts/setup.sh` 自动更新 CLI（脚本内部会跑 `npm install -g tuniu-cli@latest`）。
2. 脚本返回 `RESULT=OK` 后再执行 `tuniu skill install` 更新本地 skill。
3. 不要执行 `npm install -g`，避免把本项目依赖安装到用户或系统全局。
4. 若更新失败，明确告知用户当前 CLI 版本与 Skill 不兼容，部分操作可能失效。

**使用场景简述**

- **`tuniu skill version`**：在已配置多台 Agent（如 Cursor、Claude 等）时，检查各目录下已安装的 skill 版本、来源与安装时间。
- **`tuniu skill install`**：需要**安装或更新**本 skill 时使用。默认仅写入 `~/.agents/skills/tuniu-cli/`；通过 `--agent` 可指定单个、多个（逗号分隔）或 `all`；`--dir` 可额外指定自定义 skills 根目录。
- **`npm install` / `npm ci`**：安装项目依赖；Python 版直接读取仓库内 `backend/resources/skills/tuniu-cli`，不需要执行 `tuniu skill install`。

更完整的参数与示例见：`tuniu skill install --help`。
