# AI 开发入口

修改、调试或审查本仓库前，必须完整阅读并遵循根目录的 [`CONTRIBUTING.md`](CONTRIBUTING.md)。其中定义了项目架构、数据契约、数据源插件化、缓存与性能要求、测试矩阵以及 PR 复审和合并标准。

涉及代码二次开发、前端插槽、后端可替换策略、扩展注册或上游升级兼容时，还必须阅读 [`docs/secondary-development.md`](docs/secondary-development.md)。该文档区分当前已实现能力与目标扩展契约；不得根据设计示例虚构尚不存在的 API。

同时遵守以下规则：

- 先理解调用链和现有测试，再进行修改。
- 保持实现简单、改动范围最小，不处理无关问题。
- 不覆盖工作区已有修改，不虚构测试或审查结果。
- 以实际验证结果作为完成标准。

## 已确定最终版策略（冻结）

以下 `generated/` 目录中的策略文件已经由用户确认，属于最终版本。除非用户明确要求解冻并指定新的版本号，否则禁止修改、重命名、替换、格式化或自动重生成这些文件。后续优化必须新建更高版本文件（例如 `v4`），不得回写以下冻结版本：

- `generated/custom_sequoia_high_tight_flag_v2.py`
- `generated/custom_sequoia_high_tight_flag_v3.py`
- `generated/custom_sequoia_limit_up_shakeout_v2.py`
- `generated/custom_sequoia_ma_volume_v2.py`
- `generated/custom_sequoia_private_placement_v2.py`
- `generated/custom_sequoia_rps_breakout_v2.py`
- `generated/custom_sequoia_turtle_trade_v2.py`
- `generated/custom_sequoia_uptrend_limit_down_v2.py`
