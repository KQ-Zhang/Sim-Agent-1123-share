# 运行与集成说明

版本 0.1.0；2026-11-23。Python 3.10+，仅依赖标准库；本机验证 Python 3.12.7。以下命令在 llm-collective-dynamics 目录执行，Windows CMD/PowerShell 均可用。也可从任意目录使用 run.py 和 config 的完整路径。

## 离线运行

```text
python run.py plan --config configs/smoke.json
python run.py run --config configs/smoke.json
python -m unittest discover -s tests -v
```

默认矩阵为两事件×两模式×两 fixture 代理×三个种子，共 24 次运行，每次 12 人、6 轮。fixture 为确定性的人工响应生成器，不调用网络，不代表 LLM，不提供真实价值状态。

```text
python run.py run --config configs/smoke.json --until 2
python run.py run --config configs/smoke.json
```

第一次只提交至第二轮，第二次接续。已完成目录不会倒退；如要演示中断，先在新配置中设置新的 outputs 子目录。完成后重跑不增加请求。原子 checkpoint 是恢复依据，trajectory/summary 可重建。轮内已完成请求通过持久化缓存复用；接口已经执行但响应尚未落盘时，无法保证远端恰好一次执行，预算记录会保留该尝试。

## 配置与日志

configs/base.json 是公共配置，smoke.json 使用 extends 继承；子配置递归覆盖对象、整体替换数组。路径相对于定义该字段的配置文件，不依赖启动目录。配置拒绝未知字段和不支持的模式。增加实验时复制实验协议模板，配置引用新的冻结协议；不覆盖已用配置。当前批处理顺序执行，不提供并行调度。

输入：data/fixtures/scenarios.json。包含虚构背景、角色/关键词兴趣、按轮入场的消息，以及固定回放每轮指定的卡片与本人历史。固定回放的曝光和历史不受模型响应影响，且始终上线；闭环使用配对上线/渠道日程并允许曝光随行动分化。输入的 fixture=true 是强制标签；当前场景加载器尚不接收正式历史场景。

输出在 outputs/framework-smoke-v1，每个哈希 run_id 一份目录：

- resolved_config.json：合并配置、运行身份及协议/源码/材料/prompt 哈希。
- network.json：固定社交边。
- checkpoint.json：全部已提交状态、记录及完整性哈希。
- trajectory.json：逐代理逐轮输入、动作、状态、请求关联和测量占位。
- attempts.jsonl、requests/：每次尝试的参数、缓存、原始响应和错误类型，不保存凭据。
- summary.json：运行完成状态、计数、轨迹哈希；batch.json 汇总本配置的批次。

同一输出根目录下不同配置/源码/输入产生新 run_id；旧目录保留。若 run_id 截断哈希发生冲突，完整冻结配置检查会拒绝覆盖。网络、上线、渠道、推荐及动作使用按用途派生的随机数。不同种子在请求中单独标识，不跨独立重复复用响应。

公开背景、曝光、本人历史分别装配；当前轮的新行动只在全部参与者决策后提交。转发只能选择本轮卡片，跨渠道分享只能选择本人历史亲自见过的另一渠道卡片。offline/silent/published 独立记录；测量一律 value=null、status=not_measured，不把它当作中立、保持或价值分档。

## 旧轨迹只读审计

```text
python run.py audit-e23 --source ../outputs/social-values-v1 --output outputs/e23-audit-new
```

输出必须是新目录。保留来源文件哈希、行号、原文、原评分及 own_full_history 测量范围。历史评分标记 legacy_uncalibrated，不自动替代当前轮评分。支持 E23 原格式；E26/E29 的专用适配器尚未实现。

首次生成默认 smoke 后，可以运行批次验收。独立克隆不需要旧实验：

```text
python scripts/verify_smoke.py
```

旧文件和e23-audit-v1存在时会额外验证来源哈希；缺失时明确报告history_checks_skipped，不把未检查项记为通过。需要强制检查旧历史时加`--require-history`。测试使用`tests/legacy_reference.json`中冻结的纯函数输出进行独立验证；原旧源码存在时再进行直接比较。此文件不包含账号或历史发言。

## 模型接口

chat.example.json 展示同一运行器的聊天接口配置。需替换模型名称/冻结版本，设置 LCD_API_BASE（以 /v1 结尾）和可选 LCD_API_KEY，手动启动时加 --allow-remote。适配器使用旧代码同类的 /chat/completions JSON 请求，不承诺所有服务都支持 seed 或 response_format；供应商差异需要在正式运行前校准。请求失败不会改温度和种子，尝试总数和预算跨恢复保留。

本次只通过本机回环 HTTP 服务测试了请求、响应解析与缓存，没有访问真实模型。示例配置使用虚构场景，即使接入真实模型仍是工程连通性测试。正式实验须另冻结研究协议、实际场景、模型版本、采样设置、测量、阈值与预算。

## 本次迁移范围

data/legacy_sources.json 记录旧文件哈希与函数行号。E01 小世界图仅将度和重连概率参数化；E02 作息、E18 热度账本、E26 跨平台文件锁为原函数/类提取。其余环境模块是按 E19/E23 机制重构的最小实现，未声称与旧 E21 轨迹完全等价。

当前推荐采用时间衰减、热度加权抽样优先级与关键词兴趣；社区展示每个邻居最近提交的消息。未迁入 E19 的完整多阶段候选池、专业账号监测、E21 的历史背景 Parquet 准备与 BGE-M3 兴趣矩阵；没有复制旧凭据、模型权重或大规模输入。完整旧平台精确回放、独立探针与裁判、动力学拟合、最终留出预测仍待后续实现。

目前 checkpoint 保存完整轨迹，适用于小规模集成验证；大规模运行前应改为分轮存储和增量状态。当前实现以进程故障恢复为验证范围，不承诺断电时所有平台文件系统的目录元数据持久性。
