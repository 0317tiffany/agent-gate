#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gate.py — 天气查询 Agent 本地退化信号断言（Day 2）

用法:
    python gate.py cases/baseline.json
    python gate.py cases/degrade.json --config gate.yaml --out report.md

轨迹 JSON 字段:
    user_input     用户消息原文（城市名，可为空）
    input_type     case 标记: normal / empty（R3 用；缺省时按 strip 兜底判定）
    tool_calls     工具调用列表，每项 {tool, args, result, success}
    final_output   最终输出文本
    total_tokens   本次运行总 token（口径须与 baseline 一致）

退出码: 1 = 存在 block 级触发；0 = 仅 warn 或全绿
依赖:  优先使用 PyYAML；未安装时自动回退到内置迷你解析器（覆盖 gate.yaml 的语法子集）
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

TAIL_PUNCT = "。．.!！?？~～"


# ================= 配置加载 =================

def _unquote(s):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [] if not inner else [_scalar(x) for x in inner.split(",")]
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    for conv in (int, float):
        try:
            return conv(s)
        except ValueError:
            pass
    return s


def _split_kv(line):
    idx = line.find(":")
    if idx == -1:
        return _unquote(line.strip()), ""
    return _unquote(line[:idx].strip()), line[idx + 1:].strip()


def _mini_yaml(text):
    """解析 gate.yaml 用到的语法子集:
    整行注释 / 两空格缩进 / 嵌套 mapping / list-of-mapping / 行内 [a, b] / 带引号标量"""
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), stripped))

    pos = [0]

    def block_at(indent):
        if pos[0] >= len(lines):
            return None
        if lines[pos[0]][1].startswith("- "):
            items = []
            while (pos[0] < len(lines)
                   and lines[pos[0]][0] == indent
                   and lines[pos[0]][1].startswith("- ")):
                content = lines[pos[0]][1][2:]
                pos[0] += 1
                if ":" not in content:
                    items.append(_scalar(content))
                    continue
                item = {}
                k, v = _split_kv(content)
                _fill(item, k, v, indent + 2)
                items.append(item)
            return items
        mapping = {}
        while (pos[0] < len(lines)
               and lines[pos[0]][0] == indent
               and not lines[pos[0]][1].startswith("- ")):
            k, v = _split_kv(lines[pos[0]][1])
            pos[0] += 1
            if v:
                mapping[k] = _scalar(v)
            elif pos[0] < len(lines) and lines[pos[0]][0] > indent:
                mapping[k] = block_at(lines[pos[0]][0])
            else:
                mapping[k] = None
        return mapping

    def _fill(item, k, v, child_indent):
        if v:
            item[k] = _scalar(v)
        elif pos[0] < len(lines) and lines[pos[0]][0] > child_indent - 2:
            item[k] = block_at(lines[pos[0]][0])
        else:
            item[k] = None
        while (pos[0] < len(lines)
               and lines[pos[0]][0] >= child_indent
               and not lines[pos[0]][1].startswith("- ")):
            k2, v2 = _split_kv(lines[pos[0]][1])
            pos[0] += 1
            if v2:
                item[k2] = _scalar(v2)
            elif pos[0] < len(lines) and lines[pos[0]][0] > child_indent:
                item[k2] = block_at(lines[pos[0]][0])
            else:
                item[k2] = None

    return block_at(0)


def load_config(path):
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:
        print("[gate] PyYAML 未安装，使用内置迷你 YAML 解析器（覆盖 gate.yaml 语法子集）")
        return _mini_yaml(text)


# ================= 判定工具 =================

def is_empty_input(trace, r3):
    r3 = r3 or {}
    flag = r3.get("case_flag")
    if flag and flag in trace:
        return trace.get(flag) == r3.get("case_value")
    if r3.get("strip_fallback", True):
        return not str(trace.get("user_input", "")).strip()
    return False


def extract_temp(text, r1_pattern):
    m = re.search(r1_pattern, text)
    if not m:
        return None, None
    raw = m.group()
    return float(re.sub(r"(℃|°C|度|\s)", "", raw)), raw


def strip_tail_punct(text):
    s = str(text).strip()
    while s and (s[-1] in TAIL_PUNCT or s[-1].isspace()):
        s = s[:-1].rstrip()
    return s


def finding(sid, name, level, status, detail):
    return {"id": sid, "name": name, "level": level, "status": status, "detail": detail}


# ================= 六条信号判定 =================

