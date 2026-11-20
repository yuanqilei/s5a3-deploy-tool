# S5A3 Deploy Tool

S5A3 服务端部署工具：Python 源码、打包配置和输入素材，产出独立部署工具 EXE。

## 归属

- 原始资料：`04_部署工具-S5A3服务端管理-v2026.11.19.1`

## 迁入范围

- `src` 源码
- 构建 / 打包配置及输入素材
- 必要测试与文档

## 发布责任

独立部署工具 EXE。大文件（EXE、素材二进制）放 Release 附件或对象存储，不提交 Git 历史；每个 Release 附 `SHA256SUMS`。

## 禁止提交

口令、Token、私钥、证书私钥、真实 `.env`、数据库连接串、玩家数据、线上配置、本机 DPAPI 凭据。配置示例一律用占位值。

## 配套

- 服务端：`yuanqilei/s5a3-server`
- 客户端：`yuanqilei/s5a3-client`
- 登录器 / GM：`yuanqilei/purplemagic-nexus`
