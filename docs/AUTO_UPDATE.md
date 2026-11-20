# 部署工具自动更新

从 `2026.11.20.3` 起，打包后的 EXE 启动时后台检查签名新版，底部也有“检查更新”。用户确认后下载并验证，再退出安装。部署、备份或其他任务执行期间禁止安装更新；任务中收到的新版本检查结果会等待任务结束。

更新源是本仓 GitHub Releases 的 `update.json`，不需要在客户端保存 GitHub Token。Ed25519 签名公钥固定在 `src/update_trust.py`；文件大小、SHA256、产品标记、版本和固定仓库下载地址都要通过验证。同版本及旧版不安装。首次启用必须手动换装本版本一次；已安装旧 EXE 不会因仓库更新而自动获得此能力。

安装使用当前程序的临时副本作为独立更新进程，等待主程序退出后只替换原 EXE。原 EXE 保留为 `.previous`；新版在完整创建界面后发送启动回执，60 秒未就绪或提前退出会恢复旧版。只操作 EXE，不覆盖 `s5a3_config.json`、备份或其它文件。目录不可写时更新失败并保留原版，不自动提权。断电或强制结束更新进程后的恢复可使用保留的 `.previous` 文件。

## 发布新版

1. 同时更新 `TOOL_VERSION` 与 `build/version_info.txt`。
2. 用 `build/S5A3服务端管理.spec` 打包并执行 `--selftest`。
3. 在本机签名，生成三个 Release 资产：

```bat
python build/make_update.py --exe build/dist/S5A3服务端管理.exe --version 2026.11.20.3 --key runtime/update-signing-key.pem --notes-file runtime/release-notes.txt --output runtime/release-2026.11.20.3
```

4. 新建 `v2026.11.20.3` Release，上传 `S5A3-ServerManager-2026.11.20.3.exe`、`update.json`、`SHA256SUMS.txt`，确认下载地址与清单一致，再完整下载验证摘要和签名。

签名私钥首次已生成在本机忽略目录 `runtime/update-signing-key.pem`。私钥必须离线备份，禁止加入仓库或 Release。其它电脑打包发布需要安全转移这份私钥；重新生成私钥会使旧客户端拒绝后续更新。仓库只包含公钥。纯净版发布脚本会清空目录，仅用于首次交付，不用于自动更新。

GitHub 无法访问或尚无签名清单时，工具保持当前版本可用。测试入口：`python tests/test_self_update.py -v`，以及 `python tests/test_pm_deploy_schema.py -v`；不连接业务服务器。

首版验证：6 项更新专项测试、3 项部署回归通过；打包 EXE 的 `--selftest` 通过；使用隔离目录及同构建版本的 Windows 进程夹具，实际完成主进程退出、EXE 替换、PyInstaller 子进程界面回执、旧 EXE 备份及配置保留。失败回滚和篡改拒绝由隔离单元测试覆盖。没有更新线上服务器或业务数据库。
