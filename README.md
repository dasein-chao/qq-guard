# QQ 消息智能分级与提醒

把 QQ 消息分成三档：**紧急→立即提醒**、**其他→定时摘要**、**水群→过滤**。

当前完成的是**与账号无关的核心部分**：分级引擎、手机提醒通道、本地存储、摘要、消息入口。可以直接用样例验证效果，不需要 QQ 账号。

## 快速验证

```bash
python main.py local          # 用样例消息跑一遍，看分级和摘要
python main.py stats          # 查看存储统计和最近消息
python main.py config         # 重新生成默认配置文件（会覆盖现有 config.json）
```

## 分级逻辑

每条消息按规则打分（0-100）：

| 信号 | 分值 |
|---|---|
| 紧急关键词（崩了/急诊/立刻…） | 每个 +50，上限 100 |
| 重要关键词（会议/截止/面试…） | 每个 +18，上限 36 |
| 被 @（含 @全体成员） | +40 |
| 重要联系人 | +35 |
| 私聊 | +12 |
| 水群关键词（签到/红包/早安…） | 每个 -30 |
| 黑名单群 | 直接归为过滤（重要联系人或紧急词除外） |

- ≥75 分 → **紧急**，立即提醒
- ≥35 分 → **重要**，进摘要
- 其他 → 普通（默认也进摘要）或过滤（命中水群词/黑名单群）

所有关键词、联系人、群名单、阈值都在 `config.json` 里改。

**AI 兜底**：规则分落在模糊区间（默认 20-70）时，可调用 OpenAI 兼容接口判断。在 `config.json` 里设 `ai_fallback_enabled: true`，填 `ai_api_base`、`ai_api_key`、`ai_model`。不配也能用，纯规则。

## 手机提醒配置

### 方式一：邮件（推荐，手机邮箱 App 有推送）

QQ 邮箱为例：

1. QQ 邮箱 → 设置 → 账户 → 开启 IMAP/SMTP 服务
2. 生成**授权码**（不是登录密码）
3. 在 `config.json` 的 `notify` 里填：

```json
{
  "email_enabled": true,
  "smtp_host": "smtp.qq.com",
  "smtp_port": 465,
  "smtp_user": "你的QQ号@qq.com",
  "smtp_password": "授权码",
  "email_to": "你的QQ号@qq.com"
}
```

### 方式二：Webhook（Server酱³ / 企业微信 / 钉钉 / Bark）

设 `webhook_enabled: true`，填 `webhook_url` 和对应的 `webhook_payload_template`。模板里 `{title}` 和 `{content}` 是占位符。

- **Server酱³**（微信推送）：URL 形如 `https://sctapi.ftqq.com/<SCKEY>.send`，模板用 `{"title":"{title}","desp":"{content}"}`
- **企业微信机器人**：URL 形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx`，模板用 `{"msgtype":"text","text":{"content":"{title}\n{content}"}}`
- **钉钉机器人**：`{"msgtype":"text","text":{"content":"{title}\n{content}"}}`
- **Bark**（iOS）：URL 形如 `https://api.day.app/<key>/`，模板按需调整

### 摘要时间

`summary_hour` 默认是 21（晚上 9 点）。摘要发送后相关消息标记为已摘要，不会重复发。

## 实时接入（需要协议端，暂未启用）

`config.json` 的 `source.websocket_url` 默认 `ws://127.0.0.1:3001`，对应 OneBot v11 正向 WebSocket。

协议端就绪后：

```bash
pip install -r requirements.txt
python main.py listen    # 只监听
python main.py daemon    # 监听 + 定时摘要
```

支持的协议端：NapCat、Lagrange 等输出 OneBot v11 事件的项目。

**重要**：协议端部分尚未在本项目里实际联调。接入前请先阅读 README 末尾的"未解决事项"。

## 文件结构

