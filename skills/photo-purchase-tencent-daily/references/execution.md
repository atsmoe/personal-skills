# 本地入口与状态

脚本只做当天扫描、识别结果校验、类型适配和操作日志，不做 OCR，也不直接联网。Codex 看原图、调用当前连接的腾讯工具、规范化读回证据后，将结果交给脚本。prepare 接收真实读回证据并强制比较，不会自行生成“已读当前表”的证据。嵌入式执行器也可调用 `prepare(project, state, operation, read_current=callback)`，在函数内经 callback 读取连接器。真实采购写入应严格执行 SKILL.md 和腾讯参考中的定位/并发检查。

入口：`python3 <技能目录>/scripts/daily.py <command> --project <业务根目录> [--date YYYY-MM-DD]`。没有 `skill init/run` 这种假设的系统命令。

配置从业务根目录 `.purchase-tencent-daily.json` 读取；路径相对此根目录。日期缺省按 timezone 的当天，用户给的 MMDD 由模型先明确成年月日。配置没有该月份则停，不回退。photos_directory 支持 `{date}` 和 `{mmdd}`，工作目录必须不同于来源日期目录；扫描不递归、不访问 symlink 指向的其他日期。状态位于 `<work_directory>/<目标指纹>/<YYYY-MM-DD>/state.json`，避免跨年、跨文档覆盖。

## 1. scan

扫描当前日期，流式哈希，按 SHA-256 缓存 EXIF/可靠 birth time。Linux ctime/mtime不作原始顺序。可 `--order-file <当天原设备清单>`，格式兼容 inventory 的 `images`（sha256、creation_timestamp），或人工确认顺序的 `order`（sha256、timestamp）。未知时间仍扫描成功并标出，不阻断可凭内容识别的照片。

重命名/复制相同字节只记别名，不重复识图；新增的 hash 是待审查来源。已有采购来源消失/字节变化则标识 source_missing 并暂停该组，不删除线上采购。改图应经重新看图确认后显式调整来源映射。缓存只是避免重复解析，不证明看图核实。

## 2. identify --input <当天groups.json>

将已看原图的结果导入。一次可以只导入新增组；旧组保持，已写组的业务字段不能无授权改写。输入：

```json
{
  "groups": [{
    "receipt_sha256": ["64位真实SHA256"],
    "physical_sha256": ["64位真实SHA256"],
    "viewed_sha256": ["本组所有已看原图的SHA256"],
    "pairing_basis": "票面商品与实物包装对应",
    "receipt_total": "255.00",
    "settlement_note": "",
    "items": [{
      "name": "水泥", "spec": "32.5等级", "quantity": "10",
      "unit": "袋", "unit_price": "25.5", "amount": "255.00", "note": ""
    }]
  }]
}
```

数量/单价可 null；金额最多两位小数，不改单价凑总额。未识清的关键字段和未解释的差额存为 issues，该组不能自动写入或报完成。settlement_note 只接受有票面依据的尾差说明，不能用空泛备注掩盖差错。receipt_total为空时不能声称票面核验完成。查看 status 获取组 ID；脚本输出有上限。

## 3. prepare --input <当天operation.json>

在任何在线变更前记录意图。kind 为 text/image/summary/structure，group_id 在 text/image 必填；target 指当前 file_id、实际 sheet_id。把写前快照和期望快照转换为业务语义的 JSON，不直接塞入整个原始响应。

```json
{
  "kind": "text",
  "group_id": "scan/identify得到的g_...",
  "target": {"file_id": "当前文档", "sheet_id": "已核实当天子表"},
  "before": {"items": []},
  "expected": {"items": [{"name":"水泥","spec":"32.5等级","quantity":"10","unit":"袋","unit_price":"25.5","amount":"255.00","note":""}]},
  "before_structure": {"date":"2026-10-12","fields":{"金额":{"id":"真实字段ID","type":"currency"}},"merged_cells":[]},
  "current": {
    "date":"2026-10-12", "target":{"file_id":"当前文档","sheet_id":"已核实当天子表"},
    "captured_at":"真实读回完成时间，带时区ISO格式", "evidence":"当前MCP读回证据标识",
    "observed":{"items":[]},
    "structure":{"date":"2026-10-12","fields":{"金额":{"id":"真实字段ID","type":"currency"}},"merged_cells":[]},
    "groups":{}
  },
  "scope_checked": true
}
```

