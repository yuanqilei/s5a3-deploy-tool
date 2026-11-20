# Git 源码首次部署

`runtime/docker/compose.json`、`server.env`、`database.env` 是私有生成文件，不属于 Git 源码。旧工具把它们和旧版 `Dockerfile.deploy` 当成仓库必需文件，导致首次从 Git 拉取后直接失败。

从 `2026.11.20.4` 起，首次“一键部署”按以下次序执行：获取源码、恢复补丁、准备 PVF、检查/生成运行配置、构建并启动。没有旧版运行目录 Dockerfile 时使用仓库中的 `deploy/docker/Dockerfile`。普通源码包可以不带运行配置。

新安装需要填写“部署 PVF”。工具调用仓库自带 `deploy/docker/manage.py` 构建资源提取器、校验统一频道表并生成配置，再合入官方推荐配置、重新生成环境文件；镜像名和 Compose 项目统一为工具使用的 `servers5a3:launcher`、`servers5a3`。不绕过生成器自身的频道校验、资源校验、网卡私网地址检查或兼容条件。

已有配置完整且无中断标记时直接复用，不生成新密码。若发现旧容器、数据库卷或服务端运行数据，则拒绝把它当作新安装；请恢复现有配置备份。更新服务端模式不会初始化丢失的运行配置。本机源码包覆盖现有安装时保留整个 `runtime/docker`。

生成中会写入 `.s5a3-prepare-incomplete` 标记，全部步骤成功后才移除。`2026.11.20.5` 修复旧标记导致不能重试的问题：使用“一键部署”重新检查容器、数据库卷和运行数据；尚无配置时允许安全重试，已有中断 `instance.json` 时先校验原身份和 database.env 口令一致性，备份到受限的 `runtime/docker/prepare-backups/` 后复用原身份继续。缺少实例身份却已有环境文件、口令不一致或有旧业务容器/数据时仍然停止。成功前不能启动服务，也不需要手动删除标记绕过保护。

新标记记录 `build_image`、`prepare`、`recommended`、`final_prepare` 阶段和实际退出码；阶段原始输出实时显示并保留于 `runtime/docker/.prepare-state/last.log`（目录权限 0700）。旧版空标记无法还原第一次的原始错误，但安全重试可重新记录真正失败的阶段。

验证：`python tests/test_runtime_prepare.py -v`。10 项隔离测试通过，未连接业务服务器或执行实际 Docker 部署；还需在目标服务器使用真实 PVF、网络与 Docker 环境验收。
