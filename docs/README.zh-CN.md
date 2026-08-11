# endnotex9-mcp 中文说明

`endnotex9-mcp` 是一个本地优先的 EndNote X9 MCP 服务。Codex、Claude 和其他支持
MCP stdio 的客户端可以通过它检索文献、读取现有分组、设计可审计分类、生成引用并
读取 PDF 附件。EndNote 文献库不需要上传到第三方服务。

[English README](../README.md)

> [!IMPORTANT]
> 直接读取功能以 EndNote X9 为测试目标。EndNote 的底层表格式没有公开文档；使用
> 更高版本时，请先运行 `endnotex9-mcp crosscheck`。绝大多数 MCP 工具是只读的。
> 只有 `update_reference_fields` 和 `mark_references` 两个工具能写入文献库，且仅限于
> 已验证安全的一小类字段（卷号/期号/页码等书目字段、期刊、Notes、作者列表、关键词
> 列表，或未使用的 Custom 2–7 字段）。两者默认只预览不写入（`dry_run=True`），真正
> 写入前会先备份整个文献库。详见下方“安全边界”。

## 主要功能

- 只读解析 `.enl` 和 `<文献库>.Data/rdb`，不需要安装 MySQL。
- 支持 XML 导出作为回退数据源和字段级交叉验证来源。
- 支持中文分词、FTS5 全文检索以及中英文多查询合并。
- 读取 EndNote 普通分组和智能分组。
- 提供分类树、规则、证据、冲突、未决文献、抽样审核和报告组成的可复现分类流程。
- 增量提取和检索 PDF 全文，可按标题、DOI 或 EndNote 内部编号读取指定页。
- 单篇和批量工具统一支持内部编号、标题和 DOI；发生歧义时返回候选项，不擅自选择。
- 支持 APA 7th、GB/T 7714-2015 和 BibTeX。
- 文献库更新后可自动增量刷新索引。

## 安全边界

正常读取流程不会写入 `.enl`、`.MYD`、`.MYI` 或 PDF。分类状态、检索索引和 PDF
文本保存在独立的本地 SQLite 数据库中。`prepare_classification_writeback` 仍然只生成
带文件指纹的只读预览，`custom_1`（主题分类字段）没有提交通道。

有两个工具真正会写入文献库：`update_reference_fields`（卷号、期号、页码、文章编号、
卷数、期刊、Notes、标题、摘要、作者列表、关键词列表）和 `mark_references`（往 Custom
2–7 写入标记文本，用来配合 EndNote 智能分组做批量筛选——本项目本身从不删除任何条目）。
`year`（年份）完全不可写——它是文献库仅有的两个 MyISAM 索引键之一，改索引值意味着要在
B-tree 里删旧键插新键，不是简单地原地改字节。2026年8月做过一次只读的探路实验，直接在
真实 `refs.MYI` 上定位到了两棵键树的根页（page 1 对应 `id`，page 13 对应 `yearIndex`，
在里面找到了字面的 "2017" 文本确认无误），19个索引页两棵树全部对上，没有多余页面——但
页内部的具体格式和插入/删除/再平衡算法没有解出来，这块没做通，年份和真正的删除功能一样，
算独立的后续项目，不在这次的能力范围内。两者共享同一套保护机制：

- 可写字段列表是机械验证过的，不只是策略约定：`writeback.WRITABLE_COLUMNS` 只收录
  已确认不在任何 `.MYI` 索引键或排序缓存（`csort`/`misc.DbSortedRecList`）范围内的
  字段。`author`（作者）、`secondary_title`（期刊）、`keywords`（关键词）是清单里
  唯一"看起来有风险"的几个——它们正好对应 EndNote 自己的三个词表——所以在把它们加进
  可写清单前，专门逐字节读取了一份真实 1386 条文献库的 `terms.MYD`：全部 4818 行都
  符合同一种固定格式（行 id、list_id、长度前缀字符串），**没有任何字段把某个词条和
  实际引用它的 `refs` 记录关联起来**。这说明 `terms`/`jterms` 只是给"术语列表管理器"
  UI 用的去重缓存，并不是 `refs` 记录要对齐校验的结构——直接改 `refs` 里的文本，
  后果只是这份缓存暂时过期（旧词条还留着、新词条还没进去），需要用户在 EndNote 里跑
  一次 Tools/References → Define Term Lists → Update List 才会刷新，这是 UI 层面的
  滞后，不是数据损坏风险，而且本项目的写入代码从头到尾都不会打开
  `terms.MYD`/`jterms.MYD`，只碰 `refs.MYD`/`refs.MYI`/`.enl`。排序缓存那边同样是
  实测而非假设：这份真实库里的 `misc.DbSortedRecList` 已经因为之前在 EndNote 里手动
  删除了约160条记录而过期，但库本身用起来完全正常。完整依据见 `writeback.py` 模块
  文档。
