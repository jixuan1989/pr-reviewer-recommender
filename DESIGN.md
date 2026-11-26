# PR Reviewer 自动推荐引擎 — 设计文档

> 目标：为 apache/iotdb 与 apache/tsfile 提供 GitHub Action，PR 打开时基于「涉及模块 + 模块关系 + 历史专长」自动推荐并请求 reviewer。
> 形态：GitHub Action（Python，零第三方依赖，API 走 runner 自带的 `gh` CLI）
> 版本：2026-11-25 修订（v1、v2、v2.1 完成于 2026-11-05；相对 v2.1 的改动见 §12）

## 1. 数据流

```
pull_request_target: opened / ready_for_review（可选 synchronize）
  │   workflow 文件必须在默认分支上，否则这个事件不会触发
  └─ composite action
      ├─ checkout PR 的 base 提交（fetch-depth: 0，不保留凭据）
      │     不检出、也不运行 PR 里的代码
      ├─ REST：PR 作者、创建时间、完整文件列表（超过 100 个文件时翻页）
      ├─ 模块解析：变更文件 → 模块集合 M（最长前缀匹配）
      ├─ 信号采集（参考时间 = PR 创建时刻；窗口 365 天）
      │   ├─ git log（本地，base 提交的历史，--no-merges）：commit 作者 × 模块，时间衰减
      │   ├─ REST commits?sha=<base 提交>：git 作者邮箱 → GitHub 账号
      │   ├─ GraphQL：最近 100 个已合并 PR 的 review 记录 × 模块，时间衰减
      │   └─ REST：当前打开的 PR 上各人被请求、尚未完成的 review 数（负载）
      ├─ 打分 + 过滤
      ├─ POST requested_reviewers：逐人请求，记下每个人的结果
      └─ 评论（新建，或就地更新上一次的）：推荐表 + 请求结果
```

为什么是 `pull_request_target`：从 fork 发来的 PR 在 `pull_request` 事件下拿到的是只读的
`GITHUB_TOKEN`，写评论会 403。两个仓库合并过的 PR 多数来自 fork（§6），这是常态而不是特例。
代价是它以默认分支的上下文、带写权限运行，所以 action 只读 base 提交，并把 PR 数据当数据看。

## 2. 模块解析（modules.py）

规则按优先级，通用、不写死仓库：

1. **Maven 多模块**：递归扫描（深度 ≤ 2）所有 `pom.xml` 的 `<module>` 条目 → 模块 = 相对目录。
   如 iotdb 的 `iotdb-client`、`iotdb-core/datanode`，tsfile 的 `java/tsfile`、`java/common`。
2. **构建标记目录**：深度 1–2 的目录里有 `go.mod` / `CMakeLists.txt` / `pyproject.toml` / `setup.py` /
   `package.json` / `Cargo.toml` / `build.gradle` / `Makefile` 之一的，视为模块。
3. **兜底**：顶层目录即模块（tsfile 的 `docs`）。

跳过的目录按**完整目录名**比较（`.git`、`.github`、`target`、`node_modules`、`build`、`out`、`dist`）。
早先按前缀比较，会把 `distribution` 这种以 `dist` 开头的真实模块吞掉（2026-11-25 修正）。

文件 → 模块：最长前缀匹配。不属于任何模块的文件（如根目录 README）记为 `(root)`；
评分只看真正的模块，PR 只碰到 `(root)` 文件时才按 `(root)` 计，权重 0.2。

## 3. 信号与打分（history.py / reviewlog.py / score.py）

时间衰减：`w = 2^(−Δ天 / HL)`，半衰期 HL = 180 天。参考时间 = PR 创建时刻（回测时不产生未来信息泄漏）。
窗口（`--since` / `--before`）按提交的 **committer date** 截断，衰减按 **author date** 计算。

对候选 u、PR 涉及模块集合 M：