```
main.py                 入口
config.json             配置文件（所有标准在这里改）
sample_messages.json    测试样例
qq_guard/
  config.py             配置定义与加载
  models.py             消息与分级结果模型
  engine.py             分级引擎（规则 + AI 兜底）
  notifiers.py          邮件 / Webhook / 桌面提醒 / 摘要
  sources.py            OneBot WebSocket / 本地样例入口
  store.py              SQLite 存储
  app.py                编排与 CLI
messages.db             运行数据（自动生成）
```

## 兜底：备份与换号迁移（重要）

封号是分级处罚：异常提醒 → 临时封禁 → 功能限制 → 永久封禁。挂协议端只监听不发言属于低风险，但不是零风险。所以做了完整兜底。

**日常备份**（建议每次改完规则、或每周跑一次）：

```bash
python main.py backup --label 周备份   # 打包配置+数据库+可读归档+迁移清单
python main.py backups                 # 列出所有备份
python main.py verify --from latest    # 校验备份完整性（SHA256）
```

备份目录在 `backups/<时间戳>/`，包含：
- `config.json`、`messages.db`：一键恢复用的原始数据
- `export/messages.csv`、`export/messages.md`：可读归档，任何工具都能打开
- `export/rules.txt`：规则快照，换号照抄
- `MIGRATION_GUIDE.md`：换号迁移清单
- `manifest.json`：文件校验和

**万一封号，换新号恢复**：

```bash
python main.py restore --from latest
```

恢复前会自动把当前现状再存一份备份，防止误覆盖。恢复是**一键**的，但下面这些**做不到自动**，因为数据在腾讯服务器上，只能照清单手动做：

- 注册新号、实名、养号
- 用新号重新加回所有要监控的群（群号列在 `export/messages.csv` 里）
- 旧手机重新部署、扫码时改用新号
- 配置里写的是旧 QQ 号的地方（`MIGRATION_GUIDE.md` 会自动列出来）

## 部署路线（已定：旧手机 + 主号 + 流量卡随身带）

- **宿主**：闲置安卓手机，随身带，插唯一那张流量卡，**全程只用流量，不连 WiFi、不连热点**。
- **主手机**：没卡了，连校园网 WiFi 即可；要上网也可用旧手机开的热点。两件事互不影响。
- **为什么流量卡反而最稳**：腾讯风控盯的是机房 IP、短时间跨城、高频发言。手机流量 IP 随基站渐变是正常行为，只监听不发言属低风险。旧手机随身带，IP 变化是真人行为，不算异常。
- **别用主手机热点给旧手机**：主手机切 WiFi/流量会让出口 IP 跳变，反而更像异地登录。流量卡插旧手机就没这问题。
- **续航**：旧手机同时跑协议端 + 开热点会耗电翻倍，用 20000mAh（撑一晚）或 30000mAh（撑两天）充电宝。别用电脑，普通充电宝带不动。
- **同时在线**：协议端登录平台选「iPad」或「电脑端」，和日常手机 QQ 不冲突。别选「手机」平台，会互踢。
- **30 天**：PC 端协议每 30 天要重新登录一次，到时扫码即可。

部署脚本（Termux + NapCat）等你确认旧手机系统版本后给。

## 判定标准（待你提供）

当前是通用默认值。把你关心的**群名单、紧急关键词、重要联系人**发我，直接改进 `config.json`，不用动代码。

## 未解决事项（早期记录）

1. **消息从哪来**：你否定了官方 bot（必须进群），要全量消息只能第三方协议端。但主号 + 云端服务器 + 第三方协议是封号率最高的组合。稳妥路线是固定家庭网络的常驻小设备（旧手机 / 迷你主机 / 树莓派）+ 养过的号。需要你定。

2. **筛选标准**：当前是通用默认值。你的具体标准（哪些人、哪些群、哪些词算紧急）还没给。给我之后直接改进 `config.json`，不用改代码。

3. **协议端联调**：WebSocket 入口写好了，但需要你确定宿主设备和账号后才能真正联调。云端 IP 挂 QQ 会触发风控（登录态吊销、频繁扫码、错误码 168 账号受限），这是腾讯的风控机制，不是代码能绕过的。
