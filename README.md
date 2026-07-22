# Hermes Kanban SDD Agent Fleet

这是一个基于 Hermes Agent 0.19.0 的 12-profile 部署包。正式交付采用“一个已合入 PRD 版本、一个 Kanban run、一个共享分支、一个 Draft MR”；详细规则以 [WORKFLOW.md](WORKFLOW.md) 为准。

## 部署形态

- 不构建自定义镜像，两个 Compose service 都直接使用宿主机已有的官方 `nousresearch/hermes-agent:v2026.7.20`。
- profiles、模板、schema、配置、脚本和 Hermes 运行时补丁从本目录只读挂载。
- `tooling-sync` 将锁定版本的 `glab`、`lark-cli` 和官方 Skills 安装到独立 named volume；Hermes 只读使用该工具卷。
- 一个 Hermes 容器、12 个隔离 profile、每项目一个持久化 Kanban SQLite board。
- `dispatcher`、`prd-writer`、`fde` 各有独立飞书 App 和 Gateway；只有 dispatcher 启用 Kanban dispatcher。
- 其他专业 Agent 仅由 Kanban 唤醒，共享明确的 run worktree，不启动 Gateway。
- GitLab HTTPS 动态 clone；12 个互不相同的 token 只存在各 profile 的 `.env`，不进入 Compose 环境或 remote URL。
- 仅 `dispatcher`、`prd-writer`、`fde` 的 `.env` 包含独立飞书 App；lark-cli 加密副本位于各自的持久 `.lark-cli` 目录。
- 正式交付不创建 GitLab Task；FDE 仍可创建普通 Issue。

不要让两个 active 容器共享同一 `HERMES_DATA_DIR`。

## Ubuntu AMD64 首次部署

前置条件：Docker Compose、已经存在于本机的官方 Hermes 镜像、三个飞书机器人 App、允许群组内各 Agent 的最小权限 GitLab token，以及可用模型凭据。首次工具同步还需要容器能访问 GitLab.com、GitHub.com 和 npm registry。

```bash
docker image inspect nousresearch/hermes-agent:v2026.7.20 >/dev/null
cp .env.example .env
# 根 .env 只填写模型配置和 producer commit identity。

./scripts/init-profile-envs.sh
# 编辑 HERMES_DATA_DIR/profiles/<profile>/.env：
# 12 份分别填写 GitLab host、允许群组和不同 token；
# dispatcher、prd-writer、fde 另外填写各自的飞书 App 和消息范围。

./scripts/verify-bundle.sh
docker compose up -d
```

`init-profile-envs.sh` 会读取根 `.env` 中的 `HERMES_DATA_DIR`，仅创建缺失文件并设置 profile 目录 `0700`、`.env` 文件 `0600`；重复执行不会覆盖凭据。Compose 不使用 `env_file`，因此根 `.env` 中即使误留旧 GitLab/飞书变量也不会整体注入容器。

请使用与 `.env` 中 `PUID` 相同 UID 的宿主用户运行初始化脚本，不要使用 `sudo`；否则容器会因 `.env` 所有者不是 Hermes 运行用户而拒绝启动。通常可把 `PUID`/`PGID` 设置为 `id -u`/`id -g` 的结果。

Compose 设置了 `pull_policy: never`，不会静默拉取其他镜像，也没有 `build` 配置。首次 `up` 时 `tooling-sync` 先填充 named volume，成功后 Hermes 才启动。s6 启动时按顺序校验 0.19.0、幂等应用 Kanban handler 补丁、安装 12 个 profiles、验证独立凭据、将三个飞书 profile 绑定到各自的持久加密 lark-cli 存储并启动三个 Gateway。项目 checkout、`gitlab-p<project_id>` board 与 run worktree 由 dispatcher 在首次请求时幂等创建。

检查：

```bash
docker compose ps
docker compose logs tooling-sync
docker compose logs --tail=100 hermes
docker compose exec hermes hermes version
docker compose exec hermes hermes profile list
docker compose exec hermes hermes -p dispatcher gateway status
docker compose exec hermes hermes -p prd-writer gateway status
docker compose exec hermes hermes -p fde gateway status
docker compose exec hermes hermes -p dispatcher kanban boards list
```

## 使用

向 dispatcher 发送且必须同时提供两个 URL：

```text
实现 PRD <精确 PRD blob/raw URL> <已合并 PRD MR URL>
```

缺失或错配时不会创建 run。常用控制命令：

```text
状态 <run_key>
暂停 <run_key>
继续 <run_key>
取消 <run_key>
```

观察：

```bash
docker compose exec hermes hermes -p dispatcher kanban --board gitlab-p<project-id> watch
docker compose exec hermes hermes -p dispatcher kanban --board gitlab-p<project-id> list
```

## 权限与更新

目标默认分支必须 protected。dispatcher 使用可合并该分支的 Maintainer；producer/coder 使用 Developer；reviewer/tester/FDE 使用 Reporter + API。任何权限失败都作为 capability block 处理，不临时共享 Maintainer token。

启动会拒绝空白或重复的 GitLab token、重复的飞书 App ID/Secret、worker 中的飞书变量、错误文件权限和符号链接。验证过程只比较摘要，不打印凭据。轮换时只修改对应的 `${HERMES_DATA_DIR}/profiles/<profile>/.env` 并执行 `docker compose restart hermes`；容器会从该文件重新绑定 lark-cli，加密副本保留在 `/opt/data/profiles/<profile>/.lark-cli/`。由于是单容器，三个 Gateway 会一起重启，但其他 profile 的凭据文件不会被修改。

版本与官方 Skill revision 见 [config/skills-lock.yaml](config/skills-lock.yaml)。更新 CLI 或 Skills 时修改其中的精确版本/revision，然后执行：

```bash
docker compose stop hermes
docker compose run --rm tooling-sync
docker compose up -d hermes
```

工具按锁内容生成不可变 release 目录，并原子切换 `current` 链接；更新不重建镜像。不要在 Hermes 运行期间切换工具版本。

升级 Hermes 时，先把新官方镜像放到宿主机，再同步修改 `.env` 的 `HERMES_IMAGE`、版本锁和运行时补丁。补丁脚本会在版本不匹配时拒绝启动，不能只换 tag。默认不覆盖持久数据中的 `config.yaml`；接受仓库托管配置时将 `FLEET_FORCE_CONFIG=1` 启动一次，确认后恢复为 `0`。备份前停止 active 容器并备份整个 `HERMES_DATA_DIR`。

`docker compose down` 不删除工具卷；`docker compose down -v` 会删除工具卷，下次启动需重新联网同步。

## 验证边界

```bash
./scripts/verify-bundle.sh
```

该命令验证静态部署包。三机器人、真实 GitLab 最小权限、protected branch、MR/pipeline/discussion、checked-head merge、崩溃恢复和故障注入未完成前，不得声明生产自治 E2E。