def judge(trace, cfg):
    rules = cfg.get("shared_rules", {}) or {}
    sigs = {s["id"]: s for s in (cfg.get("signals") or [])}
    r1 = rules.get("R1_temp_extract", {}).get("pattern", r"-?\d+(\.\d+)?\s*(℃|°C|度)")
    r2 = rules.get("R2_temp_format", {}).get("pattern", r"-?\d+(\.\d+)?\s*℃")
    r3 = rules.get("R3_empty_input", {})
    calls = trace.get("tool_calls") or []
    out = str(trace.get("final_output", ""))
    findings = []

    def name_of(sid):
        return sigs.get(sid, {}).get("name", sid)

    # --- S1 温度与工具返回不一致 ---
    ok_calls = [c for c in calls if c.get("tool") == "get_weather" and c.get("success")]
    value, raw = extract_temp(out, r1)
    if ok_calls:
        temp_c = float(ok_calls[0]["result"]["temp_c"])
        if value is None:
            findings.append(finding("S1", name_of("S1"), "block", "PASS",
                                    "输出无可提取温度，字段缺失由 S2 检查"))
        elif value != temp_c:
            findings.append(finding("S1", name_of("S1"), "block", "BLOCK",
                                    f"输出温度 {value:g} ≠ temp_c {temp_c:g}（R1 提取自 '{raw}'，转 float 比对）"))
        else:
            findings.append(finding("S1", name_of("S1"), "block", "PASS",
                                    f"输出温度 {value:g} == temp_c {temp_c:g}"))
    else:
        if value is not None:
            findings.append(finding("S1", name_of("S1"), "block", "BLOCK",
                                    f"无成功工具返回，输出仍含温度 {value:g}（'{raw}'，编造）"))
        else:
            findings.append(finding("S1", name_of("S1"), "block", "PASS",
                                    "无成功返回且输出无可提取温度"))

    # --- S2 最终答案缺少/错必要字段 ---
    if is_empty_input(trace, r3):
        findings.append(finding("S2", name_of("S2"), "block", "SKIP",
                                "空输入场景，输出字段不适用"))
    else:
        city = str(trace.get("user_input", "")).strip()
        problems = []
        if city and city not in out:
            problems.append(f"输出不含输入原文城市名子串 '{city}'（R4 写死，无别名表）")
        if not re.search(r2, out):
            problems.append("无 R2 匹配的摄氏温度（格式不合规，须为 数字+℃）")
        if problems:
            findings.append(finding("S2", name_of("S2"), "block", "BLOCK", "；".join(problems)))
        else:
            findings.append(finding("S2", name_of("S2"), "block", "PASS",
                                    f"含城市名 '{city}'，温度格式合规（R2 匹配）"))

    # --- S3 工具序列偏离约定 ---
    seq = [c.get("tool") for c in calls]
    threshold = int(sigs.get("S3", {}).get("call_count_threshold", 3))
    triggers = []
    non_gw = sorted({str(t) for t in seq if t != "get_weather"})
    if non_gw:
        triggers.append(f"a) 非 get_weather 调用: {non_gw}")
    gw_count = sum(1 for t in seq if t == "get_weather")
    if gw_count >= threshold:
        triggers.append(f"b) get_weather 调用 {gw_count} 次 ≥ {threshold}")
    first_ok = next((i for i, c in enumerate(calls)
                     if c.get("tool") == "get_weather" and c.get("success")), None)
    if first_ok is not None and any(c.get("tool") == "get_weather" for c in calls[first_ok + 1:]):
        triggers.append(f"c) 第 {first_ok + 1} 次调用已成功，其后仍出现 get_weather 调用（非约定重试）")
    if triggers:
        findings.append(finding("S3", name_of("S3"), "block", "BLOCK", "；".join(triggers)))
    else:
        findings.append(finding("S3", name_of("S3"), "block", "PASS",
                                f"调用序列 {seq} 合规"))

    # --- S4 空输入未按约定处理 ---
    if not is_empty_input(trace, r3):
        findings.append(finding("S4", name_of("S4"), "block", "SKIP", "非空输入，不适用"))
    else:
        problems = []
        if len(calls) >= 1:
            problems.append(f"空输入时调用了工具 {len(calls)} 次: {seq}")
        expected = str(sigs.get("S4", {}).get("expected_output", "请提供城市名"))
        actual = strip_tail_punct(trace.get("final_output", ""))
        if actual != expected:
            problems.append(f"输出 '{actual}' ≠ 约定文本 '{expected}'（strip 空白与结尾标点后比对）")
        if problems:
            findings.append(finding("S4", name_of("S4"), "block", "BLOCK", "；".join(problems)))
        else:
            findings.append(finding("S4", name_of("S4"), "block", "PASS",
                                    "0 次调用，输出为约定文本"))

    # --- S5 token 涨幅超 50% ---
    key = str(trace.get("user_input", "")).strip()
    baseline = (cfg.get("baselines") or {}).get(key)
    ratio = float(sigs.get("S5", {}).get("threshold_ratio", 1.5))
    policy = str(sigs.get("S5", {}).get("no_baseline_policy", "warn"))
    tokens = trace.get("total_tokens")
    if baseline is None:
        status = "WARN" if policy == "warn" else "PASS"
        findings.append(finding("S5", name_of("S5"), "warn", status,
                                f"输入 '{key}' 无 baseline，按 no_baseline_policy={policy} 处理"))
    elif tokens is not None and tokens > baseline * ratio:
        findings.append(finding("S5", name_of("S5"), "warn", "WARN",
                                f"total_tokens {tokens} > baseline {baseline} × {ratio:g} = {baseline * ratio:g}"))
    else:
        findings.append(finding("S5", name_of("S5"), "warn", "PASS",
                                f"total_tokens {tokens} ≤ {baseline * ratio:g}"))

    # --- S6 输出缺少穿衣建议 ---
    if not ok_calls:
        findings.append(finding("S6", name_of("S6"), "warn", "SKIP",
                                "前置不满足：无成功的 get_weather 调用"))
    else:
        city = str(trace.get("user_input", "")).strip()
        remainder = out.replace(city, "") if city else out
        remainder = re.sub(r1, "", remainder)
        min_chars = int(sigs.get("S6", {}).get("min_chars", 5))
        has_content = re.search(r"[\u4e00-\u9fffA-Za-z0-9]", remainder)
        if len(remainder) >= min_chars and has_content:
            findings.append(finding("S6", name_of("S6"), "warn", "PASS",
                                    f"剔除城市名与温度后剩余 {len(remainder)} 字符，含实质内容"))
        else:
            findings.append(finding("S6", name_of("S6"), "warn", "WARN",
                                    f"剔除后剩余 '{remainder}'（{len(remainder)} 字符 < {min_chars} 或无实质字符）"))

    return findings


