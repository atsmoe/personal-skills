# 腾讯接口与日期隔离

本参考依据当前连接暴露的工具 schema 编写。执行时重新发现相应工具并核对参数，不能假设设计稿中的 `document_version_v2`、`skill run` 命令存在。

## 定位范围

先用 `manage_query_file_info(file_id)` 核实 `type`：`sheet` 或 `smartsheet`。配置与实际不符时停写并修正配置，不调用另一种类型的写工具。元数据只用于定位，不把文档修改时间当条件版本令牌。

普通表格用 `sheet_get_sheet_info` 查当天子表的 ID、已用范围。SmartSheet 用 `smartsheet_list_tables`、`smartsheet_list_fields` 核实当天表及字段类型；使用当前选区时保留 view_id。没有当天表时，只有当前整理请求/项目配置明确采用每日子表结构，才创建当天表和业务字段；已有整月混排表不据此迁移。

当前项目如果是整月同一子表，须有用户确认的当天 `daily_ranges[ISO日期]`（sheet_id、start_row/end_row、允许的新增空白范围），或已验证的仅当天过滤视图。没有范围时，可转综合技能处理定位，但不能本技能偷偷加载整月。范围发生插删行后需重新定位，旧范围不是永久安全坐标。

月度汇总 `summary_locators[ISO日期]` 可配置：

- SmartSheet：`record_id`；首次可使用专用 `view_id`，先通过 `list_views` 核实其过滤规则严格限定当天，并确认当前接口确实在服务端应用视图过滤（仅说明view_id控制排序不能证明筛选生效），再分页 `list_records(view_id=...)`。返回每条再校验日期，不可传工具并不支持的 filter 参数。没有记录 ID 或已确认可用过滤视图时停在“汇总定位缺失”，不要读整月来找。
- SHEET：`row_index`（0-based）及列映射，单独读这一行并核对日期。新日期须有已验证空白预留行/固定日历行，写入前同时确认日期与业务格空白；不得占用他人行。有旧记录但行号失效时先取得仅当天的新定位。

只读/写当天。定位元数据允许跨表列名，不允许加载历史行数据。月累计沿用现有公式，不重建整月汇总。创建整月文件或调整跨日结构属于额外任务。

## SHEET

- `sheet_get_cell_data` 必须传 start/end row/col，`return_csv=false, include_formula=true`；不省略范围默认全表。数字用 number_value，文本用 string_value，公式仅在明确需要时使用 FORMULA。
- `sheet_set_range_value` 的坐标 0-based，只发送本次需要改的单元格。补入前确认目标格与合并关系，不把旧数组覆盖现表。不因省略字段调用清空或整体样式替换。
- 普通表格行号会移动；保存行定位加实际业务快照，写前校验。多行共享图片时，仅在本组已确认空白且无冲突区域 `sheet_merge_cell`；边框、尺寸沿实际模板，不能复制 SmartSheet 行为。
- `sheet_insert_image` 可以传 image_id 或实际原图 Base64（content），二选一。`sheet_get_cell_data` 不返回图像或真实样式：文字读回不能证明图片、合并、边框成功。合并用 `sheet_get_merged_cells` 的当前参数核验；图片通过当前界面/可用原生读取检查。能力缺失时保留 partial。

## SmartSheet

- 明细通过 `smartsheet_list_records` 分页读当天表，limit 最大100；跟随 has_more/next，不能把第一页当全表。需要计算列时开启 include_computed_values 并限制 field_titles。
- `smartsheet_add_records` 返回 record_id，保存每条 ID；不把它当行号。`smartsheet_update_records` 只改授权字段。没有条件写入参数时，写前比较最新内容，写后对 record_ids 读回。
- text_value 必须是 `{"items":[{"text":"水泥","type":"text"}]}`；number_value 是数字；选项 option_value 为 `{"items":[{"text":"完成"}]}`；dateTime 用该字段所需的毫秒时间戳（Asia/Shanghai 当地零点）。按实际字段类型选择，不把所有日期当文本。
- 图片 `image_value={"items":[{"image_id":"..."}]}`。同组明细可引用同一个 image_id，一张原图只上传一次。SmartSheet 没有普通单元格合并语义，不调用 SHEET 合并接口。现有照片字段不整体替换；先读回并保留已有 items，再按授权补充。
- 不直接调用供 MCP App 内部使用的 fetch、get_client_var、commit_changeset。

## 上传、重试与并发

`upload_image` 要实际图片 Base64，不能传路径/URL；当前单张不超过10MB，返回 image_id 有一天有效期。Base64 在运行环境内读文件并直接传工具，不输出到聊天。不能因接口要求 Base64 就把编码写入业务照片格。原图超过限额时报未完成，不未经用户要求替换成 OCR 图。

状态中按 SHA-256 保存 `uploads[sha256]={uploaded_at, expires_at, image_id}`，cache-image 写缓存，image-cache 查过期与关联状态。过期且尚未关联时重传；已核验嵌入来源保存在组的 image_sha256，补新图时保留旧图及已有 image_id。SmartSheet 和使用 image_id 插图的 SHEET 都在 prepare 检查有效期；SHEET 也可直接传原图 content，此时记录 original_sha256 而不冒充上传缓存。上传成功、关联失败只补关联；文字成功、图失败只补图。上传响应丢失时先查当天照片字段/界面，无法确定的普通插图不得自动再插一遍。

prepare 强制使用最新目标读回：current 的值、日期、目标、结构均须符合写前快照，且此前已核实的当天采购、照片、实际汇总和结构不得被改。证据超60秒须重新获取；必须先持久化 prepared 才发送变更。summary 的 resolve 同时比较四个字段及写后当天明细指纹，依据变动不报成功。

本地状态锁仅防同一状态目录的并行执行；另一台机器和线上协作者不受此锁控制。持久化 prepared 意图后发调用，进程崩溃视为可能已执行。若接口有实际条件版本写入能力，可使用；当前通用 SHEET/SmartSheet 写接口不承诺 compare-and-swap。读前比较加写后核验不能消除检查与写入之间的竞态，不称为原子版本锁。文档级版本变动只是提示，必须比较本次目标值和结构，不能因别人改了其他日期就全量重做。

恢复先读当天目标：期望值唯一且确已生效则 verified；精确写前值仍在且能证明该操作没有生效才 not_applied；部分生效、身份不唯一、图片证据不足则 uncertain/conflict。对 absent 状态的新增应核对当天全范围并排除同笔采购被移到另一位置，不能只看原插入位置为空就重试。不得自动撤销协作者修正或复活其删除记录。

只读网络请求可限次重试（如2次）。变更请求一旦发送，先核对效果；not_applied 后最多一次自动重试，再失败留待接续。每批立即读回，此验收优先于节省读调用。