示例中的日期、类型、字段 ID 和时间必须替换为当前目标的真实值，并包括所有相关字段、当天边界和受影响区域的合并关系。结构快照在同一 target 下使用一致格式，不包含易变的已用行数或其他日期业务数据。before/expected 不同才能 prepare；current.target/date 必须精确匹配，证据不超过60秒且带时区，observed 必须等于 before，structure 必须等于 before_structure 及此前已核实的结构。否则 ConflictError（CLI输出 conflict）或证据不足而 blocked，均不保存 prepared。

`current.groups` 第一次没有已核实组时为 {}；之后必须包含当天每个文字已核实组：`{"g_真实ID":{"target":组的实际目标,"identity_verified":true,"locators":[真实定位],"text":{"items":[当前规范化明细]},"image":上次核实图片的当前快照,"structure":该目标的当前结构}}`。image/structure 在已有对应快照时必填；不能把本地旧值直接当 current。程序比较 text_snapshot/image_snapshot，SmartSheet record_id 不允许换成同内容的新记录；SHEET 只有采购身份与上下文核实后才接受移动后的行号。

scope_checked 由执行者核实范围、来源、定位后设置；这些证据声明不能由脚本独立证明在线授权/身份。输出 operation_id，持久化后才调用腾讯工具；若发送前间隔超过60秒，重新读回并核查条件，不能延迟发送旧意图。同一日期存在未决操作时，不允许新操作或导入识别结果。观察认领必须用 adopt，不能通过 observation_only 绕过 prepare。

summary 的 target 还须含真实 record_id（SmartSheet）或 row_index（SHEET），before/expected 只含 date、item_count、daily_amount、status。expected 金额采用两位小数字符串；同时提交 `daily_snapshot={"date":"YYYY-MM-DD","items":[当天全部规范化明细],"captured_at":"真实ISO时间","evidence":"当天全范围读回标识"}`，脚本验证数量和金额包含协作者，并检查已核实采购没有遗漏，保存包含重复明细的指纹。上次实际汇总 data/target 在 stale 时仍保留，作为写前冲突基线。只改配置所映射字段，不带用户备注。structure 的 expected 使用变更后的完整同格式结构，不能包含跨日重排。

## 4. resolve --input <当天readback.json>

发送请求后读回，再导入：

```json
{
  "operation_id": "prepare输出的ID",
  "target": {"file_id":"同一文档","sheet_id":"同一子表"},
  "observed": {"items": [{"name":"水泥","spec":"32.5等级","quantity":"10","unit":"袋","unit_price":"25.5","amount":"255.00","note":""}]},
  "locators": ["SmartSheet的record_id或SHEET实际行号"],
  "captured_at": "真实读回完成时间，带时区ISO格式",
  "structure": {"date":"2026-10-12","fields":{"金额":{"id":"真实字段ID","type":"currency"}},"merged_cells":[]},
  "evidence": "当前工具读回/图片界面核验的证据路径或标识"
}
```

规范化值的精度/空值一致。写后证据必须晚于 prepare 并在60秒内；只有 observed 精确匹配 expected 且结构符合预期才 verified；与 before匹配仍默认 uncertain，只有 absence_verified: true（执行者已核查当天完整目标、确认没有其他位置生效）才能 not_applied。其他结果 uncertain，不盲重试。verified 的组映射保存 locators，summary 保存实际当天金额。resolve 不发在线调用；查看/核对请求也不调用 prepare。

汇总读回另外必须带写后重新读取的 daily_snapshot（同写前格式）。四个汇总字段完全相同但当天明细指纹变了，仍 uncertain；只有排序变化可通过。月度汇总的其他日期不读、不比较、不写，不能宣称检测了其他日期的修改。这样他人改其他日期不会冲突，也不会被覆盖；同一天的目标值与计算依据变化会阻断验收。

