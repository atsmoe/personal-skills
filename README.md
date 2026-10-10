# Personal Skills

我的个人 agent skills 集合。每个技能独立放在 `skills/<技能名>/`，以后新增技能沿用这个结构。

## 已收录

| 技能 | 版本 | 用途 |
|---|---|---|
| [照片转采购明细](skills/photo-purchase-ledger/SKILL.md) | 2.3.1 | 按日期识别收据及实物照片，增量更新月度采购 Excel，并核对或同步腾讯在线采购表。 |
| [剧组报销](skills/prepare-reimbursement-submission/SKILL.md) | 1.0.0 | 整理完整报销提交包，按需调用铁路客票附件整理和报销工作簿核验。 |

采购技能支持 Codex 和 agy/Gemini，保留在线协作者的增补，核对我们手里的采购是否已入表。在线修改、本地保存和格式各有独立验收要求。

剧组报销只有一个技能入口。铁路电子客票整理与工作簿核验放在该技能的参考文件和脚本中，按任务加载；不会为仅改名、转图的附件任务自动制作总报销表。客票脚本依赖 PyMuPDF，使用已核实元数据、300 DPI 渲染和来源哈希记录，拒绝覆盖已有输出。

## 获取与使用

```text
git clone https://github.com/atsmoe/personal-skills.git
```

选择所需的 `skills/<技能名>/` 文件夹，安装到当前工具已配置的技能目录。安装的是单个技能文件夹，不是仓库根目录。避免在多个扫描目录重复安装同名技能；更新后确认工具实际加载了新版本。

每个技能从 `SKILL.md` 开始阅读，只按当前任务加载它链接的参考资料。脚本的参数先查看 `--help`。采购技能现有三个脚本均只读，依赖 Python 标准库；它们不会自行完成在线写入或 Excel 公式重算。

采购技能保留了我的项目约定，包括正式表路径、在线主表和格式基准。用于其他项目时以用户指定的文件、日期、主表及格式要求为准。

## 目录

```text
skills/
  photo-purchase-ledger/
    SKILL.md
    agents/
    references/
    scripts/
  prepare-reimbursement-submission/
    SKILL.md
    agents/
    assets/
    references/
    scripts/
tests/
  test_reimbursement_railway.py
docs/
  ledger-low-consumption-plan.md
```

## 待实施计划

[采购表低消耗执行计划](docs/ledger-low-consumption-plan.md)记录固定执行脚本、工具回传限制和图片增量核对的后续工作。该文件是计划，尚未实施；当前发布的技能仍为 2.3.1。

仓库只收录技能与相关文档，不存放采购图片、业务表格、识别结果、访问凭据或个人任务日志。

开发验证使用合成 PDF，不使用真实票据：`python -m unittest discover -s tests -v`。测试需要 PyMuPDF；完整报销表的实际 Excel 打开验收仍在具体报销任务中执行。
