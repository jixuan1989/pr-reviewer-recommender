# PR Reviewer 自动推荐引擎 — 设计文档

> 目标：为 apache/iotdb 与 apache/tsfile 提供 GitHub Action，PR 打开时基于「涉及模块 + 模块关系 + 历史专长」自动推荐并请求 reviewer。
> 形态：GitHub Action（Python，零第三方依赖，API 走 runner 自带的 `gh` CLI）
> 版本：v1（2026-11-05）

## 1. 数据流

```
PR opened/synchronize
  └─ workflow (pull_request target: opened)
      ├─ actions/checkout (fetch-depth: 0，全量历史)
      ├─ gh pr view --json files,author   → 变更文件、PR 作者
      ├─ 模块解析：变更文件 → 模块集合 M
      ├─ 信号采集（时间窗口：PR 前 365 天）
      │   ├─ git log（本地）：commit 作者 × 模块，时间衰减加权
      │   └─ gh api graphql：历史 PR 的 review 记录 × 模块
      ├─ 打分 + 过滤（排除作者/bot/不活跃者）
      └─ 输出：
          ├─ gh pr comment：推荐理由（打分明细）
          └─ POST requested_reviewers：正式请求 top-N
```

## 2. 模块解析（modules.py）

规则按优先级，通用不写死仓库：

1. **Maven 多模块**：递归扫描（深度≤2）所有 `pom.xml` 的 `<module>` 条目 → 模块 = 相对目录。
   iotdb 场景：根 pom 有 `iotdb-core`，其内层 pom 有 `iotdb-core/iotdb-engine` 等，**最长前缀匹配**文件路径。
2. **构建标记目录**：顶层与二层目录含 `go.mod` / `CMakeLists.txt` / `pyproject.toml` / `package.json` / `Cargo.toml` / `build.gradle` 的目录视为模块。
   tsfile 场景：`java/`、`cpp/`、`go/`、`python/`，其中 `java/` 内部再细分 `java/tsfile` 等 Maven 模块。
3. **兜底**：顶层目录即模块。

文件 → 模块：最长前缀匹配；不匹配任何模块（如根目录 README）→ 记 `"(root)"`。

## 3. 信号与打分（history.py / reviewlog.py / score.py）

时间衰减：`w = 2^(-Δdays / HL)`，半衰期 HL = 180 天（可配）。参考时间 = PR 创建时刻（回测时不产生未来信息泄漏）。

对候选 u、PR 涉及模块集合 M：

```
score(u) = Σ_m∈M [ λc·C(u,m) + λr·R(u,m) ] + coupling(u, M)

C(u,m) = Σ_{u 在模块 m 的 commit} w(t)          λc = 1.0
R(u,m) = Σ_{u 对涉及 m 的 PR 的 review} w(t)    λr = 1.5  （review 是更强的行为信号）
coupling = |M|>1 且 u 对每个 m∈M 都有 C>0 时：λx · min_m C(u,m) · (|M|-1)   λx = 0.5
```

耦合加分的含义：改动横跨多个模块时，**在所有相关模块都有历史**的人最理解模块间接缝——这正是「模块关系」维度的推荐依据。
（v2 再加：Maven 模块依赖边、import 依赖图——PR 触及被依赖方时的下游 owner 提示。）

### 身份归一

- git log 只给 `name <email>`；Action 模式经 GitHub API（commits 接口）拿 login；本地模式用 name。
- 信号主键：login 优先，name 兜底。

### 过滤

- PR 作者本人
- bot：`[bot]`、`bot`、`github-actions`、`dependabot` 等模式（姓名或邮箱匹配）
- 不活跃：窗口内最后动作 > 365 天前
- （v2）已挂起的 review 请求过多者降权

## 4. 输出

```markdown
## 🤖 Reviewer 推荐（模块：java/tsfile, java/common）
| Reviewer | 分数 | 依据 |
|---|---|---|
| @xxx | 12.3 | java/tsfile 18 commits(近半年 6) · 12 次 review · 跨模块覆盖 |
| @yyy | 9.1  | java/common 22 commits · 5 次 review |

<details><summary>打分明细</summary>每模块 C/R/coupling 分项</details>
```

随后 `POST /repos/{o}/{r}/pulls/{n}/requested_reviewers`（正式请求）。

## 5. 回测（scripts/backtest.py）

取最近 N 个 merged PR，对每个 PR：
- 仅用 **PR 创建时刻之前** 的 git/API 数据做推荐（无泄漏）
- 与真实 reviewer（reviews 节点去重作者）比对
- 指标：Top-1 命中率、Top-3 命中率、MRR

## 6. 已确认的仓库事实（2026-11-05）

- 两仓库均无 CODEOWNERS（.github 与根目录都查过，404）→ 没有静态信号可依赖，动态推荐价值更大
- 两仓库 workflows 均为纯 CI，无 reviewer 自动分配 → 空白
- tsfile：多语言 monorepo（java/cpp/go/python），按语言分工明确（近 12 个月贡献者 17/14/1/10 人）
- tsfile/java Maven 模块：common, tsfile, examples, tools

## 7. 回测结论（2026-11-05，无泄漏：仅用 PR 创建前的数据）

| 仓库 | 有效样本 | Top-1 | Top-3 | MRR |
|---|---|---|---|---|
| apache/iotdb | 36 | 36% | **75%** | 0.528 |
| apache/tsfile | 15 | 40% | 53% | 0.444 |