# ================= 报告与主流程 =================

def render_report(trace_path, trace, findings, exit_code, block_code, warn_code):
    seq = [c.get("tool") for c in (trace.get("tool_calls") or [])]
    blocked = [f for f in findings if f["status"] == "BLOCK"]
    warned = [f for f in findings if f["status"] == "WARN"]
    skipped = [f for f in findings if f["status"] == "SKIP"]
    L = []
    L.append("# Gate Report")
    L.append("")
    L.append(f"- 用例: `{trace_path}`")
    L.append(f"- 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    L.append(f"- 输入: \"{trace.get('user_input', '')}\"（input_type={trace.get('input_type', '<未标记>')}）")
    L.append(f"- 工具调用序列: `{seq}`")
    L.append(f"- 最终输出: \"{trace.get('final_output', '')}\"")
    L.append(f"- total_tokens: {trace.get('total_tokens')}")
    L.append("")
    L.append("## 信号判定")
    L.append("")
    L.append("| 信号 | 名称 | 级别 | 状态 | 细节 |")
    L.append("|---|---|---|---|---|")
    for f in findings:
        L.append(f"| {f['id']} | {f['name']} | {f['level']} | **{f['status']}** | {f['detail']} |")
    L.append("")
    L.append("## 汇总")
    L.append("")
    L.append(f"- BLOCK: {len(blocked)}（{', '.join(f['id'] for f in blocked) if blocked else '无'}）")
    L.append(f"- WARN: {len(warned)}（{', '.join(f['id'] for f in warned) if warned else '无'}）")
    L.append(f"- SKIP: {len(skipped)}（{', '.join(f['id'] for f in skipped) if skipped else '无'}）")
    if exit_code == block_code:
        verdict = f"存在 block 级触发，退出码 {block_code}"
    else:
        verdict = f"仅 warn / 全绿，退出码 {warn_code}"
    L.append(f"- 结论: {verdict}")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="天气查询 Agent 退化信号门禁")
    ap.add_argument("trace", help="轨迹 JSON 路径")
    ap.add_argument("--config", default="gate.yaml", help="gate.yaml 路径（默认当前目录）")
    ap.add_argument("--out", default="report.md", help="报告输出路径（默认 report.md）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cfg = load_config(args.config)
    trace = json.loads(Path(args.trace).read_text(encoding="utf-8"))

    findings = judge(trace, cfg)

    policy = cfg.get("exit_policy", {}) or {}
    block_code = int(policy.get("block", 1))
    warn_code = int(policy.get("warn_only", 0))
    exit_code = block_code if any(f["status"] == "BLOCK" for f in findings) else warn_code

    report = render_report(args.trace, trace, findings, exit_code, block_code, warn_code)
    Path(args.out).write_text(report, encoding="utf-8")

    for f in findings:
        print(f"{f['id']:>3} [{f['status']:^5}] {f['detail']}")
    print(f"report -> {args.out}")
    print(f"exit_code={exit_code} (block={sum(1 for f in findings if f['status'] == 'BLOCK')}, "
          f"warn={sum(1 for f in findings if f['status'] == 'WARN')})")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
