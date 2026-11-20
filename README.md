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

最新 exe 见 [v2026.11.20.1](https://github.com/yuanqilei/s5a3-deploy-tool/releases/tag/v2026.11.20.1)（附 `SHA256SUMS.txt`）。

## 发布责任

独立部署工具 EXE。大文件（EXE、素材二进制）放 Release 附件或对象存储，不提交 Git 历史；每个 Release 附 `SHA256SUMS`。

## 禁止提交

口令、Token、私钥、证书私钥、真实 `.env`、数据库连接串、玩家数据、线上配置、本机 DPAPI 凭据。配置示例一律用占位值。

## 配套

- 服务端：`yuanqilei/s5a3-server`
- 客户端：`yuanqilei/s5a3-client`
- 登录器 / GM：`yuanqilei/purplemagic-nexus`
