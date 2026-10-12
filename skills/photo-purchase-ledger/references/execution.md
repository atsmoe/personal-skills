# 分阶段执行与接续

> 规范版本：v2.5.0。日常新增一天或继续同一天只需本页及技能入口；异常再加载专项参考。

## 边界

三个原观察脚本和准备／匹配／验收状态逻辑仅用 Python 标准库。本地写入适配器需要 Windows、PowerShell 7、可用的 Excel COM；其他系统可使用观察和计划，不会假装完成编辑或重算。在线图片呈现额外需要 Pillow，腾讯读写通过当前已授权连接注入，不内置账号、令牌或 MCP 地址。

`ledger_run.py` 是统一入口。所有路径由参数或项目配置提供。只处理一个采购日期；其他日期、非标准汇总或对象不兼容时保留计划和现场。观察报告不等于识别记录；机器检查、人工／模型视觉验收、本地发布分别记录。

## 来源记录

原始图片按 CreationTime 排序。Windows 记录 `creation_timestamp`；其他设备用 `inventory.py` 生成的原设备清单，在识别文件顶层指定相对 `order_file`，并保留每张图的原时间与 SHA-256。同时间或重新配对须在采购组写 `order_note` 说明依据。程序会阻止缺失或变化的顺序证据进入写入。

识别 JSON 使用 `purchase-recognition/v1`。顶层 `groups` 每组包括：

- `id`：稳定且唯一的采购组标识，使用字母、数字、点、横线或下划线。
- `date`：当前表实际归属日期，`YYYY-MM-DD`；不自行覆盖用户手动归属。
- `sources`：按原顺序记录 `{id, path, role, sha256, creation_timestamp}`，path 相对显式来源根目录；role 为 `receipt` 或 `item`。receipt 从不入照片列。
- `items`：按票面顺序记录 `name, spec, quantity, unit, unit_price, amount, scene`。小数用字符串，未知用 `null`。
- 每行 `verification` 对七个字段分别填写 `verified`、`not_provided`、`unresolved`。`not_provided` 必须确实核对过来源且值为 null；提取失败用 unresolved，会阻止写入。
- 每行 `evidence: {source, description}` 指向已看过的来源和具体依据，保留原文、更正及空字段依据。程序不能代替看图，也不会把旧 OCR 自动升级为已核实。
- 可选 `receipt_total` 独立核对票面总额。数量乘单价与行金额有超过一分的差异，必须有 `settlement_note` 说明来源中的结算依据。
- 单组单张实物照默认覆盖整组；多张用 `photos: [{source, first_item, last_item}]`，明细索引从 0 开始，照片承载区域不重叠、不跨组；不得漏映射实物照。

表中已有相同内容，但无可信旧基线时，`verify_binding` 要求先核实是否同一采购。在该组记录 `binding: {workbook_sha256, rows, evidence}`，绑定当前文件、行号和核实依据。内容相同不足以自动认定重复；无法确认就保留疑点，不强行绑定。

## 本地固定流程

命令中的 `python` 指当前环境实际可用运行时，`<scripts>` 为当前安装技能的 scripts 目录；不要求加载源码。

```text
python <scripts>/ledger_run.py inspect --workbook <最新.xlsx> --recognition <识别.json> --source-root <来源根目录> --config <项目.json> --output <新的观察.json> [--baseline <上次context_after.json>]
python <scripts>/ledger_run.py prepare --workbook <最新.xlsx> --recognition <识别.json> --source-root <来源根目录> --config <项目.json> --run-dir <新的工作目录> [--baseline <上次context_after.json>]
python <scripts>/ledger_run.py apply-local --run-dir <工作目录>
python <scripts>/ledger_run.py verify-local --run-dir <工作目录>
python <scripts>/ledger_run.py accept-visual --run-dir <工作目录> --evidence <本地视觉记录.json>
python <scripts>/ledger_run.py finish --run-dir <工作目录>
python <scripts>/ledger_run.py publish-local --run-dir <工作目录>
```

prepare 只保存计划；apply-local 修改工作目录中的候选副本，重算、保存、正常模式重开并输出局部 PDF。每次失败重试从未变化的输入新建候选，绝不在半成品上重插行。只退出自己创建的编辑器，不关闭用户应用。输入发生变化就重新观察和准备。

verify-local 独立核对金额、历史内容、公式、样式、照片哈希与合并。Excel 图片采用保存后原生尺寸检查，避免把未校准的字符宽度估算当实际像素；`check_xlsx.py --check-drawings` 的字宽仍须按目标软件校准。

