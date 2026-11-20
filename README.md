# S5A3 Deploy Tool

S5A3 服务端部署工具：Python 源码、打包配置和输入素材，产出独立部署工具 EXE。

## 归属

- 原始资料：`04_部署工具-S5A3服务端管理-v2026.11.19.1`

## 构建

```powershell
cd build
python -m PyInstaller --noconfirm --clean "S5A3服务端管理.spec"   # 产出 build/dist/S5A3服务端管理.exe
```

`build/` 是**源码目录**（spec、版本资源、图标、背景、同步脚本），必须在 Git 里；
`build/build/`、`build/dist/`、`build/out/` 才是产物，已被忽略。

## 迁入范围

- `src` 源码
- 构建 / 打包配置及输入素材
- 必要测试与文档

## 下载

`2026.11.20.3` 增加签名自动更新：启动后台检查、用户确认安装、任务期间禁止替换、新版启动失败回滚、保留本机配置。首次需手动换装支持更新的新 EXE；发布与签名方法见 [自动更新说明](docs/AUTO_UPDATE.md)。

最新 exe 见 [v2026.11.20.3](https://github.com/yuanqilei/s5a3-deploy-tool/releases/tag/v2026.11.20.3)：`S5A3-ServerManager-2026.11.20.3.exe`（36,296,897 B / sha256 `b6ef0463…`，附签名更新清单和 `SHA256SUMS.txt`）。

> Windows 版中文文件名在 Release 上传时会被清洗为 ASCII，故资产名用英文；下载后可自行改名。

## 发布责任

独立部署工具 EXE。大文件（EXE、素材二进制）放 Release 附件或对象存储，不提交 Git 历史；每个 Release 附 `SHA256SUMS`。

## 禁止提交

口令、Token、私钥、证书私钥、真实 `.env`、数据库连接串、玩家数据、线上配置、本机 DPAPI 凭据。配置示例一律用占位值。

## 配套

版本 `2026.11.20.2` 修复“部署 GM”在已有物品索引时跳过管理表初始化的问题。每次部署按“同步源码 → 构建镜像 → 按需建立索引 → 初始化管理表 → 启动服务 → 探测”执行；建表失败会停止后续启动。旧 EXE 需要重新打包或更新，拉取源码不会自动更新已安装的工具。

- 服务端：`yuanqilei/s5a3-server`
- 客户端：`yuanqilei/s5a3-client`
- 登录器 / GM：`yuanqilei/purplemagic-nexus`

## 纯净版发布（给他人使用）

对外只发 **exe + 使用说明 + 版本信息 + SHA256SUMS**，不含任何源码：

```powershell
cd build
pwsh -File .\同步到管理工具.ps1            # 打包 → 清空目标目录 → 只写 4 个文件 → 自检 → 校验哈希
pwsh -File .\同步到管理工具.ps1 -SkipBuild  # 只发布已有产物
```

- 目标目录默认 `C:\DNF\管理工具\S5A3服务端管理工具`，每次发布**整体覆盖**；
- `publish_clean.py` 会自检：目录内出现 `.py/.spec/.ps1/.ico/.jpg` 等一律报错退出；
- 版本号取自 `version_info.txt` 的 `filevers`，发布前务必先改它。