```
score(u) = [ Σ_{m∈M} rw(m)·( λc·C(u,m) + λr·R(u,m) ) + coupling(u, M) + seams(u, M) ] · workload(u)

C(u,m)   = Σ_{u 在模块 m 的 commit} w(t)              λc = 1.0
R(u,m)   = Σ_{u 对涉及 m 的已合并 PR 的 review} w(t)   λr = 2.5（review 是离任务更近的信号）
rw(m)    = 1；(root) 为 0.2
coupling = |M|>1 且 u 在每个 m∈M 都有 commit：λx · min_m C(u,m) · (|M|−1)     λx = 0.5
seams    = λd · Σ_{m∈M} Σ_{n∈adj(m)\M} min(C(u,m), C(u,n))                   λd = 0.3
workload = 0.75^(u 被请求、尚未完成的 review 数)；达到 6 个时不是候选
```

- **耦合加分**：改动横跨多个模块时，在所有相关模块都有历史的人最理解模块之间怎么配合。
- **接缝加分**（`deps.py`）：PR 触及 m，候选人在 m 和它的 Maven 依赖邻居 n 两侧都有提交 → 懂这条接缝。
  依赖图来自各 pom 的 reactor 坐标与 `<dependencies>`，丢弃外部依赖，模块间双向邻接。
- **排序键**：`(−score, −commit_score, 账号)`，结果与进程无关。
- **至少推荐 2 个**（`effective_top_n`）：只给一个名字，没有备选。

耦合与接缝加分对命中率没有可测的影响（§8）；保留它们是因为评论里的推荐理由要说得通，
不是因为它们提高了指标。

### 身份归一

- `git log` 只给 `Name <email>`。邮箱 → GitHub 账号经 commits 接口：GitHub 在服务端按提交邮箱关联账号，
  所以 `noreply` 地址、一个人用几个提交名的情况也能对上。
- 提交列表必须带 `sha=<检出提交>`。不带的话接口列的是**默认分支**的提交；PR 打到别的分支时那是另一段历史，
  只在 base 分支上提交的作者会被当成「未关联账号」而丢弃（2026-11-25 修正）。
- 只按邮箱归一，解析不到的作者直接丢弃，不回退成提交名。提交名不是账号：既不能请求 review 也不能 @，
  留下来还会绕过作者过滤（过滤比较的是账号）。
- bot 按 GitHub 的账号类型判定（GraphQL `__typename` / REST `type` 为 `Bot` / `Mannequin`）。
  账号类型拿不到时才用名字启发式：按整词匹配 `bot` / `codecov` / `copilot` / `dependabot` / `renovate`，
  或整个账号是 `github-actions` 这类已知自动化账号。

### 过滤

- PR 作者本人
- bot
- 待审请求 ≥ 6 个的人
- 不活跃：最后一次提交距参考时间超过 `inactive_days`（365）天。窗口同为 365 天时这一条基本不会触发，
  窗口调大后才有意义。
- review 信号里不含作者对自己 PR 的 review：`COMMENTED` 状态常被作者用来行内回复，那是对话不是评审。
  回测的真值同样不含（§5）。

## 4. 输出

每个 PR 一条评论，之后的运行就地编辑它（更早版本留下的中文评论也被识别，就地替换成新的）。
评论用英文写（2026-11-26 起；此前的版本只有中文）：

```markdown
## 🤖 Suggested reviewers

This PR touches `java/common` and `java/tsfile`.

| Reviewer | Score | Why |
|---|---|---|
| @alice | 15.8 | `java/tsfile` 12 commits, 4 reviews · `java/common` 6 commits · **history in every touched module** |
| @bob | 7.4 | `java/tsfile` 16 commits · `java/common` 1 commit · **history in every touched module** |

Reviews requested from @alice and @bob.

<details><summary>How the score is computed</summary>

Commits and reviews in the modules this PR touches, each weighted by a 180-day half-life. A PR that touches several modules adds a bonus for people with history in every one of them, and for people with history on both sides of a dependency between a touched module and its neighbours. People with several reviews waiting rank lower.
</details>
```

表格下面那一行是对 review 请求的**如实汇报**，放在折叠区外面。四种情况：