视觉记录使用 `purchase-ledger-visual/v1`：`target_id` 从 run.json 取，`workbook_sha256` 对应当前已检查文件，`reviewer` 标识实际验收者，`artifact` 为实际查看的预览或截图绝对路径，`artifact_sha256` 为其哈希，`checks` 中 `content, photo_binding, merges, row_heights, borders, preview` 必须逐项实际检查后才写 true。不是自动生成一组 true；不可用未看过的图片、旧截图或本地预览代替在线现场。

finish 在本地及已请求的在线验收均通过后写 `context_after.json` 和当前目标的 `success.json`。后者仍注明候选是否发布。publish-local 才备份并替换本次选定输入文件；源变化、文件占用或检查未完都停止发布。已有其他正式文件不能借此被隐式覆盖。

`status --run-dir ...` 只回传阶段与必要金额。中断后读取工作记录继续，不靠完整聊天历史；失败或部分成功不会推进成功基线。

## 腾讯连接的固定适配

只在已授权在线任务中运行；仅本地请求不调用这一段。

1. 用 `tencent_bridge.js` 的 `exportWorkbook` 调当前连接创建导出、轮询，再通过 `ledger_bridge.py download` 下载。临时签名 URL 只经宿主内存／标准输入传递；下载要求 HTTPS、明确选定的腾讯 COS 主机、无跳转、256 MiB 上限、新输出，并保存不含链接的导出回执。导出失败不能拿旧文件冒充最新状态。
2. 针对该最新导出 prepare。项目配置必须明确腾讯 file_id、sheet_id、字体、字号、平台行高、照片列实际像素宽 `online_photo_width_px`。不能把本地 pt 或估算列宽直接当平台值。
3. `online-plan --run-dir ...` 生成固定的插行、类型化数值、公式、完整样式块、尺寸、合并及图片操作。单元格图片 API 没有偏移参数，适配器将实物照放进与实际单元格同尺寸的白色呈现画布，等比居中留白；保留原始证据，另存来源与呈现图的双哈希。此图只用于表内展示，不能代替原图识别。
4. 宿主加载在线计划，在模型之外调用 `applyOperations`。`acquireWriter` 先调用 `ledger_bridge.py claim` 保存独占 token，`releaseWriter` 在所有远端调用结束后调用 release。refresh、checkpoint、readback、image、release 的请求均携带 `writer_token`；禁止在另一个写手仍运行时抢占或按时间自动解锁。宿主崩溃后，先确认原调用已终止，才可用保存的 token 显式 release，再重新 claim 和核对完整导出。`call(name,args)` 只路由当前腾讯连接；`refresh` 必须先重新导出并由 `ledger_bridge.py refresh` 校验，不能替换成空函数。图片分片通过 `loadImageParts` 拼接，完整数据不能进入模型。
5. 每次写入前局部核对、先落操作记录，再执行、立即读回。结构变更或插图结果不明时，通过最新完整导出证明已经完成或尚未发生，才跳过或重试。样式赋值可在内容和历史保留检查后重试。出现未知变化停止该区域，不回滚整表。
6. 写后重新导出：`verify-online --run-dir ... --export <新导出.xlsx> --receipt <新导出.receipt.json>`。机器通过后仍检查在线界面、照片合并和留白，单独 `accept-visual --target online`。连接不支持或现场看不到时保留未完成状态。

适配宿主只有少量固定回调；腾讯连接在各 agent 中的调用机制不同，不能假定 Python 能直接使用 Codex MCP。不要把整个 SDK 或全部工具定义交给模型。集成时保留 `tencent_bridge.js` 的完整导出与接续门槛，不绕过它只执行 payload。

无可信修订号时，写前刷新加读回仍无法排除最后瞬间的并发写入。该情况必须由实际读回和冲突处理控制；没有“总额相同所以没变化”的快捷路径。

## 输出与成本

常规摘要默认 2,000 字符，硬上限 5,000；完整报告留在辅助文件，异常最多默认 10 项，保留错误数、检查是否完整及省略信息。不能用截断或隐藏错误换取成功状态。宿主读原始 JSON、图片、导出或完整日志时不将其直接回传模型。

测量用相同输入和起始表，分别记工具回传体积、任务墙钟时间、重试、主会话及所有子任务用量。缓存包含关系须按客户端记录确认；缺失 token、费用和图片归因填“不可取得”，不填零，不把字符降幅当费用降幅。开发调试和日常执行成本分开。当前发布证据及限制见仓库 `docs/ledger-2.5.0-validation.md`。