- 两个工具默认 `dry_run=True`：第一次调用永远只返回差异预览，不写入任何内容；必须
  显式再传一次 `dry_run=False` 才会真正提交。
- 提交时只写入实际发生变化的字段，且总是先把整个文献库（`.enl` 加完整 `rdb` 目录）
  备份到一个新的带时间戳的目录，备份路径会在返回结果的 `backup_path` 中给出。
- 每次写入后都会逐块重新读取文件校验：目标字段必须精确等于预期值，其余记录必须与
  写入前逐字节一致，任何不一致都会在覆盖真实文件之前直接报错。
- `.enl` 镜像在 `refs.MYD`/`refs.MYI` **之前**重建并校验，所以注定失败的重打包不会
  留下"rdb 已更新、镜像还是旧的"这种分叉状态，而是什么都没动。重打包会把压缩包里的
  每一个成员都原样搬过去，只替换 `refs.MYD`：目前检查过的库里只有 `refs.MYD` 和
  `refs.frm` 两项，但万一存在没预料到的成员，静默丢弃是不可恢复的。
- **真正提交前必须先关闭 EndNote。** 两个工具都是直接改 `refs.MYD`/`refs.MYI`/`.enl`
  这几个文件，完全绕开 EndNote 自己的进程。如果 EndNote 还开着库，轻则写入因为文件
  被占用而失败，重则 EndNote 内存里那份过期的旧数据后续被写回磁盘时，会把这次修改
  悄悄覆盖掉。代码里有一个尽力而为的锁检测（`writeback._ensure_unlocked`，提交前
  会先试着以读写模式打开这几个文件，打不开就直接拒绝），但这不是绝对保证——如果
  EndNote 打开文件时用的是允许其他进程共享读写的模式，这个检测是发现不了的。所以
  无论检测结果如何，提交前都要先手动关闭 EndNote。

`writeback.py` 里更底层、可写任意列的 API（`set_column`/`set_fields`）本身并不对
MCP 客户端暴露，只有 `commit.py` 里这两个字段范围受限的封装才是工具。继续扩展写回
能力前请先阅读 [SECURITY.md](../SECURITY.md)，并始终先在真实文献库的一次性副本上
验证，确认其仍能在 EndNote 中正常打开、排序和检索后，再考虑用于真实文献库。写入
`author`/`journal`/`keywords`后如果在意自动补全是否同步，记得在 EndNote 里手动跑一次
Update List——本工具不会替你更新词表。

## 作为插件安装（Claude Code 或 Codex）