- 请求成功：`Reviews requested from @alice and @bob.`；已在请求列表里的人另起一句 `Already requested: @carol.`
- 有人请求失败：`Could not request a review from @carol (Reviews may only be requested from collaborators. (HTTP 422));
  add @carol in the Reviewers box.`。非协作者不能被请求，这是 GitHub 的规则。
- 只评论模式（`request-reviewers: false`）：`No review was requested (request-reviewers: false).`
- 试运行（`dry-run: true`）：评论不发，同一句话写进日志，动词换成 `Would request reviews from …`。

证据不足时表格上方多一行低置信提示：最佳候选在触及模块的 commit + review 次数少于 5
（`low_confidence_min_evidence`）。按**次数**而不是分数判断——按分数会被接缝加成和大仓库的高绝对分干扰。
`(root)` 不计入次数，所以只改了模块外文件的 PR 一定带低置信提示；这时提示写的是「PR 不涉及任何模块，
没有模块历史可依」，而不是「0 次提交」。

退出码：0 = 完成，或没有可推荐的对象；1 = GitHub 调用失败，或评论没发出去；2 = 环境变量不合法。
评论发不出去算失败，不算告警：一次什么都没发却显示成功的运行，和一次成功的运行看起来一样。

## 5. 回测协议（scripts/backtest.py）

```bash
python3 scripts/backtest.py --owner apache --name iotdb --repo /path/to/iotdb \
    --before 2026-11-25T00:00:00Z --scan-prs 200 --max-prs 100
```

- **样本**：`--before` 之前最近更新的 `--scan-prs`（默认 100）个已合并 PR 里，收到过「作者以外真人 review」
  的 PR，最新的在前，最多 `--max-prs`（默认 50）个。创建时间晚于克隆 HEAD 的 PR 不参与（克隆里的历史不够），
  脚本会计数并告警。
- **每个 PR 只用它创建时刻之前的数据**：git 提交的 committer date、review 的提交时间、历史 PR 的创建时间
  都早于它。
- **真值** = 在这个 PR 上留下 review 的、作者以外的真人。
- **指标**：Top-1 = 排第一的推荐在真值里；Top-k = 前 k 个推荐里至少有一个在真值里；
  MRR = 第一个命中的名次倒数之均值。没有任何候选的 PR 按未命中计。区间是 95% Wilson 区间。
- **review 语料**：扫描到的那批已合并 PR。action 实时运行时用的是最近 100 个，所以 tsfile 另跑了
  `--scan-prs 100 --max-prs 50` 以便对齐（§7）。

与 action 的三处差别：负载信号不回溯（拿不到「当时」的待审数）；模块图和依赖图取自克隆 HEAD，而不是 PR 当时的
base 提交；所有 PR 的历史都从默认分支的克隆里读，包括打到发布分支的 PR。

## 6. 仓库事实（2026-11-25 重测）

| | apache/iotdb | apache/tsfile |
|---|---|---|
| 克隆 HEAD | 2026-11-24 | 2026-11-20 |
| 模块 | 54 | 15 |
| Maven 模块间依赖边 | 198 | 4 |
| CODEOWNERS | 无 | 无 |
| workflow 文件 | 18 个，没有分配 reviewer 的 | 15 个，没有分配 reviewer 的 |
| 合并过的 PR | 15,831 | 833 |
| 其中来自 fork | 9,592（61%） | 467（56%） |
| PR 总数 / 打到非默认分支的 | 18,075 / 4,778（26%） | 1,037 / 144 |

tsfile 是多语言 monorepo，近一年（按作者邮箱计）各语言的贡献者：java 17、cpp 14、go 1、python 10。
两个仓库都没有 CODEOWNERS，也没有任何自动分配 reviewer 的 workflow——没有静态信号可以依赖。

review 记录很稀疏。最近更新的 200 个已合并 PR 里：

| | apache/iotdb | apache/tsfile |
|---|---|---|
| review 事件 | 288 | 569 |
| 其中 PR 作者自己的 | 137（48%） | 306（54%） |
| 有「作者以外真人 review」的 PR | 99 | 51 |

