"""Behavioral tests; no network access or real GitHub writes."""

import base64
import copy
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from monitor import (
    Config,
    GitHub,
    GitHubError,
    PriceError,
    Quote,
    decide,
    fetch_quote,
    main,
    parse_catalog,
    run_monitor,
)

NOW = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)


def config():
    return Config.from_dict(
        {
            "product_id": "9PF8WT09D2NT",
            "sku_id": "0010",
            "market": "CN",
            "language": "zh-CN",
            "currency": "CNY",
            "baseline_price": "25.00",
            "notify_user": "coyomii",
            "state_branch": "watch-state",
        }
    )


def availability(price="25", regular="25", **changes):
    result = {
        "AvailabilityId": "offer-1",
        "Markets": ["CN"],
        "Actions": ["Purchase", "Details"],
        "Conditions": {
            "StartDate": "2026-10-01T00:00:00Z",
            "EndDate": "2026-12-01T00:00:00Z",
            "ClientConditions": {"AllowedPlatforms": [{"PlatformName": "Windows.Desktop"}]},
        },
        "OrderManagementData": {
            "Price": {
                "CurrencyCode": "CNY",
                "ListPrice": price,
                "MSRP": regular,
            }
        },
    }
    result.update(changes)
    return result


def catalog(*offers):
    return {
        "Products": [
            {
                "ProductId": "9PF8WT09D2NT",
                "LocalizedProperties": [{"ProductTitle": "Vivid Player"}],
                "MarketProperties": [{"Markets": ["CN"]}],
                "DisplaySkuAvailabilities": [
                    {
                        "Sku": {
                            "SkuId": "0010",
                            "SkuType": "full",
                            "Properties": {"IsTrial": False},
                        },
                        "Availabilities": list(offers or [availability()]),
                    }
                ],
            }
        ]
    }


def quote(price="25", regular="25"):
    return Quote(
        "9PF8WT09D2NT",
        "Vivid Player",
        "CN",
        "CNY",
        Decimal(price),
        Decimal(regular),
        NOW,
        "offer-1",
        None,
    )


class CatalogTests(unittest.TestCase):
    def test_rejects_foreign_offer_even_when_product_supports_cn(self):
        with self.assertRaises(PriceError):
            parse_catalog(catalog(availability(Markets=["US"])), config(), NOW)

    def test_missing_localization_becomes_a_reportable_source_error(self):
        payload = catalog()
        payload["Products"][0]["LocalizedProperties"] = []
        with self.assertRaises(PriceError):
            parse_catalog(payload, config(), NOW)

    def test_reads_current_mainland_full_version_price(self):
        actual = parse_catalog(catalog(), config(), NOW)
        self.assertEqual((actual.price, actual.currency), (Decimal("25"), "CNY"))

    def test_trial_and_free_license_do_not_become_free_sale(self):
        payload = catalog(availability(), availability("0", "0", Actions=["License", "Browse"]))
        payload["Products"][0]["DisplaySkuAvailabilities"].append(
            {
                "Sku": {"SkuId": "0011", "SkuType": "trial", "Properties": {"IsTrial": True}},
                "Availabilities": [availability("0", "0")],
            }
        )
        self.assertEqual(parse_catalog(payload, config(), NOW).price, Decimal("25"))

    def test_free_full_version_is_valid(self):
        self.assertEqual(parse_catalog(catalog(availability("0", "25")), config(), NOW).price, 0)

    def test_uses_active_desktop_offer_only(self):
        expired = availability("1", "25")
        expired["Conditions"]["EndDate"] = "2026-11-05T00:00:00Z"
        future = availability("2", "25")
        future["Conditions"]["StartDate"] = "2026-11-06T00:00:00Z"
        xbox = availability("3", "25")
        xbox["Conditions"]["ClientConditions"]["AllowedPlatforms"][0]["PlatformName"] = (
            "Windows.Xbox"
        )
        self.assertEqual(
            parse_catalog(catalog(expired, future, xbox, availability()), config(), NOW).price, 25
        )

    def test_ignores_other_currencies_and_entitlement_only_offers(self):
        usd = availability("1", "25")
        usd["OrderManagementData"]["Price"]["CurrencyCode"] = "USD"
        member = availability("5", "25")
        member["Conditions"]["BeneficiaryHasEntitlementKeys"] = ["members-only"]
        self.assertEqual(
            parse_catalog(catalog(usd, member, availability()), config(), NOW).price, 25
        )

    def test_unknown_product_missing_prices_and_invalid_numbers_fail_closed(self):
        for value in (None, "NaN", "Infinity", "-1", True):
            with self.subTest(price=value), self.assertRaises(PriceError):
                parse_catalog(catalog(availability(value)), config(), NOW)
        with self.assertRaises(PriceError):
            parse_catalog({"Products": []}, config(), NOW)
        with self.assertRaises(PriceError):
            parse_catalog(catalog(availability(Actions=["License"])), config(), NOW)

    def test_does_not_select_another_edition(self):
        payload = catalog()
        another = copy.deepcopy(payload["Products"][0]["DisplaySkuAvailabilities"][0])
        another["Sku"]["SkuId"] = "0020"
        another["Availabilities"][0]["OrderManagementData"]["Price"]["ListPrice"] = 1
        payload["Products"][0]["DisplaySkuAvailabilities"].append(another)
        self.assertEqual(parse_catalog(payload, config(), NOW).price, 25)

    def test_rejects_wrong_market_and_malformed_dates(self):
        payload = catalog()
        payload["Products"][0]["MarketProperties"][0]["Markets"] = ["US"]
        with self.assertRaises(PriceError):
            parse_catalog(payload, config(), NOW)
        payload = catalog()
        payload["Products"][0]["DisplaySkuAvailabilities"][0]["Availabilities"][0]["Conditions"][
            "EndDate"
        ] = "bad"
        with self.assertRaises(PriceError):
            parse_catalog(payload, config(), NOW)


