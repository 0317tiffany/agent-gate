# agent-gate

工具调用 Agent 的退化信号门禁（Degradation Gate）——以天气查询 Agent 为最小单元示例。

判定标准只有一个：**一次运行中可观察的具体证据**。不写形容词，只写行为断言。

## 组成

| 文件 | 说明 |
|---|---|
| `gate.yaml` | 规则文件：4 条共享判定规则（R1 温度宽松提取 / R2 温度严格格式 / R3 空输入 case 标记 / R4 城市名原文子串）+ 6 条退化信号（S1–S4 = block，S5/S6 = warn） |
| `gate.py` | 断言脚本：读取 gate.yaml，判定一份轨迹 JSON，输出 report.md；存在 block 级触发则退出码 1 |
| `cases/` | 轨迹用例：baseline（全绿）、degrade（2 block + 2 warn）、empty_violation（空输入违规） |
| `.github/workflows/agent-gate.yml` | CI 门禁：每个 PR 自动跑 gate，block 则 Action 变红，并把 report.md 评论到 PR |

## 本地运行

```bash
python gate.py cases/baseline.json --out report.md          # 全绿，exit 0
python gate.py cases/degrade.json --out report.md           # S1/S3 block，exit 1
python gate.py cases/empty_violation.json --out report.md   # S4 block，exit 1
```

零依赖：优先使用 PyYAML，未安装时自动回退内置迷你解析器（覆盖 gate.yaml 语法子集）。

## 6 条退化信号

| # | 信号 | 级别 | 判定 |
|---|---|---|---|
| S1 | 温度与工具返回不一致 | block | R1 提取输出温度转 float ≠ 成功返回的 temp_c；或全部调用失败时输出仍含温度 |
| S2 | 最终答案缺少/错必要字段 | block | 输出不含输入原文城市名子串（R4），或无 R2 匹配的摄氏温度 |
| S3 | 工具序列偏离约定 | block | a) 非 get_weather 调用 ≥ 1；b) get_weather ≥ 3 次；c) 首次成功后仍再调 |
| S4 | 空输入未按约定处理 | block | R3 判定空输入时：工具调用 ≥ 1，或输出 ≠ 约定文本 |
| S5 | token 涨幅超 50% | warn | 同输入 token > baseline × 1.5；无 baseline 则 warn |
| S6 | 输出缺少穿衣建议 | warn | 剔除城市名与温度子串后剩余 < 5 字符或无实质字符 |

## 轨迹 JSON 字段

```json
{
  "user_input": "北京",
  "input_type": "normal",
  "tool_calls": [{"tool": "get_weather", "args": {"city": "北京"}, "result": {"temp_c": 22}, "success": true}],
  "final_output": "北京 22℃，适合穿薄外套。",
  "total_tokens": 200
}
```

退出码约定：存在 block 级触发 = 1；仅 warn 或全绿 = 0。