也就是说，在这 200 个里，一半（iotdb）到四分之三（tsfile）的 PR 是在没有别人留下正式 review 的情况下
合并的（这里只算 GitHub 的 review 对象，评论里的「+1」不算）。回测只覆盖剩下的那些。

## 7. 回测结果（2026-11-25）

默认参数，`--before 2026-11-25T00:00:00Z --scan-prs 200 --max-prs 100`：

| 仓库 | 样本 | Top-1 | Top-3 | MRR |
|---|---|---|---|---|
| apache/iotdb | 99 | 23%（16–32%） | 77%（68–84%） | 0.487 |
| apache/tsfile | 51 | 57%（43–69%） | 82%（70–90%） | 0.670 |
| 合并 | 150 | 35% | 79% | 0.549 |

tsfile 用 action 的语料量（`--scan-prs 100 --max-prs 50`）：16 个 PR，56%（33–77%）/ 81%（57–93%）/ 0.688。
三次运行都没有「无候选」的 PR。

读这张表时要知道的：

- **Top-1 低、Top-3 高**（iotdb 23% 对 77%）：名单里有人是对的，但排不出唯一的首选。
  用这份名单时看整个名单，别只看第一个名字。
- **tsfile 略高于 iotdb**（82% 对 77%），但两者的区间重叠，这点差异不足以下结论。
- **样本只占已合并 PR 的一半到四分之一**（§6），结论不能外推到没人 review 的 PR 上。
- **tsfile 的样本对 `--scan-prs` 敏感**：最近更新的 100 个里只有 16 个合格，200 个里有 51 个。
  原因没有查；至少说明「最近更新」作为抽样口径不稳定。
- 与 2026-11-05 的旧结果（iotdb 36 个 PR：36% / 75% / 0.528；tsfile 15 个 PR：40% / 53% / 0.444）不可比，
  原因有三：真值不再含作者自评和 bot；样本定义变了、样本量从 36 / 15 变成 99 / 51；评分实现修过几处（§12）。

## 8. 调参（2026-11-25，150 个 PR × 135 组，`scripts/tune.py`）

事件采集一次后冻结，参数组合在内存里重算。下表是每个参数取各值时、对其余参数取平均的 Top-3 命中率：

| 参数 | 取值 → 平均 Top-3 | 读法 |
|---|---|---|
| λr（review 权重） | 0 → 60%，1.0 → 74%，1.5 → 77%，2.5 → 80%，4.0 → 81% | **唯一影响大的参数** |
| 半衰期 | 90 天 → 78%，180 天 → 74%，365 天 → 71% | 越短略好，差距小 |
| λx（耦合加分） | 0 → 75%，0.5 → 74%，1.0 → 74% | 没有影响 |
| λd（接缝加分） | 0 → 76%，0.3 → 75%，0.6 → 73% | 没有影响 |

默认组合（180 天，2.5，0.5，0.3）：79% / 35% / 0.549。135 组里最好的是（90 天，4.0，1.0，0.0）：
85% / 39% / 0.589，但 43 组的 Top-3 不低于默认组合。

**默认参数不改。** 从 135 组里挑最大值有选择偏差——在同一批样本上挑出来的最优值，换一批样本通常回落；
85% 对 79% 只差 9 个 PR，也没有做配对显著性检验。半衰期调短还会让小仓库的候选池变薄。
11-05 的两轮调参（27 组、81 组，87 个 PR）在旧协议下得到的是同一组默认值。

## 9. GitHub 实测记录

**2026-11-05**（apache/tsfile 的一个 fork，本地 action 形态）：

- 本地 action（`uses: ./...`）必须由 workflow 先显式 checkout——action 内部的 checkout 救不了解析阶段。
- 修掉 `(root)` 污染后重测：只在根目录有提交的人跌出前三，在两个涉及模块都有提交的人排到第一。
- 评论就地更新（`synchronize` 不刷屏）。
- 当时记的「request reviewers 在 fork 没有协作者时只告警 ✓」**不成立**：发出去的请求体是字符串
  （`reviewers=a,b`），GitHub 一律回 422；代码只是打了告警，评论却照常写「已自动请求」。11-25 修复（§12）。
