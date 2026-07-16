# Donald Skills

Donald 常用 Agent Skills 的统一仓库，面向 Claude Code、Codex、Cursor、Gemini CLI、Kimi Code、OpenCode 和兼容 Agent Skills 规范的运行时。

当前版本包含仓库维护、安全 Git 提交、GitHub 仓库调研、按工具独立绑定且按 Profile 共享登录状态的浏览器配置，以及微信公众号采集、X 内容采集和 ChatGPT Web 外部出图等通用 skill。下面的安装入口可以直接发现并安装 `skills/<name>/SKILL.md` 中的内容。

当前工具 skills：

- `donald-safe-commit`：审查并安全提交 Git 变更，仅在用户要求时推送。
- `donald-research-github`：把 GitHub 仓库获取到可配置的调研目录并按需分析。
- `donald-config-browser`：为每个工具单独选择 Chrome Profile；相同 Profile 复用同一份 CDP User Data 和 Cookie。
- `donald-collect-wechat`：采集公众号文章列表和公开正文。
- `donald-collect-x`：采集 X 账号帖子、thread、Article 和媒体。
- `donald-chatgpt-imagegen`：通过可恢复的 ChatGPT Web 浏览器任务外部出图。

采集和出图结果默认保存在系统“文档”目录的 `Donald Skills/Data/` 下，不写入源码仓库或
当前工作目录。可以用全局环境变量 `DONALD_SKILLS_OUTPUT_ROOT` 改写共享 Data 根目录，
也可以在单次命令中传 `--output-root`；单次命令优先。Chrome Profile 绑定配置和 Cookie
运行数据分别保存在系统原生应用配置、应用数据目录，不与用户内容混放。

GitHub 调研优先使用当前请求指定的目录或 `DONALD_GITHUB_RESEARCH_ROOT`。如果两者都未
提供，skill 会在获取仓库前提醒用户配置全局调研根目录；用户跳过时，本次使用系统
“文档”目录下的 `Donald Skills/Data/github-research/<owner>/<repo>/`。

## 安装

### Skills CLI

全局安装仓库中的 skills：

```bash
npx skills add donald2028/donald-skills -g
```

先查看可安装列表：

```bash
npx skills add donald2028/donald-skills --list
```

### Claude Code plugin

```bash
claude plugin marketplace add donald2028/donald-skills
claude plugin install donald-skills@donald-skills
```

### Codex plugin

```bash
codex plugin marketplace add donald2028/donald-skills
codex plugin add donald-skills@donald-skills
```

### Gemini CLI extension

```bash
gemini extensions install https://github.com/donald2028/donald-skills
```

### Cursor and Kimi Code

仓库分别提供 `.cursor-plugin/plugin.json` 和 `.kimi-plugin/plugin.json`，通过对应运行时的
插件市场或本地插件安装入口安装即可；两个 manifest 都直接指向根目录的 `skills/`。

### OpenCode

OpenCode 会原生发现项目级 `.agents/skills/<name>/SKILL.md` 和
`.claude/skills/<name>/SKILL.md`。本仓库的同步脚本会维护这两份镜像，因此在仓库目录内
直接启动 OpenCode 即可使用；也可以用 Skills CLI 安装到用户级目录：

```bash
npx skills add donald2028/donald-skills -g
```

不需要额外的 OpenCode 插件或 `opencode.json` 配置。

## 构建与版本同步

`package.json` 是插件名称、版本、描述、作者、仓库地址和关键词的唯一维护入口。普通
build 会把这些共享字段同步到所有 channel manifest 和 marketplace，并刷新 Claude/Codex
runtime mirrors：

```bash
npm run build
```

发布新版本时只写一次版本号：

```bash
npm run build -- --version 0.2.0
```

CI 使用只读模式检查是否有人直接修改 manifest，或忘记在 skill 变更后重新 build：

```bash
npm run build:check
```

各平台独有字段（例如 Codex/Kimi 的 `interface`）仍保留在对应 manifest 中，build 只覆盖
由 `package.json` 管理的共享字段。

## 架构

```text
donald-skills/
├── package.json                    # 共享插件元数据、版本和 build 命令
├── scripts/build.py                # 生成渠道 manifest 和 runtime mirrors
├── skills/                         # 唯一的 skill 源码目录
├── .claude/skills/                 # 生成的 Claude Code 项目级镜像
├── .agents/skills/                 # 生成的 Codex 项目级镜像
├── .claude-plugin/
│   ├── marketplace.json            # Claude marketplace
│   └── plugin.json                 # Claude plugin manifest
├── .codex-plugin/plugin.json       # Codex plugin manifest
├── .cursor-plugin/plugin.json      # Cursor plugin manifest
├── .kimi-plugin/plugin.json        # Kimi Code plugin manifest
├── .agents/plugins/marketplace.json# Codex marketplace
├── gemini-extension.json           # Gemini CLI extension manifest
├── GEMINI.md                       # Gemini CLI extension context
├── AGENTS.md                       # 跨运行时仓库协作约定
└── CLAUDE.md -> AGENTS.md          # Claude Code 共享同一份约定
```

`skills/` 是唯一需要手工维护的 skill 源。`.claude/skills/` 和 `.agents/skills/` 由同步脚本生成，避免维护多份副本；OpenCode 会直接利用这两份官方支持的兼容目录。Claude、Codex、Cursor、Kimi Code 和 Gemini CLI 的 plugin/extension manifest 也都指向根目录的 `skills/`，避免运行时之间出现内容漂移。

浏览器业务 skill 通过 `REQUIRED SUB-SKILL` 调用 `donald-config-browser`，由 Agent 先完成环境、
Profile 绑定和 CDP 验证，再进入各自 runner。这个依赖写在 workflow 指令中，不依赖某个 Agent
专用的工具名；Claude Code、Codex 和其他兼容 Agent Skills 的运行时都加载同一份正文。脚本、
references 和 assets 仍保留在各自 skill 内，不通过兄弟目录路径互相 import。
默认的整库和 aggregate plugin 安装会同时提供依赖；若某个运行时允许选择性安装单个 skill，
安装浏览器业务 skill 时必须同时选择 `donald-config-browser`。

## 添加 skill

1. 创建 `skills/<skill-name>/SKILL.md`，目录名和 frontmatter `name` 使用相同的 kebab-case 名称。
2. 把该 skill 需要的脚本、参考资料和资源放在自己的目录内；如需复用另一个 workflow，使用
   `**REQUIRED SUB-SKILL:** Invoke <skill-name>` 声明，并确保 aggregate plugin 同时分发两者。
3. 重新 build 并执行检查：

```bash
npm run build
npm run build:check
npx skills add . --list
claude plugin validate . --strict
gemini extensions validate .
```

发布新版本时运行 `npm run build -- --version <semver>`，不要逐个修改渠道 JSON。

## License

[MIT](LICENSE)
