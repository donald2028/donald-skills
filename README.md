# Donald Skills

Donald 常用 Agent Skills 的统一仓库，面向 Claude Code、Codex 和兼容 Agent Skills 规范的运行时。

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

## 架构

```text
donald-skills/
├── skills/                         # 唯一的 skill 源码目录
├── .claude/skills/                 # 生成的 Claude Code 项目级镜像
├── .agents/skills/                 # 生成的 Codex 项目级镜像
├── .claude-plugin/
│   ├── marketplace.json            # Claude marketplace
│   └── plugin.json                 # Claude plugin manifest
├── .codex-plugin/plugin.json       # Codex plugin manifest
├── .agents/plugins/marketplace.json# Codex marketplace
├── AGENTS.md                       # 跨运行时仓库协作约定
└── CLAUDE.md -> AGENTS.md          # Claude Code 共享同一份约定
```

`skills/` 是唯一需要手工维护的 skill 源。`.claude/skills/` 和 `.agents/skills/` 由同步脚本生成，避免维护多份副本。plugin 安装则直接从根目录的 `skills/` 自动发现内容。

## 添加 skill

1. 创建 `skills/<skill-name>/SKILL.md`，目录名和 frontmatter `name` 使用相同的 kebab-case 名称。
2. 把该 skill 需要的脚本、参考资料和资源放在自己的目录内，保持可独立分发。
3. 同步运行时镜像并执行检查：

```bash
python3 skills/sync_runtime_skills.py
python3 skills/sync_runtime_skills.py --check
npx skills add . --list
claude plugin validate . --strict
```

发布新版本时，同时更新 `.claude-plugin/plugin.json`、`.codex-plugin/plugin.json` 和 `.claude-plugin/marketplace.json` 中的版本。

## License

[MIT](LICENSE)
