---
name: prepare-reimbursement-submission
metadata:
  version: '1.0.0'
description: 整理、修改或验收中文剧组报销提交包；也处理其中的铁路电子客票 PDF 批量改名、转图、打包和报销工作簿核验。用于总报销 Excel、人员材料文件夹、发票及付款截图整理，或铁路客票附件处理；普通记账和无附件的简单金额计算不适用。
---

# 剧组报销

以完整报销提交包为主流程，铁路客票整理和工作簿核验是按需使用的子流程。采用当前环境可用的 Excel、PDF 和本地执行工具，不要求为单个子任务重做整套报销材料。

## 按任务加载

| 本次任务 | 读取与执行 |
|---|---|
| 仅整理铁路电子客票、按票面改名或转图打包 | [railway-tickets.md](references/railway-tickets.md)；使用 [process_railway_tickets.py](scripts/process_railway_tickets.py)。不自动创建或修改报销总表。 |
| 制作、修改或整理完整报销提交包 | 完整读取 [submission-standard.md](references/submission-standard.md) 与 [acceptance-checklist.md](references/acceptance-checklist.md)；含 PDF 证据时同时读取 [evidence-provenance.md](references/evidence-provenance.md)。 |
| 只核验现有报销 Excel、票据匹配或图片 | [workbook-validation.md](references/workbook-validation.md)，再读取验收清单中的相关项目；涉及证据图片时读取来源规则。不因检查请求重建或覆盖交付包。 |

## 共同约定

- 使用用户指定模板和人员范围，金额、日期、路线、付款与分摊有据可查；未知项保持“待确认”，不猜测、不拿相似票据替代。
- 原始 PDF 和图片只读。票据改名通过复制完成；PDF 内容不重建。PDF 页面以本地确定性工具按 300 DPI、RGB、整页逐页渲染，不使用阅读器截图、生成式修复或二次美化。OCR/视觉只用于识别元数据。
- 工作记录、转换页和预览放候选/返工目录；原件不改、不删，已有输出不自动覆盖。用户已明确授权正式整理时，完成候选验收后按该授权执行，不重复索取相同确认。
- 正式报销包是一份总 Excel、一份原始证据材料 ZIP 及对应人员/共同费用文件夹。铁路客票整理输出是附件处理结果；只有完成正式报销包的金额、模板、媒体追溯和打开检查后，才能称为完整报销交付。
- 正式 Excel 图片默认采用嵌入式 DrawingML 图片，随单元格移动缩放，禁用压缩、外链和裁切；单元格 rich-data 图片仅在接收环境明确支持且交付副本已验证时使用。
- 程序计算、哈希、结构检查和完整报告保留在文件中，模型只接收必要摘要、差异与异常。金额与验收结论使用同一份当前副本；未完成检查明确说明。

## 脚本与验收边界

- [render_pdf_evidence.py](scripts/render_pdf_evidence.py)：确定性 PDF 转 PNG 与来源清单；依赖 PyMuPDF，不识别票面字段，也不验证 Excel。
- [process_railway_tickets.py](scripts/process_railway_tickets.py)：依据已核实元数据复制改名、复用上述渲染器、生成报告及可选附件 ZIP；不做 OCR、不推断路线、不计算报销标准。
- 脚本成功不代替票面视觉核对、人员匹配或 Excel 实际重开检查。完整报销包按验收清单逐项确认，不能只凭 ZIP 可解析或公式缓存正确宣告完成。
