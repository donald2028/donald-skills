# Donald Skills

Donald 常用 Agent Skills 的统一仓库，面向 Claude Code、Codex 和兼容 Agent Skills 规范的运行时。

当前版本包含仓库架构和一个用于维护该集合的基础设施 skill，不包含生产业务 skill。下面的安装入口已经可以直接使用；日后新增的 `skills/<name>/SKILL.md` 会沿用同一套分发结构。

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
└── CLAUDE.md                       # Claude Code 入口
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
