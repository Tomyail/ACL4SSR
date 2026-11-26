#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 v2fly/domain-list-community 的 data/anthropic 同步成 Clash 规则集。

为什么需要这个脚本：上游那份数据更新比较频繁（跟着 Claude 发版走，会陆续新增
claude.* 子站、CDN、遥测域名），但它本身是 V2Ray domain-list 源格式——裸域名、
full: 前缀、@attr 标签——Clash / subconverter 的 ruleset= 只认
DOMAIN-SUFFIX,xxx / DOMAIN,xxx 这类规则行，不能直接引用。所以由
.github/workflows/update.yml 每天拉一次、转一次、有变化才提交。

用法：
    python3 scripts/sync_claude_domains.py            # 同步并写回
    python3 scripts/sync_claude_domains.py --check    # 只检查，有差异时退出码 1
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

SRC_URL = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/anthropic"
OUT_FILE = Path(__file__).resolve().parent.parent / "Clash" / "Ruleset" / "ClaudeAI.list"
# mihomo 的 rule-provider 镜像，两份要一起改，否则用 yaml 的人只剩旧的两条
OUT_YAML = Path(__file__).resolve().parent.parent / "Clash" / "Providers" / "Ruleset" / "ClaudeAI.yaml"

# 上游没有、但用 Claude 时会顺手请求的域名与 IP 段。这些漏掉时的症状是
# 「登录/验证卡住、页面一直重试报错、订阅付款失败」，而 v2fly 的 anthropic 数据
# 只收自家域名，不收这些第三方依赖，所以在这里补齐。
# 每一项就是一条 Clash 规则行拆成的字段，第三项是可选的附加参数：
# IP 段一律 no-resolve，避免匹配时本地做 DNS 解析。
EXTRA_RULES: list[tuple[str, ...]] = [
    # ---- 鉴权：只写 Anthropic 自己的 Auth0 租户，不写 auth0.com，
    #      否则会把 OpenAI 等别的鉴权一起抢过来 ----
    ("DOMAIN-SUFFIX", "anthropic.auth0.com"),
    # ---- 官网博客 / 官网 CDN ----
    ("DOMAIN-SUFFIX", "anthropic-com.ghost.io"),
    ("DOMAIN-SUFFIX", "anthropic.com.cdn.cloudflare.net"),
    # ---- 客服与站内信（帮助中心、服务状态弹窗）----
    ("DOMAIN-SUFFIX", "intercom.io"),
    ("DOMAIN-SUFFIX", "intercomcdn.com"),
    ("DOMAIN-SUFFIX", "intercomassets.com"),
    ("DOMAIN-SUFFIX", "intercom-messenger.com"),
    # ---- 前端监控 / 遥测（漏了不影响登录，但页面会一直重试报错）----
    ("DOMAIN-SUFFIX", "us5.datadoghq.com"),
    ("DOMAIN-SUFFIX", "browser-intake-us5-datadoghq.com"),
    ("DOMAIN-SUFFIX", "us.sentry.io"),
    ("DOMAIN-SUFFIX", "statsigapi.net"),
    ("DOMAIN-SUFFIX", "usefathom.com"),
    ("DOMAIN-SUFFIX", "siftscience.com"),
    ("DOMAIN-SUFFIX", "fcmtoken.googleapis.com"),
    # ---- 注册 / 人机验证（Cloudflare Turnstile、hCaptcha、Persona、Yoti）----
    ("DOMAIN-SUFFIX", "hcaptcha.com"),
    ("DOMAIN-SUFFIX", "challenges.cloudflare.com"),
    ("DOMAIN-SUFFIX", "withpersona.com"),
    ("DOMAIN-SUFFIX", "yoti.com"),
    # ---- 支付：Claude Pro/Max 订阅页的 Stripe 前端 ----
    ("DOMAIN-SUFFIX", "r.stripe.com"),
    # ---- Anthropic 自有 IP 段：客户端走 IP 直连、没有 SNI 时靠这几条兜住 ----
    ("IP-CIDR", "34.8.0.0/16", "no-resolve"),
    ("IP-CIDR", "160.79.104.0/21", "no-resolve"),
    ("IP-CIDR", "153.61.208.0/20", "no-resolve"),
    ("IP-CIDR", "153.61.224.0/22", "no-resolve"),
]