class DecisionTests(unittest.TestCase):
    def test_configurable_threshold_uses_validated_decimal(self):
        custom = Config.from_dict({**vars(config()), "alert_below": "18.50"})
        self.assertEqual(custom.alert_below, Decimal("18.50"))
        self.assertFalse(decide({}, quote("18.50"), custom)[1])
        self.assertTrue(decide({}, quote("18.49"), custom)[1])
        for invalid in (None, True, "NaN", "Infinity", "-1"):
            with self.subTest(value=invalid), self.assertRaises(PriceError):
                Config.from_dict({**vars(config()), "alert_below": invalid})

    def test_regular_first_check_and_unchanged_checks_stay_quiet(self):
        state, alert = decide({}, quote(), config())
        self.assertFalse(alert)
        self.assertEqual(state["last_quote"]["price"], "25")
        self.assertFalse(decide(state, quote(), config())[1])

    def test_first_check_already_on_sale_alerts(self):
        self.assertTrue(decide({}, quote("15", "25"), config())[1])

    def test_lower_price_alerts_once_and_further_drop_alerts(self):
        state, _ = decide({}, quote(), config())
        state, alert = decide(state, quote("15", "25"), config())
        self.assertTrue(alert)
        self.assertFalse(decide(state, quote("15", "25"), config())[1])
        state, alert = decide(state, quote("10", "25"), config())
        self.assertTrue(alert)
        self.assertEqual(state["event_number"], 2)

    def test_price_rebound_within_same_sale_does_not_repeat(self):
        state, _ = decide({}, quote("10", "25"), config())
        state, alert = decide(state, quote("15", "25"), config())
        self.assertFalse(alert)
        self.assertFalse(decide(state, quote("10", "25"), config())[1])

    def test_return_to_regular_then_new_sale_notifies_again(self):
        state, _ = decide({}, quote("15", "25"), config())
        state, alert = decide(state, quote(), config())
        self.assertFalse(alert)
        self.assertTrue(decide(state, quote("15", "25"), config())[1])

    def test_only_prices_strictly_below_twenty_notify(self):
        for price in ("0", "19.99", "20", "20.01", "24", "25"):
            with self.subTest(price=price):
                self.assertEqual(
                    decide({}, quote(price), config())[1], Decimal(price) < Decimal("20")
                )

    def test_price_below_threshold_counts_without_strikethrough(self):
        self.assertTrue(decide({}, quote("19", "19"), config())[1])

    def test_return_to_threshold_then_new_low_price_notifies_again(self):
        state, _ = decide({}, quote("19"), config())
        state, alert = decide(state, quote("20"), config())
        self.assertFalse(alert)
        self.assertTrue(decide(state, quote("19"), config())[1])

    def test_legacy_above_threshold_alert_does_not_block_eligible_price(self):
        state, _ = decide({}, quote(), config())
        state.update(sale_active=True, last_alert_price="22", event_number=1)
        self.assertFalse(decide(state, quote("21"), config())[1])
        self.assertTrue(decide(state, quote("19"), config())[1])

    def test_legacy_sale_above_threshold_starts_a_new_low_price_period(self):
        state, _ = decide({}, quote("22"), config())
        state.update(sale_active=True, last_alert_price="15", event_number=2)
        updated, alert = decide(state, quote("19"), config())
        self.assertTrue(alert)
        self.assertEqual(updated["event_number"], 3)

    def test_price_reduction_above_threshold_stays_quiet(self):
        state, _ = decide({}, quote("35", "35"), config())
        state, alert = decide(state, quote("30", "30"), config())
        self.assertFalse(alert)
        self.assertFalse(decide(state, quote("30", "30"), config())[1])

    def test_state_does_not_mutate_input_and_history_is_bounded(self):
        state, _ = decide({}, quote(), config())
        original = copy.deepcopy(state)
        decide(state, quote("15", "25"), config())
        self.assertEqual(state, original)
        for _ in range(200):
            state, _ = decide(state, quote(), config())
        self.assertEqual(len(state["history"]), 180)