关键观察：
- miss 案例多为 ground truth 噪声——maintainer「全局兜底」review 任何模块（jt2594838 等），并非该模块最合适人选；
- tsfile 的 cpp/test、cpp/tools 粒度区分生效：cpp/src 大佬不再霸榜非其领域的 PR；
- review 记录稀疏（Apache 走邮件列表异步 review），60 个 PR 仅 15 个有记录 → review 信号在 iotdb 上权重更显著；
- 调参实验（半衰期/窗口/λ 网格）尚未做，当前数字为默认参数。

## 8. 调参结论（2026-11-05，27 组网格 × 87 PR，事件冻结后内存重算）

| 参数 | 结论 |
|---|---|
| λr（review 权重） | **最强杠杆**：2.5 在所有半衰期组合下全胜 → 默认 1.5→2.5 |
| 半衰期 | 90d(75%) vs 180d(72%) 差异在噪声内（≈4 个 PR），365d 明显衰退 → 保持 180d 求稳健 |
| λx（耦合加分） | 对命中率无统计影响（同组内 0/0.5/1.0 同分）→ 保留 0.5（无害，且是模块关系叙事核心） |

新默认（180d, 2.5, 0.5）：Top3 72% / Top1 52% / MRR 0.595（旧默认 70%/50%/0.578）。
教训：单点最优（90d+2.5 = 75%）不做默认，87 样本不足以支撑 4 个百分点的差异。

## 9. GitHub 实测记录（2026-11-05）

在 apache/tsfile 的一个 fork 上以本地 action 形态端到端验证（演示 PR 可按需提供）：
- 首测发现 local action 需 workflow 先显式 checkout（action 内部 checkout 救不了解析阶段）
- 重测验证 (root) 剔除修复：colinleeo（19 个 root commits）跌出前三，jt2594838（双模块 12+6）登顶
- 评论就地更新（synchronize 不刷屏）；request reviewers 在 fork 无 collaborators 时仅告警 ✓
- 测试痕迹已清理（分支删除、PR 关闭）

## 10. v2（2026-11-05 实现并验证）

### 10.1 Maven 依赖图接缝加成（deps.py）

解析各 pom 的 reactor 坐标（groupId:artifactId，groupId 可继承 parent）与 `<dependencies>`，
丢弃外部依赖，建模块双向邻接表（tsfile 4 条边，iotdb 189 条边）。

```
seam(u) = λd · Σ_{m∈M} Σ_{n∈adj(m)\M} min(C(u,m), C(u,n))    λd = 0.3
```
含义：PR 触及 m，候选人在 m 和其相邻依赖 n 两侧都有历史 → 懂这条接缝的人加分。
调参（81 组合×87 PR）：λd=0.3 与 0.0 差异在噪声内（76% vs 75%），**不劣化且提升理由可解释性**；
λd=0.6 有害（接缝加成放大陈旧信号，365d 半衰期下 Top3 崩至 54-66%）。→ 取 0.3。

### 10.2 Review 负载均衡（reviewlog.fetch_pending_reviews）

实时统计 open PR 的 requested_reviewers（REST /pulls?state=open）：
- 每个挂起请求对总分乘 0.75（`workload_factor`）
- 挂起 ≥ 6（`max_pending`）直接排除

**注意：仅 Action 实时模式生效**——回测的 ref 在过去，与当前 open 状态不可比，故回测不含此信号。

### 10.3 建议性 CODEOWNERS 生成（scripts/gen_codeowners.py）

按模块衰减权重取 top-2 作者（login 归一），输出 CODEOWNERS 格式建议（低信号模块跳过）。
tsfile 输出与社区认知一致：cpp 系 ColinLeeo/hongzhi-gao，java 系 jt2594838/HTHou/shuwenwei。

### 10.5 人工评判与 v2.1（2026-11-05，社区成员逐案例确认）

9 个代表案例的人工评判结论：
- **B 类（#1026，实际 reviewer 为评判者本人的兜底 review）与 D 类（#1066，jackietien97 近期在
  java/tsfile 参与度确实低）均为 ground truth 噪声** → 回测指标低估真实推荐质量；
- 产品决策：**至少推荐 2 个 reviewer**（`effective_top_n` 下限钳制，社区反馈）；
- 冷模块低置信标注上线。**踩坑记录**：最初用总分判定低置信，被两个问题打脸——
  接缝加成虚增冷模块分数（iotdb-client/cli 涨到 12.8）、小仓库总分天然偏低
  （java/tsfile 9.9 被误标）。改为**证据计数**（top 候选在触及模块的 commit+review
  次数 < 5），对仓库规模、衰减窗口、加权参数均不变。实测：cli 模块（4 次）标注 ✓，
  java/tsfile（21 次）不标注 ✓。

## 11. 局限 / 待办

## 11. 局限 / 待办

- `fetch-depth: 0` 在 iotdb 这类大仓库较重 → v1.1 可换 `git fetch --shallow-since`
- merge vs squash 策略影响 commit 作者归因（Apache PR 多为 squash，作者保留，可接受）
- go 目录仅 1 位贡献者：单人模块退化为「唯一专家推荐」，本身即正确行为
- v2：模块依赖图加权、review 负载均衡、生成建议性 CODEOWNERS