- 当时记的「测试痕迹已清理」也不准确：演示 PR（已关闭）和那条评论保留至今，其余测试分支与 PR 已删除。

**2026-11-25**（同一个 fork，修复后的代码）：

- **`pull_request_target` 只在 workflow 文件位于默认分支时触发**。workflow 只在 PR 的 base 分支上时，
  两次事件都没有任何运行；把它放到默认分支后，同一个 PR 的下一次事件就跑了。
- `pull_request_target` 下 `github.sha` / `github.ref` 是默认分支。不指定 `ref` 的 checkout 检出的是默认分支；
  新版 action 检出的是 PR 的 base 提交。`github.event.pull_request.base.sha` 还可能落后于 base 分支顶端
  （实测两者不是同一个提交）。
- 写入：用 `GITHUB_TOKEN` 新建评论、就地编辑、删除都成功。`GITHUB_TOKEN` 调 `/user` 得 403
  （`Resource not accessible by integration`），代码改为按「bot 作者」识别自己发过的评论。
- 请求 reviewer：fork 没有协作者，GitHub 回 422（`Reviews may only be requested from collaborators`）——
  请求体格式已被接受，拒绝的原因换成了协作者权限。评论如实报告了失败。
- 同一条评论反复就地编辑、@ 相同的人：5 次真实运行之后，PR 时间线上新增的事件数为 0。
- 同一仓库分支上的 PR 触发 `pull_request` 时，会把 base 仓库里**所有** workflow 都跑起来：11-05 演示 PR 的
  每次事件都在 fork 里触发了 9 个 workflow（8 个从上游带过来的 CI，加本 action；上游仓库本身没有运行）。
  之后在 fork 上测试，测试分支里先去掉上游的 workflow，11-25 就只触发本 action 了。
- fork 上的 Copilot 自动代码审查会在 PR 转为 ready for review 时被触发一次；`edited` 事件不会触发它。

**2026-11-26**（同一个 fork，推送到 `review-fixes` 分支的版本 `896a82b`，远程形态，按提交 SHA 引用）：

- **按提交 SHA 引用的远程 action，提交只在非默认分支上也能解析**（当时 `main` 还是旧版）。日志里是
  `Download action repository '…/pr-reviewer-recommender@896a82b…' (SHA:896a82b…)`；`actions/checkout` 那一行
  解析出的 SHA 与 `action.yml` 里钉的一致。
- **`pull_request_target` 的三种事件类型都触发了**：非 draft 的 PR `opened`；draft 转 ready 的
  `ready_for_review`；**已关闭 PR 上的 `edited`**。
- 检出的是 PR 的 base 提交而不是默认分支顶端：演示 PR 的两次运行里，默认分支顶端分别是 `a2d44aa8` 和
  `03687b7c`，日志里都是 `HEAD is now at 12e39fe9`（它自己的 base 提交）。
- 旧版本留下的中文标题评论被就地替换：两次运行都是 `updated recommendation comment … in place`，
  PR 上仍然只有一条评论；时间线（REST 口径）在两次编辑后新增 0 条。
- 请求 reviewer 被 GitHub 拒绝（422，非协作者），评论如实写了
  `Could not request a review from … (Reviews may only be requested from collaborators. (HTTP 422)); add … in the Reviewers box.`
- 只评论模式和试运行模式都按预期：前者不发请求；后者的日志是 `dry-run: nothing was posted and no reviewer was requested`。
- Copilot 自动审查在非 draft 的 PR `opened` 时就被触发了一次（11-25 只观察到 `ready_for_review` 会触发）。
- 测试 PR 只用了试运行模式：在 fork 上发一条新评论会 @ 到评论里点名的真实维护者。

