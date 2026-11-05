"""Microsoft Store price watch. Runtime dependencies: Python 3.12 and gh CLI."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class PriceError(RuntimeError):
    """A trustworthy purchase price could not be established."""


class GitHubError(RuntimeError):
    """A GitHub operation failed; do not pretend state was saved."""


def money(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise PriceError("价格字段缺失或类型错误")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise PriceError("价格不是有效数字") from exc
    if not result.is_finite() or result < 0:
        raise PriceError("价格必须是有限的非负数")
    return result


@dataclass(frozen=True)
class Config:
    product_id: str
    sku_id: str
    market: str
    language: str
    currency: str
    baseline_price: Decimal
    notify_user: str
    state_branch: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        config = cls(**{**data, "baseline_price": money(data["baseline_price"])})
        patterns = {
            "product_id": r"[A-Z0-9]{12}",
            "sku_id": r"[A-Za-z0-9]{4}",
            "market": r"[A-Z]{2}",
            "language": r"[A-Za-z]{2,3}-[A-Za-z]{2,4}",
            "currency": r"[A-Z]{3}",
            "notify_user": r"[A-Za-z0-9][A-Za-z0-9-]{0,38}",
            "state_branch": r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
        }
        for field, pattern in patterns.items():
            if not re.fullmatch(pattern, getattr(config, field)):
                raise ValueError(f"配置字段无效：{field}")
        return config

    @property
    def key(self) -> str:
        return f"{self.product_id}:{self.sku_id}:{self.market}:{self.currency}"

    @property
    def store_url(self) -> str:
        return f"https://apps.microsoft.com/detail/{self.product_id}?" + urlencode(
            {"hl": self.language, "gl": self.market}
        )


@dataclass(frozen=True)
class Quote:
    product_id: str
    title: str
    market: str
    currency: str
    price: Decimal
    regular: Decimal
    checked_at: datetime
    availability_id: str
    expires_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "title": self.title,
            "market": self.market,
            "currency": self.currency,
            "price": str(self.price),
            "regular": str(self.regular),
            "checked_at": self.checked_at.isoformat(),
            "availability_id": self.availability_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def parse_date(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("报价时间缺少时区")
    return result


def parse_catalog(payload: dict[str, Any], config: Config, now: datetime | None = None) -> Quote:
    """Select the active CN desktop purchase of the configured full SKU only."""
    now = now or datetime.now(UTC)
    try:
        product = next(p for p in payload["Products"] if p["ProductId"] == config.product_id)
        markets = {m for item in product["MarketProperties"] for m in item["Markets"]}
        if config.market not in markets:
            raise PriceError("接口返回的地区与配置不一致")
        title = product["LocalizedProperties"][0]["ProductTitle"]
        candidates = []
        for item in product["DisplaySkuAvailabilities"]:
            sku = item["Sku"]
            if (
                sku["SkuId"] != config.sku_id
                or sku["SkuType"].lower() != "full"
                or sku.get("Properties", {}).get("IsTrial")
                or sku.get("SubscriptionProperties")
            ):
                continue
            for offer in item["Availabilities"]:
                if config.market not in offer["Markets"] or "Purchase" not in offer["Actions"]:
                    continue
                conditions = offer["Conditions"]
                start, end = parse_date(conditions["StartDate"]), parse_date(conditions["EndDate"])
                if not start <= now < end:
                    continue
                platforms = conditions["ClientConditions"]["AllowedPlatforms"]
                if not any(p["PlatformName"] == "Windows.Desktop" for p in platforms):
                    continue
                if conditions.get("BeneficiaryHasEntitlementKeys") or conditions.get(
                    "RequiredEntitlementKeys"
                ):
                    continue
                price = offer["OrderManagementData"]["Price"]
                if price["CurrencyCode"] != config.currency:
                    continue
                current, regular = money(price["ListPrice"]), money(price["MSRP"])
                if current > regular:
                    raise PriceError("接口现价高于标价，停止生成提醒")
                candidates.append(
                    Quote(
                        config.product_id,
                        title,
                        config.market,
                        config.currency,
                        current,
                        regular,
                        now,
                        offer["AvailabilityId"],
                        end,
                    )
                )
        if not candidates:
            raise PriceError("没有找到当前有效的正式版桌面购买报价；不会将试用或许可记录当作限免")
        return min(candidates, key=lambda q: (q.price, -q.regular))
    except (KeyError, IndexError, TypeError, ValueError, StopIteration, AttributeError) as exc:
        raise PriceError("微软商店接口结构或报价日期已变化，请检查监控代码") from exc


def fetch_quote(config: Config) -> Quote:
    query = urlencode(
        {"bigIds": config.product_id, "market": config.market, "languages": config.language}
    )
    url = f"https://displaycatalog.mp.microsoft.com/v7.0/products?{query}"
    request = Request(
        url, headers={"User-Agent": "VividPriceWatch/1.0", "Accept": "application/json"}
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=25) as response:
                payload = json.loads(response.read(5_000_001).decode("utf-8-sig"))
            return parse_catalog(payload, config)
        except HTTPError as exc:
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise PriceError(f"商店接口返回 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt == 2:
                raise PriceError("重试三次后仍无法连接微软商店接口") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PriceError("商店接口没有返回有效 JSON") from exc
        time.sleep(2**attempt)
    raise PriceError("商店抓取失败")


def decide(state: dict[str, Any], quote: Quote, config: Config) -> tuple[dict[str, Any], bool]:
    on_sale = quote.price < max(quote.regular, config.baseline_price)
    previous = state.get("last_quote")
    last_alert = state.get("last_alert_price")
    if on_sale:
        alert = (
            not state.get("sale_active") or last_alert is None or quote.price < money(last_alert)
        )
    else:
        alert = bool(previous and quote.price < money(previous["price"]))
    history_entry = {
        "checked_at": quote.checked_at.isoformat(),
        "price": str(quote.price),
        "regular": str(quote.regular),
    }
    updated = {
        "version": 1,
        "watch_key": config.key,
        "last_quote": quote.as_dict(),
        "sale_active": on_sale,
        "last_alert_price": str(quote.price) if alert else (last_alert if on_sale else None),
        "event_number": state.get("event_number", 0) + int(alert),
        "history": [*state.get("history", []), history_entry][-180:],
    }
    return updated, alert


class GitHub:
    def __init__(self, repository: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub 仓库必须采用 owner/repo 格式")
        self.base = f"repos/{repository}"

    def api(
        self,
        endpoint: str,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        *,
        missing_ok: bool = False,
    ) -> Any:
        args = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
        if data is not None:
            args += ["--input", "-"]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    args,
                    input=json.dumps(data, ensure_ascii=False) if data is not None else None,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=45,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise GitHubError("无法运行 gh CLI，请检查安装、登录和网络") from exc
            if result.returncode == 0:
                try:
                    return json.loads(result.stdout) if result.stdout.strip() else None
                except json.JSONDecodeError as exc:
                    raise GitHubError("GitHub 没有返回有效 JSON") from exc
            match = re.search(r"HTTP (\d{3})", result.stderr)
            status = int(match.group(1)) if match else None
            if missing_ok and status == 404:
                return None
            if method == "GET" and status in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2**attempt)
                continue
            detail = f"HTTP {status}" if status else "请检查 gh 登录、权限和网络"
            raise GitHubError(f"GitHub {method} 请求失败：{detail}")
        raise GitHubError("GitHub 重试失败")

    def load_state(self, config: Config) -> tuple[dict[str, Any], str | None]:
        content = self.api(
            f"{self.base}/contents/state.json?ref={config.state_branch}", missing_ok=True
        )
        if content is None:
            return {}, None
        try:
            state = json.loads(base64.b64decode(content["content"]).decode("utf-8"))
            if state["version"] != 1 or state["watch_key"] != config.key:
                raise ValueError("不匹配的状态版本或产品")
            if not isinstance(state["history"], list) or not isinstance(state["event_number"], int):
                raise ValueError("状态字段无效")
            money(state["last_quote"]["price"])
            return state, content["sha"]
        except (KeyError, ValueError, TypeError, UnicodeError, PriceError) as exc:
            raise GitHubError("远端价格状态无效；为避免重复提醒，不自动重置") from exc

    def save_state(self, config: Config, state: dict[str, Any], sha: str | None) -> None:
        branch = self.api(f"{self.base}/git/ref/heads/{config.state_branch}", missing_ok=True)
        if branch is None:
            default = self.api(self.base)["default_branch"]
            commit = self.api(f"{self.base}/git/ref/heads/{default}")["object"]["sha"]
            self.api(
                f"{self.base}/git/refs",
                "POST",
                {"ref": f"refs/heads/{config.state_branch}", "sha": commit},
            )
        body = {
            "message": "chore: record Microsoft Store price check",
            "branch": config.state_branch,
            "content": base64.b64encode(
                (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            ).decode("ascii"),
        }
        if sha:
            body["sha"] = sha
        self.api(f"{self.base}/contents/state.json", "PUT", body)

    def find_issue(self, marker: str, *, open_only: bool = False) -> dict[str, Any] | None:
        page = 1
        status = "open" if open_only else "all"
        while True:
            issues = self.api(
                f"{self.base}/issues?state={status}&sort=created&direction=desc"
                f"&per_page=100&page={page}"
            )
            for issue in issues:
                if "pull_request" not in issue and marker in (issue.get("body") or ""):
                    return issue
            if len(issues) < 100:
                return None
            page += 1

    def create_issue(self, title: str, body: str, assignee: str) -> dict[str, Any]:
        return self.api(
            f"{self.base}/issues", "POST", {"title": title, "body": body, "assignees": [assignee]}
        )

    def close_issue(self, number: int, body: str) -> None:
        self.api(f"{self.base}/issues/{number}/comments", "POST", {"body": body})
        self.api(
            f"{self.base}/issues/{number}",
            "PATCH",
            {"state": "closed", "state_reason": "completed"},
        )


def china_time(value: datetime) -> str:
    return value.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S（北京时间）")


def quote_body(quote: Quote, config: Config) -> str:
    lines = [
        f"@{config.notify_user}\n",
        f"**{quote.title}** · 中国区 Microsoft Store\n",
        f"- 当前价格：**{quote.currency} {quote.price:.2f}**",
        f"- 商店标价：{quote.currency} {quote.regular:.2f}",
        f"- 监控基准：{quote.currency} {config.baseline_price:.2f}",
        f"- 检查时间：{china_time(quote.checked_at)}",
    ]
    reference = max(quote.regular, config.baseline_price)
    if reference > quote.price:
        lines.append(f"- 相对标价/基准节省：{(1 - quote.price / reference) * 100:.0f}%")
    if quote.expires_at:
        lines.append(f"- 本条报价有效至：{china_time(quote.expires_at)}（不保证期间价格不变）")
    lines += [
        "",
        f"[打开商店购买页面]({config.store_url})",
        "",
        "实际成交价以登录后的结算页面为准。",
    ]
    return "\n".join(lines)


def alert_marker(config: Config, event_number: int) -> str:
    identity = f"{config.key}|{event_number}"
    return "<!-- price-alert:" + hashlib.sha256(identity.encode()).hexdigest()[:24] + " -->"


def recover_alerts(state: dict[str, Any], github: GitHub, config: Config) -> dict[str, Any]:
    """Reconcile delivered Issues if a prior run failed to commit its new state."""
    recovered = dict(state)
    for _ in range(50):
        number = recovered.get("event_number", 0) + 1
        issue = github.find_issue(alert_marker(config, number))
        if issue is None:
            return recovered
        try:
            match = re.search(r"<!-- vivid-alert-state:([A-Za-z0-9+/=]+) -->", issue["body"])
            if match is None:
                raise ValueError("提醒元数据缺失")
            data = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
            if data["watch_key"] != config.key or data["event_number"] != number:
                raise ValueError("提醒元数据不匹配")
            last_quote = data["last_quote"]
            money(data["last_alert_price"])
            money(last_quote["price"])
            parse_date(last_quote["checked_at"])
            recovered.update(data)
        except (KeyError, TypeError, ValueError, UnicodeError, PriceError) as exc:
            raise GitHubError(
                "已发送 Issue 的去重元数据无效；为避免重复通知，停止本次运行"
            ) from exc
    raise GitHubError("待恢复提醒过多，请检查远端状态是否长期无法保存")


def run_monitor(
    config: Config,
    github: GitHub,
    *,
    fetch: Callable[[Config], Quote] = fetch_quote,
    test_notification: bool = False,
) -> tuple[Quote, bool]:
    state, sha = github.load_state(config)
    error_marker = f"<!-- vivid-watch-error:{config.key} -->"
    try:
        quote = fetch(config)
    except PriceError as exc:
        if not github.find_issue(error_marker, open_only=True):
            github.create_issue(
                "[监控异常] Vivid Player 价格暂时无法获取",
                f"@{config.notify_user}\n\n{exc}\n\n价格未写为零，原状态已保留。"
                "请查看 Actions 日志；相同异常在恢复前只提醒一次。\n\n" + error_marker,
                config.notify_user,
            )
        raise
    state = recover_alerts(state, github, config)
    updated, alert = decide(state, quote, config)
    if alert:
        marker = alert_marker(config, updated["event_number"])
        metadata = {
            key: updated[key]
            for key in (
                "watch_key",
                "event_number",
                "last_quote",
                "last_alert_price",
                "sale_active",
            )
        }
        encoded = base64.b64encode(json.dumps(metadata).encode("utf-8")).decode("ascii")
        github.create_issue(
            f"[降价提醒] Vivid Player · {quote.currency} {quote.price:.2f}",
            quote_body(quote, config) + f"\n\n{marker}\n<!-- vivid-alert-state:{encoded} -->",
            config.notify_user,
        )
    if test_notification:
        github.create_issue(
            "[通知测试] Vivid Player 折扣监控已启用",
            "这是通知渠道测试，**不代表当前正在打折**。\n\n" + quote_body(quote, config),
            config.notify_user,
        )
    github.save_state(config, updated, sha)
    error_issue = github.find_issue(error_marker, open_only=True)
    if error_issue:
        github.close_issue(
            error_issue["number"], f"监控已恢复，当前报价 {quote.currency} {quote.price:.2f}。"
        )
    return quote, alert


def main() -> int:
    parser = argparse.ArgumentParser(description="Vivid Player 中国区 Microsoft Store 降价提醒")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument(
        "--dry-run", action="store_true", help="只获取实价，不读写 GitHub 或发送通知"
    )
    parser.add_argument("--test-notification", action="store_true", help="发送明确标注的测试 Issue")
    args = parser.parse_args()
    try:
        config = Config.from_dict(json.loads(args.config.read_text(encoding="utf-8-sig")))
        if args.dry_run:
            quote, alert = fetch_quote(config), False
        else:
            quote, alert = run_monitor(
                config,
                GitHub(args.repo),
                test_notification=args.test_notification
                or os.environ.get("TEST_NOTIFICATION") == "1",
            )
        summary = (
            f"## Vivid Player 价格检查\n\n"
            f"- 当前价格：**{quote.currency} {quote.price:.2f}**\n"
            f"- 商店标价：{quote.currency} {quote.regular:.2f}\n"
            f"- 检查时间：{china_time(quote.checked_at)}\n"
            f"- 降价提醒：{'已处理（含去重）' if alert else '无新降价，保持安静'}\n"
            f"- 商店：[中国区页面]({config.store_url})\n"
        )
        print(summary)
        if path := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(path).open("a", encoding="utf-8") as report:
                report.write(summary)
        return 0
    except (PriceError, GitHubError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f"监控失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