class GitHubTests(unittest.TestCase):
    @patch("monitor.subprocess.run")
    def test_uses_gh_cli_without_shell_or_token_on_command_line(self, run):
        run.return_value = Mock(returncode=0, stdout='{"number": 3}', stderr="")
        client = GitHub("coyomii/example")
        self.assertEqual(
            client.api("repos/coyomii/example/issues", "POST", {"title": "折扣"})["number"], 3
        )
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], "gh")
        self.assertNotIn("shell", kwargs)
        self.assertEqual(json.loads(kwargs["input"])["title"], "折扣")

    @patch("monitor.subprocess.run")
    def test_only_404_is_treated_as_missing(self, run):
        client = GitHub("coyomii/example")
        run.return_value = Mock(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")
        self.assertIsNone(client.api("missing", missing_ok=True))
        run.return_value = Mock(returncode=1, stdout="", stderr="gh: forbidden (HTTP 403)")
        with self.assertRaises(GitHubError):
            client.api("forbidden", missing_ok=True)

    def test_loads_remote_state_and_preserves_content_sha(self):
        client = GitHub("coyomii/example")
        state, _ = decide({}, quote(), config())
        client.api = Mock(
            return_value={
                "sha": "original-sha",
                "content": base64.b64encode(json.dumps(state).encode()).decode(),
            }
        )
        self.assertEqual(client.load_state(config()), (state, "original-sha"))

    def test_missing_state_bootstraps_but_corruption_never_silently_resets(self):
        client = GitHub("coyomii/example")
        client.api = Mock(return_value=None)
        self.assertEqual(client.load_state(config()), ({}, None))
        client.api.return_value = {"sha": "sha", "content": "e30="}
        with self.assertRaises(GitHubError):
            client.load_state(config())

    def test_state_is_written_to_dedicated_branch_with_optimistic_lock(self):
        client = GitHub("coyomii/example")
        client.api = Mock(return_value={"ref": "refs/heads/watch-state"})
        state, _ = decide({}, quote(), config())
        client.save_state(config(), state, "old-sha")
        _, method, body = client.api.call_args.args
        self.assertEqual(method, "PUT")
        self.assertEqual((body["branch"], body["sha"]), ("watch-state", "old-sha"))
        self.assertEqual(json.loads(base64.b64decode(body["content"])), state)

    def test_creates_state_branch_from_default_branch_on_first_run(self):
        client = GitHub("coyomii/example")
        client.api = Mock(
            side_effect=[
                None,
                {"default_branch": "main"},
                {"object": {"sha": "initial-sha"}},
                {},
                {},
            ]
        )
        state, _ = decide({}, quote(), config())
        client.save_state(config(), state, None)
        self.assertEqual(
            client.api.call_args_list[-2].args[2],
            {"ref": "refs/heads/watch-state", "sha": "initial-sha"},
        )
        self.assertNotIn("sha", client.api.call_args.args[2])

    def test_closed_alert_can_be_found_without_search_index_delay(self):
        client = GitHub("coyomii/example")
        client.api = Mock(
            side_effect=[
                [{"body": "other", "number": i} for i in range(100)],
                [{"body": "<!-- marker -->", "number": 101, "state": "closed"}],
            ]
        )
        self.assertEqual(client.find_issue("<!-- marker -->")["number"], 101)
        self.assertEqual(client.api.call_count, 2)

    @patch("monitor.time.sleep")
    @patch("monitor.subprocess.run")
    def test_transient_github_reads_retry_but_issue_writes_do_not(self, run, sleep):
        client = GitHub("coyomii/example")
        bad = Mock(returncode=1, stdout="", stderr="HTTP 503")
        good = Mock(returncode=0, stdout="{}", stderr="")
        run.side_effect = [bad, good]
        self.assertEqual(client.api("example"), {})
        self.assertEqual(sleep.call_count, 1)
        run.side_effect = [bad]
        with self.assertRaises(GitHubError):
            client.api("example", "POST", {})


class FetchTests(unittest.TestCase):
    @patch("monitor.parse_catalog", return_value=quote())
    @patch("monitor.time.sleep")
    @patch("monitor.urlopen")
    def test_network_retry_then_valid_response(self, urlopen, sleep, parse):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"Products": []}'
        urlopen.side_effect = [URLError("temporary"), response]
        self.assertEqual(fetch_quote(config()).price, 25)
        self.assertEqual(sleep.call_count, 1)
        self.assertIn("market=CN", urlopen.call_args.args[0].full_url)

    @patch("monitor.time.sleep")
    @patch("monitor.urlopen", side_effect=URLError("offline"))
    def test_repeated_connection_failure_is_not_a_zero_price(self, urlopen, sleep):
        with self.assertRaises(PriceError):
            fetch_quote(config())
        self.assertEqual(urlopen.call_count, 3)

    @patch("monitor.time.sleep")
    @patch("monitor.urlopen")
    def test_forbidden_fails_immediately_and_server_error_retries(self, urlopen, sleep):
        urlopen.side_effect = HTTPError("https://example.com", 403, "forbidden", {}, None)
        with self.assertRaises(PriceError):
            fetch_quote(config())
        self.assertEqual(urlopen.call_count, 1)
        urlopen.reset_mock()
        urlopen.side_effect = HTTPError("https://example.com", 503, "unavailable", {}, None)
        with self.assertRaises(PriceError):
            fetch_quote(config())
        self.assertEqual(urlopen.call_count, 3)

    @patch("monitor.urlopen")
    def test_html_error_page_is_not_a_price(self, urlopen):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"<html>try later</html>"
        urlopen.return_value = response
        with self.assertRaises(PriceError):
            fetch_quote(config())