**在真实 GitHub 上跑过的**：远程形态在 `pull_request_target` 上，`opened` / `ready_for_review` / `edited`
三种事件类型（包括已关闭的 PR）；按提交 SHA 引用 action；本地 action 形态在 `pull_request`（同仓 PR）和
`pull_request_target` 上（11-25）；评论的新建（11-25，中文文案）、就地编辑、替换旧版本的评论；请求被拒的路径；
只评论模式；试运行模式；base 提交落后于默认分支时检出与身份扫描的区分性场景。

**没在真实 GitHub 上跑过的**：请求 reviewer **成功**的路径（fork 没有协作者）；
跨仓库 fork PR + `pull_request` 的只读 403 路径（单个账号构造不出来）；
英文文案下**新建**评论的路径（11-26 的两次真实写入走的都是就地编辑）。
前两条由「真实 `gh` + 假 API」的集成测试覆盖；第三条与就地编辑只差一个 API 调用，同样有集成测试。

## 10. v2 / v2.1 的内容（2026-11-05）

### 10.1 Maven 依赖图接缝加成（deps.py）

见 §3「接缝加分」。

### 10.2 Review 负载均衡（reviewlog.fetch_pending_reviews）

实时统计打开的 PR 上的 `requested_reviewers`（REST `/pulls?state=open`）：每个挂起请求对总分乘 0.75，
达到 6 个直接排除。**只在 action 实时运行时生效**——回测的参考时间在过去，「当时的待审数」拿不到。

### 10.3 建议性 CODEOWNERS 生成（scripts/gen_codeowners.py）

按模块衰减权重取前两名作者，输出 CODEOWNERS 格式的建议，低信号模块跳过。它反映的是「近期谁在往各模块提交」，
不了解角色分工，会随最近的提交变化，只供维护者参考。

### 10.4 人工核对（2026-11-05）

对 9 个代表性案例逐个核对了推荐和实际 reviewer：

- 一部分未命中是真值的噪声：实际 reviewer 是临时兜底的人，或近期在该模块几乎没有提交的人。
- 据此做的产品决定：**至少推荐 2 个 reviewer**；冷模块加低置信标注。
- **踩坑**：低置信最初按总分判断，被两个问题打脸——接缝加成把冷模块的分数抬高，小仓库的总分又天然偏低，
  两头都判错。改成**证据计数**（最佳候选在触及模块的 commit + review 次数 < 5）后，它对仓库规模、
  衰减窗口、加权参数都不敏感。

## 11. 局限 / 待办

- **调参有选择偏差**：默认参数来自在同一批样本上的网格，没有做配对显著性检验（§8）。
- **三件事没在真实 GitHub 上跑过**（§9 末尾），都只有集成测试覆盖。
- **本地 action 形态要求 base 分支上已经有 action 目录**。在 action 加入之前切出去的发布分支没有它，
  这些分支上的 PR 要用远程形态。iotdb 有 26% 的 PR 打到非默认分支。
- **回测的简化**：历史统一从默认分支的克隆里读，模块图和依赖图取自克隆 HEAD，负载信号不回溯（§5）。
  对打到发布分支的 PR，action 实际读的是 base 分支的历史，与此略有差别，差别大小没有量化。
- **按时间戳防泄漏有其边界**：提交按 committer date 截断，squash / rebase 合入的提交在合并前不可见；
  但以 merge commit 合入、未被重写的分支提交，会按它当初的提交时间出现，早于它进入 base 分支的时间。
- **只能请求协作者**。推荐对象通常有提交记录，多半是协作者；偶尔有提交很多的外部贡献者，
  GitHub 会拒绝请求（422），评论里会写明。
- **参考时间固定为 PR 创建时刻**。之后由 `synchronize` 触发的重算只随变更的文件变化，不看 PR 打开之后的新提交。
- `fetch-depth: 0` 在 iotdb 这类大仓库上偏重。
- merge 与 squash：squash 合并保留原作者，作者归因不受影响。
- 只有一位贡献者的模块（tsfile 的 go）退化为「推荐唯一的那个人」，这正是对的行为。

