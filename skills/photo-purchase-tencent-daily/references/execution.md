# 本地入口与状态

脚本只做当天扫描、识别结果校验和操作日志，不做 OCR，也不直接联网。Codex 看原图、调用当前连接的腾讯工具、规范化读回证据后，将结果交给脚本。真实采购写入应严格执行 SKILL.md 和腾讯参考中的定位/并发检查。

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
  "scope_checked": true
}
```

before/expected 不同才能 prepare。scope_checked 由执行者在核实范围、来源、定位、写前快照后设置；这是执行者的证据声明，脚本不能独立证明在线授权/身份。输出 operation_id，持久化后才调用腾讯工具。同一日期存在未决操作时，不允许新操作或导入识别结果。

summary 的 expected 必须包括 date、item_count、daily_amount、status；同时提交 `daily_snapshot={"date":"YYYY-MM-DD","items":[当天全部规范化明细]}`，脚本验证数量和金额包含协作者，并检查已核实采购没有遗漏。只改配置所映射字段，不带用户备注。structure 的 expected 放明确当天表结构/合并关系，不能包含跨日重排。

## 4. resolve --input <当天readback.json>

发送请求后读回，再导入：

```json
{
  "operation_id": "prepare输出的ID",
  "target": {"file_id":"同一文档","sheet_id":"同一子表"},
  "observed": {"items": [{"name":"水泥","spec":"32.5等级","quantity":"10","unit":"袋","unit_price":"25.5","amount":"255.00","note":""}]},
  "locators": ["SmartSheet的record_id或SHEET实际行号"],
  "evidence": "当前工具读回/图片界面核验的证据路径或标识"
}
```

规范化值的精度/空值一致。只有 observed 精确匹配 expected 才 verified；与 before匹配仍默认 uncertain，只有 absence_verified: true（执行者已核查当天完整目标、确认没有其他位置生效）才能 not_applied。其他结果 uncertain，不盲重试。verified 的组映射保存 locators，summary 保存实际当天金额。resolve 不发在线调用；查看/核对请求也不调用 prepare。

SmartSheet image expected/observed 为 `{"image_ids":[实际ID]}`；普通表格为 `{"visual_sha256":[本组实物图哈希]}`，只能在看当前界面原图与组对应后填写视觉证据。每条明细都提供真实定位，图片定位必须与已核实文字一致。未取得证据不 resolve 成功。upload缓存可 cache-image --input，输入 sha256、image_id、uploaded_at、expires_at（带时区 ISO），本地 metadata只保存值，不保存 Base64。

## 认领已有记录：adopt --input

新状态、状态丢失或协作者已补入时，先看原图并核对当天真实记录的唯一采购身份。不要为建立状态再发新增。输入同 prepare 的 kind/group_id/target/expected/scope_checked，再加 observed、locators、evidence，且 observed 必须与 expected 完全相等。adopt只持久化当前核验，不发写入。已嵌入图片的上传 ID 即使过期，也可在核验真实照片与来源对应后认领，不要求再上传。

## 5. status

汇总仅当天未处理来源、每组文字/图片进度、来源及金额问题、未决操作和汇总状态。不输出状态全文。completed要求所有当前图片均已纳入已审查组、没有来源/金额问题、每组文字图片验证、当天汇总verified且“完成”。新增来源/新组会使旧汇总失效，resume先scan，不能一直复用旧completed。

状态文件用同目录临时文件+fsync+atomic replace，每次本地变更持有锁；未决操作日志在外部调用前落盘，挡住另一进程重复提交。锁不覆盖另一台设备或线上协作者。state.json 丢失时重新核对当天表，不从内容相同就默认已经写入，也不清表重做。
