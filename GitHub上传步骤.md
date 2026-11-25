# GitHub 上传步骤

## 先选哪个包

| 包 | 大小 | 内容 | 传哪种仓库 |
|---|---|---|---|
| **`qq-guard-public.zip`** | 43 KB / 20 文件 | 只有代码、测试、样例、脱敏配置、**技术型 README**。无任何部署细节 | **公开仓库**（面试用） |
| `qq-guard-github.zip` | 87 KB / 34 文件 | 再加上全部内部文档：交接书、真相文档、推送设置、协议端说明、筛选模板 | **私有仓库**（自己继续开发） |

**面试用 `qq-guard-public.zip` 建公开仓库。** 里面没有协议端、旧手机、封号这些内容，面试官看到的是工程能力，不是灰色操作。

## 为什么不是我直接传

我试了两次建仓库，都返回 `403 Resource not accessible by integration`——WorkBuddy 的 GitHub 连接器只有读权限，没有建仓库权限，重新授权也一样。本地没装 GitHub CLI。这是权限边界，我不再试第三次。

两个包都已打好、验证过能直接跑。

---

# 公开版上传（面试用，推荐）

## 包内容

`qq-guard-public.zip`，43 KB，20 个文件：

- `qq_guard/` 全部源码（engine / store / notifiers / sources / backup / config / models / app）
- `main.py`、`test_phase1.py`、`test_ntfy.py`
- 三组样例消息
- `README.md`（重写成技术型：分级设计、技术要点、快速开始、测试说明）
- `config.example.json`（全部字段是占位符）
- `.gitignore`

**已剔除**：`.protocol_token`、真实 `config.json`、`messages.db`、`backups/`、协议端包、`model_bridge/`、`ai_client.py`（死代码）、所有含部署细节的文档。

**验证过**：解压 → `cp config.example.json config.json` → `python test_phase1.py` 27/27、`python test_ntfy.py` 全通过、CLI 跑出正确分级。

## 步骤（3 分钟）

1. 打开 https://github.com/new
   - Repository name：`qq-guard`
   - **选 Public**
   - **不要**勾 Add README / .gitignore / license，保持空仓库
   - 点 Create

2. 在新仓库点 **`uploading an existing file`**

3. 把 `qq-guard-public.zip` **解压**，进到 `qq-guard` 文件夹，把里面的**所有文件和文件夹**（`qq_guard/`、`main.py`、`README.md` 等）拖进上传框

   > 注意：上传框里应该看到 `qq_guard/` 文件夹、`main.py` 等直接平铺，**不要**多出一层 `qq-guard/` 目录。

4. Commit message 填 `Initial commit`，点 `Commit changes`

## 面试时怎么介绍这个项目

一句话：**"一个按优先级自动分级 IM 群消息的本地工具，纯规则引擎，零依赖零成本。"**

可以展开讲的点：
- 四级分级模型，把「重要」和「紧急」拆开评估
- 规则打分 + 上下文窗口 + 队列式去重 + 时限识别
- SQLite 数据迁移（旧三档→新四级）、分级留存策略
- 27 个测试覆盖分级/提取/去重/摘要/查询/留存/备份恢复
- 修过 WAL 模式备份导致数据库损坏的真实 bug

不要提的：怎么接 QQ、挂在哪台设备、封号风险。跟面试无关，还容易引发负面联想。

---

# 完整版上传（自己继续开发用）

`qq-guard-github.zip`，87 KB，34 文件。比公开版多出：交接书、真相文档、推送设置、协议端说明、筛选模板等全部内部文档。

步骤同上，但**选 Private**。

---

## 之后怎么继续开发

```bash
git clone <仓库地址>
cd qq-guard
python -m pip install -r requirements.txt
cp config.example.json config.json     # 私有版要填自己的 ntfy topic
python test_phase1.py                   # 确认 27/27
```

## 想让我以后能直接 push

需要在 GitHub Settings → Applications 里给 WorkBuddy 连接器授予**仓库写入权限**。只能你操作。