## 12. 2026-11-25 代码审查与修复

| # | 问题 | 后果 | 修复 | 验证 |
|---|---|---|---|---|
| 1 | 请求体发成了字符串（`reviewers=a,b`） | 自动请求 reviewer 从未成功过；评论却写「已自动请求」 | 逐人 `reviewers[]=<login>`；先请求再写评论，评论如实报告每个人的结果 | 真实 GitHub：422 的原因变为「非协作者」；真实 `gh` + 假 API 的测试 |
| 2 | 只读 token 下的 403 未被捕获；评论发不出去时运行仍算成功 | 静默失败 | 评论失败 → exit 1 并给出原因；新增 `request-reviewers: false`（只评论） | 注入 403 的测试；真实 GitHub 上的只评论模式 |
| 3 | review 信号与回测真值计入作者自评 | 一半左右的 review 事件（§6）是对话而不是评审 | 排除作者自评；bot 按账号类型判定 | 单测；回测重跑 |
| 4 | 回测数字不可复现，三处文档口径不一 | 文档不可信 | 样本定义写进脚本输出；带日期、样本量、区间重跑；三处统一 | 存档日志（§7） |
| 5 | 身份归一按名字兜底；bot 提交进入索引 | 张冠李戴；@ 不到人 | 只按邮箱；解析不到即丢弃；bot 按账号类型排除 | 单测 |
| 6 | 身份扫描固定扫默认分支 | PR 打到别的分支时，作者被误判为未关联而丢弃 | `commits?sha=<检出提交>` | 单测；真实 GitHub 的区分性场景（丢弃 1 → 0） |
| 7 | 耦合加分泄漏到没触及的模块；大 PR 文件列表被截断在 100 个；评论列表不分页；按集合迭代导致评论不稳定；`distribution` 模块被吞；locale 依赖运行环境 | 排名与评论文案不确定或出错 | 逐项修复；多哈希种子的确定性回归测试 | 单测 |
| 8 | action 在 `pull_request_target` 下检出的是默认分支 | 打到非默认分支的 PR 读错历史 | 检出 `base.sha`、`persist-credentials: false`；README 与示例 workflow 改为 `pull_request_target`；action 内部的 `actions/checkout` 固定到提交 SHA（带写权限的 job 里不该有可变的代码来源） | 真实 GitHub 的运行日志 |

测试：94 个。其中一组用真实的 `gh` CLI 对着进程内的假 GitHub 跑完整的 action 流程，
覆盖请求 reviewer 的成功、已被请求、失败三种结果和评论的新建与就地更新；另一个测试守着 `action.yml` 里
与权限有关的几项（只检出 base 提交、不保留凭据、checkout 固定到提交 SHA、每个输入都接到了环境变量上）。

### 12.1 2026-11-26 补充：评论改为英文

- 评论正文由中文改为英文，标题由 `## 🤖 Reviewer 推荐` 改为 `## 🤖 Suggested reviewers`。查找「自己发过的评论」
  时两个标题都认，所以旧版本留下的中文评论在重跑时会被就地替换，不会多出一条。
- `workflow-example.yml` 的注释一并改为英文（非注释行没变）。本文件、命令行输出和脚本的输出仍是中文。
- 改写英文时发现一处中文版就有的措辞问题：只改了模块外文件的 PR，低置信提示写的是「最佳候选仅 0 次
  commit/review 记录」——`(root)` 不计入证据数，所以这个数恒为 0，读起来像「没人提交过」。现在这种 PR 的提示
  写的是「PR 不涉及任何模块，没有模块历史可依」。
- 折叠区里的评分说明补上了负载一项（待审请求多的人排名靠后），此前只提了 commit、review 和跨模块加分。
- README 里的评论样例由测试对着代码输出逐字比对（`test_readme_shows_what_the_action_renders`），
  改了渲染代码而忘了改文档会让测试失败。
- 这一版在 2026-11-26 于真实 GitHub 上跑过，记录见 §9 末尾。
