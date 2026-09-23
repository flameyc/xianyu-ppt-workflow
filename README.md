# PPT 文件处理工具

本仓库只公开通用的本地 PPT 文件处理代码，不包含客户资料、课程材料、私有运营规则、真实订单或案例。

## 工具

- `tools/inventory_templates.py`：仅扫描本地模板文件元数据，输出 SQLite 清单与统计。
- `tools/search_templates.py`：按文件名和路径查询元数据清单；结果不代表审美质量或使用许可。
- `tools/curated_templates.py`：在本地保存人工核验的页面索引，分开记录预览、许可和反馈。
- `tools/check_delivery_notes.py`：检查 PPTX 的文字备注与批注中是否含内部制作说明；图片中的文字仍需人工查看。
- `tools/check_repo_boundary.py`：检查 Git 暂存文件，避免常见凭据和客户附件误入库。
- `adapters/pptx.mjs`：按已核对的文本锚点编辑 PPTX；需要另行配置运行依赖。

这些脚本仅处理调用者提供的本地文件。请先核对素材授权、预览效果和最终交付物。仓库没有自动接单、自动发送或无人值守交付能力。

## 验证

```powershell
python -m unittest discover -s tests -v
python tools/check_repo_boundary.py
```

`config/runtime.example.json` 是占位配置。真实路径、数据库、订单文件和凭据应留在本机，不能提交到公开仓库。