SmartSheet image expected/observed 为 `{"image_ids":[实际ID]}`；expected 保留 before 的已有图片 ID，仅追加对应新来源的有效上传 ID。普通表格为 `{"visual_sha256":[本组实物图哈希]}`，只能在看当前界面原图与组对应后填写视觉证据；使用缓存插图时另交 `upload_ids={"新增实物SHA256":"image_id"}`，直接插原图则交 `original_sha256=[新增实物SHA256]`，二选一。每条明细都提供真实定位，图片定位必须与已核实文字一致。未取得证据不 resolve 成功。

图片缓存已存在于 `state.uploads[SHA256]={"image_id":"...","uploaded_at":"带时区ISO","expires_at":"带时区ISO"}`，不另建重复的 image_cache。cache-image --input 保存真实上传返回值；当前接口有效期一天，uploaded_at 取上传发起时间，expires_at 不得超过其后24小时。image-cache 查询 usable、associated、expired/missing，默认最多20个，可用 `--input` 传 `{"sha256":[最多20个当天实物哈希]}` 精确查询。组的 image_sha256 记录已经核验嵌入的来源；该来源上传过期也不重传，新增来源单独刷新。缓存不保存 Base64。

## 类型适配：adapt --input

`scripts/tencent_adapter.py` 的 TencentAdapter 是无网络调用的适配层；daily 导入并使用。输入 `{"field_type":"money","value":"255.00"}` 可输出当前表类型的载荷。SmartSheet text 使用 text_value.items/type，currency/number 用 number_value，dateTime 转当地零点的毫秒字符串，select 用 option_value，image 用 image_value；SHEET 文本/数字额外带 value_type，图片走独立插图工具。空值输出 null，表示省略，不清空用户已有格。Decimal 在业务计算和状态中保留为精确字符串，仅输出连接器数字时转 float；无法往返保持精度则拒绝。

普通表格合并可用 `{"kind":"merge","rows":[2,3],"column":7,"allowed_range":{"start_row":2,"end_row":5,"start_col":0,"end_col":7},"current_merges":[],"cells_blank":true}` 生成范围。rows 必须来自同一已核实采购组；current_merges 用 sheet_get_merged_cells 实际响应转换为0-based范围。适配层拒绝跨当天范围、不连续行、非空区域和重叠合并；已存在完全相同合并时输出 null，不重复请求。adapt 仅生成载荷，不写表或状态；真正合并仍走 structure 的 prepare/腾讯调用/resolve。

## 认领已有记录：adopt --input

新状态、状态丢失或协作者已补入时，先看原图并核对当天真实记录的唯一采购身份。不要为建立状态再发新增。输入同 prepare 的 kind/group_id/target/expected/scope_checked，再加 observed、locators、evidence，且 observed 必须与 expected 完全相等。adopt只持久化当前核验，不发写入。已嵌入图片的上传 ID 即使过期，也可在核验真实照片与来源对应后认领，不要求再上传。

## 5. status

汇总仅当天未处理来源、每组文字/图片进度、来源及金额问题、未决操作和汇总状态。不输出状态全文。completed要求所有当前图片均已纳入已审查组、没有来源/金额问题、每组文字图片验证、当天汇总verified且“完成”。新增来源/新组会使旧汇总失效，resume先scan，不能一直复用旧completed。

状态文件用同目录临时文件+fsync+atomic replace，每次本地变更持有锁；未决操作日志在外部调用前落盘，挡住另一进程重复提交。锁不覆盖另一台设备或线上协作者。state.json 丢失时重新核对当天表，不从内容相同就默认已经写入，也不清表重做。

## 从1.0.0接续

state schema 和 uploads 字段不改名；既有组、来源、操作与实际汇总仍保留。已核实但汇总 stale 的旧状态，如旧版已丢弃汇总 data，load 从最后一笔 verified summary 的实际 observed 恢复冲突基线，再与当前当天记录比较。旧 prepared/uncertain 操作没有结构基线或汇总指纹时，先完整核实其当天目标、采购身份、结构和汇总依据，resolve 才可附 `legacy_readback_reviewed:true`；仍须真实匹配 expected、提供新鲜写后证据与当天明细，否则继续 uncertain。此声明只处理确实缺少旧基线的操作，不能跳过1.1.0的检查，也不允许重新新增。新操作必须使用本版完整写前检查。
