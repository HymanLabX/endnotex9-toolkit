# endnotex9-toolkit 中文说明

`endnotex9-toolkit` 是一个本地优先的 EndNote X9 MCP 服务。Codex、Claude 和其他支持
MCP stdio 的客户端可以通过它检索文献、读取现有分组、设计可审计分类、生成引用并
读取 PDF 附件。EndNote 文献库不需要上传到第三方服务。

[English README](../README.md)

0.3.0 的文献库固定选择、预览提交和恢复机制变动见[升级说明](../CHANGELOG.md#030--2026-11-02)。

> [!NOTE]
> 0.2.0 起由 `endnotex9-mcp` 更名而来。旧命令仍然可用，之前 `setup` 写下的配置也会在原位置被找到。

> [!IMPORTANT]
> 直接读取功能以 EndNote X9 为测试目标。EndNote 的底层表格式没有公开文档；使用
> 更高版本时，请先运行 `endnotex9-toolkit crosscheck`。绝大多数 MCP 工具是只读的。
> 只有 `update_reference_fields` 和 `mark_references` 两个工具能写入文献库，且仅限于
> 已验证安全的一小类字段（卷号/期号/页码等书目字段、期刊、Notes、标题、摘要、DOI、
> 作者列表、关键词列表，或未使用的 Custom 2–7 字段）。两者默认只预览不写入（`dry_run=True`），真正
> 写入前会先备份文献库元数据。详见下方“安全边界”。

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

普通读取不会修改 EndNote 源文件；索引、分类状态和 PDF 文本保存在独立的本地数据库。
Custom 1 分类写回目前只提供预览。只有 `update_reference_fields` 和 `mark_references`
两个 MCP 工具支持受限字段提交，默认 `dry_run=True`；`year` 和记录删除始终未开放。

实际写回前备份 `.enl` 与完整 `rdb`，备份不包含 PDF 附件。工具使用字段白名单、全记录
验证、MyISAM 空闲空间检查、跨进程互斥和写前恢复镜像。普通提交异常会回滚，进程中断则
保留恢复目录并阻止继续写入，恢复命令见下方“文献库隔离与写回恢复”。

**所有提交前都须关闭 EndNote。** 工具之间的锁不能约束 EndNote 本身。首次在实际库使用
变更前，仍应在一次性副本中确认库可以正常打开、排序和检索。作者、期刊和关键词写回后，
EndNote 自己的术语自动补全列表可能需要手动运行 Update List。

格式考证和历史故障依据见 [SECURITY.md](../SECURITY.md) 与
[写回格式说明](writeback-format-notes.md)。

## 作为插件安装（Claude Code 或 Codex）

本仓库同时是一个 agent 插件：一个 MCP server 加五个技能。两个 agent 都以此为最短路径，
只需要 PATH 上有 [uv](https://docs.astral.sh/uv/) —— 插件用 `uv run --directory <插件目录>` 启动
server，首次运行会自建环境，不需要自己 `pip install`。

Claude Code：

```text
/plugin marketplace add HymanLabX/endnotex9-toolkit
/plugin install endnotex9@endnotex9
```

Codex：

```bash
codex plugin marketplace add HymanLabX/endnotex9-toolkit
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
uv run --directory <插件目录> --extra pdf endnotex9-toolkit setup
```

加上 `--extra pdf` 是为了与插件启动 server 所用的环境一致，这样初始化会顺带把环境建好。首次启动
慢就慢在建环境：在 uv 缓存为空的机器上可能超过 30 秒，而这正是 Claude Code 等待 MCP server
连接的上限。如果刚装完插件就看到 `endnotex9` 连接失败，先执行上面的初始化，再重连它（Claude Code
里用 `/mcp`）或新开一个会话。初始化之前 server 也能启动，只是每个工具都会回复这条初始化命令，
而不是返回数据。

`/plugin` 与 `codex plugin list` 都会显示插件的安装目录；如果直接使用源码检出，把检出根目录当作
插件目录，并把 marketplace 指向 `.`（`/plugin marketplace add .`、`codex plugin marketplace add .`）。

插件自带五个技能。Claude Code 中作为斜杠命令调用，Codex 中列为 `endnotex9:<名称>`：

| 技能 | 作用 |
|---|---|
| `/lit-cleaner` | 补全或纠正元数据、合并重复条目、清理孤儿 PDF 与旧备份。 |
| `/lit-downloader` | 为选中的记录下载、校验并原生附加全文 PDF；或者把一次 Web of Science 检索做成新的文献集，生成 PDF 已放进库内的 EndNote 导入文件（仅 Windows）。 |
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

它的「新文献集」模式（`scripts/collection/`）负责从检索导出到导入的全过程：按课题规则合并、分档、
精简，先下载开放获取版本，再在用户自己的 Chrome 里逐篇打开出版商链接（ScienceDirect 每天最多 100 篇，
任何网站一出现验证页就停），最后生成 RIS 文件，其中的 `internal-pdf://` 链接 EndNote 导入时会原样保留。
整个过程不写文献库的数据库，但 PDF 会预先复制进 `<文献库>.Data/PDF` 附件目录；导入由用户自己操作。

## 安装

面向 Claude Desktop 及其它 MCP 客户端（以及宁可手工注册 server 而不装插件的 Codex 用户），
直接安装 Python 包：

```bash
python -m venv .venv
python -m pip install -e ".[pdf]"
endnotex9-toolkit setup
endnotex9-toolkit index-pdfs
endnotex9-toolkit status
```

如果不需要读取 PDF，可以省略 `[pdf]`。`setup` 接受 `.enl` 路径和可选 XML 导出，
并把配置及 SQLite 索引写入操作系统的应用配置目录。

自动化安装可直接传入路径；出现任一路径参数后，`setup` 不再询问：

```bash
endnotex9-toolkit setup --library "D:\\Research\\My Library.enl"
endnotex9-toolkit setup --xml "D:\\Research\\library.xml" --pdf-dir "D:\\Research\\My Library.Data\\PDF"
endnotex9-toolkit doctor --json
```

还可以使用 `--db-path` 指定索引、`--skip-index` 只写配置、`--no-watch` 关闭后台刷新。
非交互模式会拒绝不存在的输入路径。`doctor` 是只读诊断，不创建或修改索引；失败时
返回非零退出码，`--json` 输出适合脚本处理。

## 文献库隔离与写回恢复

文献库现在由 `setup` 明确选定并固定；在 EndNote 中打开其他库不会自动改写库、XML、PDF
或索引路径。切换时运行 `setup --library "其他库.enl"`，或在 Windows 下显式运行
`setup --use-open-library`，然后重启 MCP 服务。新配置按库分配独立索引，已有配置的索引路径
保留；发现索引属于其他数据源时拒绝覆盖，以保留已审核的分类会话。
旧索引若缺少数据源标识，可运行 `endnotex9-toolkit index --adopt-source`；只有每条记录的
源内容指纹完全一致才会绑定。若源已变化，请指定新的 `--db-path`，旧索引和审核状态会保留。

写回采用跨进程互斥、写前镜像和失败回滚。预览返回 `source_fingerprint`，提交时应通过
`expected_fingerprint` 传回；库在审核后发生变化会拒绝提交。清理脚本直接应用保存的预览，
不在提交时重新查询 CrossRef 或扩大清理候选范围。所有提交前仍须关闭 EndNote。

进程中断后会保留 `<Library>.enl.writeback-recovery`，阻止继续写回或建立源索引。关闭
EndNote 后运行：

```bash
endnotex9-toolkit recover-writeback --library "D:\Research\My Library.enl"
endnotex9-toolkit index
```

恢复会验证快照和现有文件，拒绝覆盖事务之外的修改。若失败，请保留恢复目录和备份。
此机制处理进程中断，不保证抵御所有断电和磁盘故障。

备份清理只处理可确认属于该库的完整备份，按创建时间保留；无关目录、未完成备份和无法
确认归属的旧备份会保留。孤儿 PDF 使用与全文索引相同的路径解析规则，并保留相对路径隔离。

清理报告与缓存位于用户应用数据目录的 `libraries/<库标识>/lit-cleaner`，脚本会打印
`report_dir` 绝对路径，支持 `--output-dir`。插件安装目录不再保存这些运行产物。
插件与 CI 使用已提交的 `uv.lock`，启动时使用 `uv run --locked`。

## 连接 Codex

装插件时 server 已自动注册，`codex mcp list` 即可看到。若想手工注册：

```bash
codex mcp add endnotex9 -- endnotex9-toolkit serve
codex mcp get endnotex9 --json
```

如果 Codex 找不到 `endnotex9-toolkit`，请将命令替换为虚拟环境中可执行文件的绝对路径。
配置示例见 [`examples/codex-config.toml`](../examples/codex-config.toml)。

## 连接 Claude Desktop 或 Claude Code

```json
{
  "mcpServers": {
    "endnotex9": {
      "command": "endnotex9-toolkit",
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
  编号、卷数、期刊、Notes、标题、摘要、DOI、作者、关键词）和 Custom 2–7；`custom_1` 没有
  提交通道，`year`（年份）因为是 MyISAM 索引键，需要真正的 B-tree 写入能力才能改，
  这次没做通，清单之外的任何字段都不可写，且完全不提供删除条目的能力。
- 写入 `author`/`journal`/`keywords` 不会同步更新 EndNote 的作者/期刊/关键词词表；
  如果在意自动补全里的旧词条，需要自己在 EndNote 里跑一次 Update List。
- 引用格式目前限于 APA 7th 和 GB/T 7714-2015。

项目采用 [MIT License](../LICENSE)。EndNote 是 Clarivate 的商标；本项目与 Clarivate
没有隶属或背书关系。