本仓库同时是一个 agent 插件：一个 MCP server 加五个技能。两个 agent 都以此为最短路径，
只需要 PATH 上有 [uv](https://docs.astral.sh/uv/) —— 插件用 `uv run --directory <插件目录>` 启动
server，首次运行会自建环境，不需要自己 `pip install`。

Claude Code：

```text
/plugin marketplace add HymanLabX/endnotex9-mcp
/plugin install endnotex9@endnotex9
```

Codex：

```bash
codex plugin marketplace add HymanLabX/endnotex9-mcp
codex plugin add endnotex9@endnotex9
```

两者读的是同一棵树。Claude Code 取 `.claude-plugin/plugin.json`；Codex 优先取
`.codex-plugin/plugin.json`，取不到时回退到前者。两者展开的都是同一个
`${CLAUDE_PLUGIN_ROOT}`，所以一份 [`.mcp.json`](../.mcp.json) 同时服务两边。两份清单由
`tests/test_plugin_manifests.py` 保证不漂移。

**Codex 独有的一个坑：沙箱。** Codex 默认的 `workspace-write` 策略只允许写工作目录、并且禁网，
而你的 EndNote 库几乎一定在工作目录之外。只读没问题，但 `lit-cleaner` 的 CrossRef 查询需要网络，
任何 `--commit`（以及所有 `update_reference_fields`/`mark_references` 写入）需要库所在目录可写。
按需放开即可，不必直接上 `danger-full-access`：

```toml
# ~/.codex/config.toml
[sandbox_workspace_write]
network_access = true
writable_roots = ["D:\\Research"]   # 存放 .enl 及其 .Data 目录的文件夹
```

这不改变 server 自身的任何保证：写入仍然默认 dry run、仍然先整库备份、
仍然拒绝在 EndNote 打开库时运行。

随后执行一次初始化，让 server 知道要读哪个文献库：

```bash
uv run --directory <插件目录> endnotex9-mcp setup
```

`/plugin` 与 `codex plugin list` 都会显示插件的安装目录；如果直接使用源码检出，把检出根目录当作
插件目录，并把 marketplace 指向 `.`（`/plugin marketplace add .`、`codex plugin marketplace add .`）。

插件自带五个技能。Claude Code 中作为斜杠命令调用，Codex 中列为 `endnotex9:<名称>`：

| 技能 | 作用 |
|---|---|
| `/lit-cleaner` | 补全或纠正元数据、合并重复条目、清理孤儿 PDF 与旧备份。 |
| `/lit-downloader` | 为选中的记录下载、校验并原生附加全文 PDF（仅 Windows）。 |
| `/lit-classifier` | 画像、提出分类体系、编写受限规则、抽样审计，并生成 `Custom 1` 只读回写预览。 |
| `/lit-deep-reader` | 基于附件 PDF 按 triage/standard/deep 三档精读单篇文献，对照全文核验其结论，可选择把笔记写回该记录的 Notes 字段。 |
| `/lit-review-writer` | 针对库中的一个切片——某个分组、某个研究方向、某个课题组或某位研究者——撰写综述：每条论断都带上其来源记录号，并随文交付覆盖清单。 |

`lit-review-writer` 面对的是一个封闭语料：先把解析出的记录冻结成 `corpus.json`，按约定深度阅读，
再由 `cite_audit.py` 拒绝任何引用了该文件之外记录的草稿。它能看见的是这份收藏，而不是整个领域，
这一点也会写进综述本身。

`lit-deep-reader` 本身不写库：`notes_block.py` 只负责把本次笔记并入 Notes 字段中已有的内容，
并打印 `update_reference_fields` 的调用参数，真正的写入仍要经过该工具的「先 dry-run 再确认」这道闸。

`lit-downloader` 仅支持 Windows，依赖 `lit-downloader` 这个 extra（Playwright、PyMuPDF、
pywinauto），具体命令见其 SKILL.md；它驱动机器上已安装的 Microsoft Edge（`channel="msedge"`），
Playwright 不需要另外下载浏览器。

## 安装

面向 Claude Desktop 及其它 MCP 客户端（以及宁可手工注册 server 而不装插件的 Codex 用户），
直接安装 Python 包：

```bash
python -m venv .venv
python -m pip install -e ".[pdf]"
endnotex9-mcp setup
endnotex9-mcp index-pdfs
endnotex9-mcp status
```

如果不需要读取 PDF，可以省略 `[pdf]`。`setup` 接受 `.enl` 路径和可选 XML 导出，
并把配置及 SQLite 索引写入操作系统的应用配置目录。

自动化安装可直接传入路径；出现任一路径参数后，`setup` 不再询问：

```bash
endnotex9-mcp setup --library "D:\\Research\\My Library.enl"
endnotex9-mcp setup --xml "D:\\Research\\library.xml" --pdf-dir "D:\\Research\\My Library.Data\\PDF"
endnotex9-mcp doctor --json
```

还可以使用 `--db-path` 指定索引、`--skip-index` 只写配置、`--no-watch` 关闭后台刷新。
非交互模式会拒绝不存在的输入路径。`doctor` 是只读诊断，不创建或修改索引；失败时
返回非零退出码，`--json` 输出适合脚本处理。

## 连接 Codex

装插件时 server 已自动注册，`codex mcp list` 即可看到。若想手工注册：

```bash
codex mcp add endnotex9 -- endnotex9-mcp serve
codex mcp get endnotex9 --json
```

如果 Codex 找不到 `endnotex9-mcp`，请将命令替换为虚拟环境中可执行文件的绝对路径。
配置示例见 [`examples/codex-config.toml`](../examples/codex-config.toml)。

## 连接 Claude Desktop 或 Claude Code

```json
{
  "mcpServers": {
    "endnotex9": {
      "command": "endnotex9-mcp",
      "args": ["serve"]
    }
  }
}
```

配置示例见 [`examples/claude-mcp.json`](../examples/claude-mcp.json)。

## 使用示例

```text
检索我的文献库中与“弹热制冷”和“elastocaloric cooling”有关的论文。

读取《Achieving excellent superelasticity and extraordinary elastocaloric
effect in a directionally solidified Co-V-Ga alloy》第 1–3 页。

分析文献库并提出主题分类树，在我确认分类树后再评估分类规则。

将选中的文献按 GB/T 7714-2015 格式输出，并生成 BibTeX。
```

单篇工具可以直接使用任一种选择方式：

```text
按标题读取：{"title": "Achieving excellent superelasticity ..."}
按 DOI 读取：{"doi": "https://doi.org/10.xxxx/xxxxx"}
按内部编号读取：{"rec_number": 123}
```

批量引用和 BibTeX 工具还接受混合列表，例如：

```json
{"references": [{"title": "Paper A"}, {"doi": "10.xxxx/paper-b"}, {"rec_number": 123}]}
```

## 分类流程

分类不是一个不可解释的“一键分类”调用，而是包含人工确认点的流程：

1. 分析语料和抽样；
2. 冻结当前文献库指纹；
3. 保存并由用户批准分类树；
4. 保存并冻结声明式规则；
5. 评估分类、查看证据和未决文献；
6. 建立抽样审核集并提交人工标签；
7. 输出审核范围内的指标；
8. 生成只读写回差异预览。

规则只支持受限制的声明式条件，不执行任意 Python、SQL 或正则表达式。

## 隐私与 GitHub

不要提交真实 `.enl`、`.Data`、XML 导出、PDF、SQLite 索引或带本机路径的 MCP 配置。
仓库的 `.gitignore` 已排除这些内容，测试使用临时生成的合成数据。

附件路径在 URL 解码和符号链接解析后仍必须位于配置的 PDF 目录中。XML 解析禁用
DTD、外部实体和网络访问；含 DTD 或实体声明的导出会被拒绝。

## 开发

```bash
python -m pip install -e ".[pdf,dev]"
ruff check src tests
pytest
python -m build
```

贡献规则见 [CONTRIBUTING.md](../CONTRIBUTING.md)，数据完整性与安全说明见
[SECURITY.md](../SECURITY.md)。

## 当前限制

- 尚不支持扫描 PDF 的 OCR。
- 尚不支持向量/语义检索。
- EndNote 分组目前只读；`mark_references` 只写入 Custom 字段标记供用户自建智能分组，
  不会自动创建或编辑分组。
- 写入能力仅限 `update_reference_fields` 允许清单里的字段（卷号、期号、页码、文章
  编号、卷数、期刊、Notes、标题、摘要、作者、关键词）和 Custom 2–7；`custom_1` 没有
  提交通道，`year`（年份）因为是 MyISAM 索引键，需要真正的 B-tree 写入能力才能改，
  这次没做通，清单之外的任何字段都不可写，且完全不提供删除条目的能力。
- 写入 `author`/`journal`/`keywords` 不会同步更新 EndNote 的作者/期刊/关键词词表；
  如果在意自动补全里的旧词条，需要自己在 EndNote 里跑一次 Update List。
- 引用格式目前限于 APA 7th 和 GB/T 7714-2015。

项目采用 [MIT License](../LICENSE)。EndNote 是 Clarivate 的商标；本项目与 Clarivate
没有隶属或背书关系。