class RunTests(unittest.TestCase):
    def backend(self, state=None):
        backend = Mock()
        backend.load_state.return_value = (state or {}, None)
        backend.find_issue.return_value = None
        backend.create_issue.return_value = {
            "number": 1,
            "html_url": "https://github.com/example/issues/1",
        }
        return backend

    def test_normal_check_persists_state_without_notification(self):
        backend = self.backend()
        run_monitor(config(), backend, fetch=lambda _: quote())
        backend.save_state.assert_called_once()
        backend.create_issue.assert_not_called()

    def test_discount_creates_assigned_issue(self):
        backend = self.backend()
        run_monitor(config(), backend, fetch=lambda _: quote("15", "25"))
        title, body, assignee = backend.create_issue.call_args.args
        self.assertIn("15", title)
        self.assertIn("@coyomii", body)
        self.assertEqual(assignee, "coyomii")

    def test_existing_notification_prevents_duplicate_after_failed_state_save(self):
        self.recover_failed_save("15")

    def test_failed_state_save_then_higher_sale_price_does_not_notify_again(self):
        self.recover_failed_save("18")

    def recover_failed_save(self, next_price):
        backend = self.backend()
        issues = []

        def create(title, body, assignee):
            issue = {"number": len(issues) + 1, "body": body}
            issues.append(issue)
            return issue

        backend.create_issue.side_effect = create
        backend.find_issue.side_effect = lambda marker, **kwargs: next(
            (issue for issue in issues if marker in issue["body"]), None
        )
        backend.save_state.side_effect = GitHubError("write failed after successful notification")
        with self.assertRaises(GitHubError):
            run_monitor(config(), backend, fetch=lambda _: quote("15", "25"))
        backend.save_state.side_effect = None
        run_monitor(config(), backend, fetch=lambda _: quote(next_price, "25"))
        self.assertEqual(len(issues), 1)
        self.assertEqual(backend.save_state.call_args.args[1]["last_alert_price"], "15")

    def test_failed_notification_does_not_mark_discount_as_sent(self):
        backend = self.backend()
        backend.create_issue.side_effect = GitHubError("permission denied")
        with self.assertRaises(GitHubError):
            run_monitor(config(), backend, fetch=lambda _: quote("15", "25"))
        backend.save_state.assert_not_called()

    def test_bad_source_raises_without_sending_notification(self):
        backend = self.backend()
        fetch = Mock(side_effect=PriceError("no full-version purchase price"))
        with self.assertRaises(PriceError):
            run_monitor(config(), backend, fetch=fetch)
        backend.create_issue.assert_not_called()
        backend.save_state.assert_not_called()

    def test_repeated_error_does_not_create_repeated_issue(self):
        backend = self.backend()
        backend.find_issue.return_value = {"number": 9}
        with self.assertRaises(PriceError):
            run_monitor(config(), backend, fetch=Mock(side_effect=PriceError("offline")))
        backend.create_issue.assert_not_called()

    def test_recovery_above_threshold_does_not_comment_on_old_error_issue(self):
        backend = self.backend()
        backend.find_issue.side_effect = lambda marker, **kwargs: (
            {"number": 7} if "vivid-watch-error:" in marker else None
        )
        run_monitor(config(), backend, fetch=lambda _: quote())
        backend.close_issue.assert_not_called()
        backend.create_issue.assert_not_called()

    def test_discount_at_or_above_threshold_creates_no_issue(self):
        for price in ("20", "22", "25"):
            with self.subTest(price=price):
                backend = self.backend()
                run_monitor(config(), backend, fetch=Mock(return_value=quote(price)))
                backend.save_state.assert_called_once()
                backend.create_issue.assert_not_called()

    def test_manual_notification_is_explicitly_marked_as_test(self):
        backend = self.backend()
        run_monitor(config(), backend, fetch=lambda _: quote(), test_notification=True)
        title, body, _ = backend.create_issue.call_args.args
        self.assertIn("通知测试", title)
        self.assertIn("不代表当前正在打折", body)


class CliTests(unittest.TestCase):
    @patch("monitor.GitHub")
    @patch("monitor.fetch_quote", return_value=quote())
    @patch("sys.argv", ["monitor.py", "--dry-run"])
    def test_dry_run_has_no_github_side_effects_and_writes_summary(self, fetch, github):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.md"
            with (
                patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(path)}),
                patch("sys.stdout", io.StringIO()),
            ):
                self.assertEqual(main(), 0)
            self.assertIn("25.00", path.read_text(encoding="utf-8"))
        github.assert_not_called()

    @patch("monitor.run_monitor", return_value=(quote("15", "25"), True))
    @patch("sys.argv", ["monitor.py", "--repo", "coyomii/example"])
    def test_cli_uses_repository_argument(self, run):
        with patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertEqual(run.call_args.args[1].base, "repos/coyomii/example")

    @patch("sys.argv", ["monitor.py", "--config", "does-not-exist.json"])
    def test_configuration_errors_exit_nonzero(self):
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(), 1)


if __name__ == "__main__":
    unittest.main()
