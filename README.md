# QQ 群聊智能监控系统

> **状态：开发中，还不是能直接用的成品。** 分级引擎、存储、提醒通道已完成并通过测试；但**尚未接入真实 QQ 账号联调**，也没有部署到常驻设备。现在能跑的是本地样例和单元测试，`listen` / `daemon` 两个实时模式还没有在真实群上验证过。

监控指定的 QQ 群聊，按重要性分级：**P0 立即提醒 / P1·P2 进日报 / P3 直接过滤**。保留原文，支持历史查询。**私聊一律不监控。**

纯规则引擎，不依赖任何大模型、云服务或付费 API。零成本，判断逻辑离线可跑。

## 当前状态

| 模块 | 状态 |
|---|---|
| P0-P3 分级引擎 | ✅ 完成（含结构化提取、上下文、去重） |
| SQLite 存储 + 迁移 + 分级留存 | ✅ 完成 |
| 九要素即时提醒 | ✅ 完成 |
| 五类每日摘要 | ✅ 完成 |
| 历史查询（群/日期/关键词/类别/优先级） | ✅ 完成 |
| 备份/校验/恢复/换号迁移清单 | ✅ 完成 |
| ntfy 分级推送（P0 静音也响） | ✅ 完成并验证 HTTP 200 |
| OneBot v11 WebSocket 接入 | ✅ 代码完成，待真实账号联调 |
| 旧手机常驻部署 | ⬜ 未开始（需 linux-arm64 + Termux） |

测试：`test_phase1.py` 27/27 通过，`test_ntfy.py` 全通过。

## 环境

- Python 3.13（用 `python -m pip` 装包，别用裸 `pip`）
- 唯一依赖：`websocket-client`

```bash
python -m pip install -r requirements.txt
```

## 快速开始

```bash
python main.py local --sample sample_realworld.json   # 本地跑 20 条真实场景样例，看分级
python main.py stats                                   # 存储统计 + 最近消息
python main.py summary                                 # 立即生成并发送日报
python main.py query --priority P0                     # 历史查询
python main.py backup --label 说明                      # 完整备份
python main.py listen                                  # 连协议端实时监听
python main.py daemon                                  # 监听 + 每日定时摘要
```

## 配置

复制 `config.example.json` 为 `config.json`，按需修改：

| 段 | 作用 |
|---|---|
| `rules` | P0-P3 关键词、重要联系人、忽略群、上下文窗口、去重阈值 |
| `notify` | ntfy 推送（推荐）、邮件、Webhook、桌面弹窗、摘要时间 |
| `source` | OneBot WebSocket 地址、监听哪些群、token 文件 |
| `retention` | P2/P3 保留天数、P0/P1 保留天数 |

**ntfy 配置**：手机装 ntfy App，订阅一个随机 topic，把 topic 填进 `config.json` 的 `notify.ntfy_topic`。详见 `手机推送设置.md`。

## 分级标准

判断核心：**这条消息是否影响现实事务/学习发展？错过是否有损失？** 重要 ≠ 紧急。

| 级别 | 定义 | 处理 |
|---|---|---|
| **P0** | 现在不处理就会错过或效果打折（临时改时间、今明截止、名额有限、@全体且紧急） | 立即推送 |
| **P1** | 重要不能忘的机会（科研/课题组/导师招募、竞赛、正式通知、公示） | 时效强的立即推，其余进日报 |
| **P2** | 一般有价值（资料分享、学习讨论、经验、普通讲座） | 只进日报 |
| **P3** | 低价值（闲聊、表情包、广告、重复） | 不推、不进摘要 |

## 项目结构

```
main.py               CLI 入口
qq_guard/
  models.py           Message / ExtractedInfo / Verdict
  config.py           配置定义与加载、配置升级
  engine.py           分级引擎（P0-P3、结构化提取、上下文、去重）
  store.py            SQLite 存储、迁移、查询、清理
  notifiers.py        提醒通道（ntfy/邮件/Webhook/桌面）、日报
  sources.py          OneBot v11 WebSocket 客户端、本地样例
  backup.py           备份/校验/恢复/迁移清单
  app.py              编排与 CLI 分发
test_phase1.py        核心功能测试（27 项）
test_ntfy.py          推送通道测试
test_acceptance.py    验收测试（27 项对抗性边界用例）
model_bridge/         【遗留】云服务桥接，已弃用，可删
protocol/             协议端包（仅供开发机联调，非最终宿主）
```

## 重要文档

- `手机推送设置.md` ntfy 手机端设置
- `协议端启动说明.md` 协议端启动（开发机）
- `项目真相文档.md` 项目的权威决策记录
- `我的筛选标准-请填写.md` 筛选标准模板

## 安全说明

- 协议端仅绑 `127.0.0.1:3001`，带随机 token 认证
- token 存本地 `.protocol_token`，不进代码、不进日志
- 日志不记录消息正文
- 不自动回复、不自动报名、不自动联系他人

## 已知限制

- **无法保证 100% 不封号**（非官方协议端固有风险）
- 换号后好友/群关系需手动加回，无法自动迁移
- P0 判定基于规则，极端语义可能误判
- 当前无独立文件日志系统（用 print 输出）
- 私聊按需求完全不碰