HEADER = """\
# 内容：Claude（Anthropic）
# 数量：{count}条
# 来源：{src}
#       （v2fly 的裸域名 = DOMAIN-SUFFIX，full: 前缀 = DOMAIN 精确匹配）
#       另有一批上游未收但用 Claude 必走的域名 / Anthropic IP 段（客服、人机验证、
#       遥测、支付、自有 IP），写在 scripts/sync_claude_domains.py 的 EXTRA_RULES 里。
# 更新：本文件由 scripts/sync_claude_domains.py 生成，
#       .github/workflows/update.yml 每天自动同步一次，不需要手改。
#       上游这份 anthropic 数据更新比较频繁（跟着 Claude 发版节奏走，会陆续新增
#       claude.* 子站、CDN、遥测域名），所以刻意用自动同步而不是一次性抄写。
# 注意：本规则集必须排在 AI.list 之前，否则会被 AI.list 里的
#       DOMAIN-KEYWORD,claude / DOMAIN-KEYWORD,anthropic 先接走，单独分组形同虚设。
"""


def fetch(url: str, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ACL4SSR-sync"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - 上游抖动不应该炸掉整个 workflow
            last = exc
            print(f"[warn] attempt {attempt}/{retries} failed: {exc}", file=sys.stderr)
    raise RuntimeError(f"fetch failed: {url}") from last


def parse_domain_list(text: str) -> list[tuple[str, str]]:
    """domain-list -> [(clash 类型, 域名)]，保持上游顺序、去重。"""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()  # 去掉行内注释
        if not line:
            continue
        # 去掉 @attr 标签（如 "@telemetry"）
        parts = [p for p in line.split() if not p.startswith("@")]
        if not parts:
            continue
        domain = parts[0]
        if domain.startswith("full:"):
            kind, domain = "DOMAIN", domain[len("full:") :]
        elif domain.startswith(("regexp:", "keyword:")):
            print(f"[skip] unsupported prefix: {raw.strip()}", file=sys.stderr)
            continue
        else:
            kind = "DOMAIN-SUFFIX"
        domain = domain.strip(".")
        if not domain:
            continue
        if (kind, domain) not in seen:
            seen.add((kind, domain))
            out.append((kind, domain))
    return out


def format_rule(rule: tuple[str, ...]) -> str:
    """('IP-CIDR', '34.8.0.0/16', 'no-resolve') -> 'IP-CIDR,34.8.0.0/16,no-resolve'。"""
    return ",".join(rule)


def render(rules: list[tuple[str, ...]]) -> str:
    lines = [HEADER.format(count=len(rules), src=SRC_URL)]
    for rule in rules:
        lines.append(format_rule(rule))
    return "\n".join(lines) + "\n"


def render_yaml(rules: list[tuple[str, ...]]) -> str:
    lines = ["payload:"]
    for line in HEADER.format(count=len(rules), src=SRC_URL).splitlines():
        lines.append(f"  {line}" if line.startswith("#") else line)
    for rule in rules:
        lines.append(f"  - {format_rule(rule)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查是否有变化")
    args = ap.parse_args()

    try:
        upstream = parse_domain_list(fetch(SRC_URL))
    except RuntimeError as exc:
        print(f"[error] {exc}; 保留现有文件不动", file=sys.stderr)
        return 0 if not args.check else 0

    rules = upstream + [r for r in EXTRA_RULES if r not in upstream]
    new_text = render(rules)
    new_yaml = render_yaml(rules)
    old_text = OUT_FILE.read_text(encoding="utf-8") if OUT_FILE.exists() else ""
    old_yaml = OUT_YAML.read_text(encoding="utf-8") if OUT_YAML.exists() else ""

    if new_text == old_text and new_yaml == old_yaml:
        print(f"[ok] {OUT_FILE.name} / {OUT_YAML.name} 已是最新（{len(rules)} 条）")
        return 0

    if args.check:
        print(f"[diff] {OUT_FILE.name} 或 {OUT_YAML.name} 需要更新")
        return 1

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(new_text, encoding="utf-8")
    OUT_YAML.parent.mkdir(parents=True, exist_ok=True)
    OUT_YAML.write_text(new_yaml, encoding="utf-8")
    print(f"[ok] 已写入 {OUT_FILE} 与 {OUT_YAML}（{len(rules)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
