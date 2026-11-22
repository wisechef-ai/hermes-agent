"""Z.AI quota-wall 429 (code 1310) must fail over, never ride the retry ladder.

Observed on Chef 2026-11-22: loopskill/cron-watchdog died every 30 min on
``Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-11-24_10:00:35``.
The body names a DATE two days out, not a delay, so waiting is pointless.
"""

from types import SimpleNamespace

import pytest

from agent.error_classifier import FailoverReason, classify_api_error

ZAI_1310_BODY = {
    "error": {
        "code": "1310",
        "message": "Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-11-24_10:00:35",
    }
}
ZAI_1310_TEXT = f"Error code: 429 - {ZAI_1310_BODY}"


class _Err(Exception):
    def __init__(self, message, status_code=429, body=None, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body if body is not None else {}
        self.response = SimpleNamespace(headers=headers or {})


def _classify(err):
    return classify_api_error(err, provider="zai", model="glm-5.3-flash")


class TestZaiQuotaWall:
    def test_exact_observed_body_is_billing_and_fails_over(self):
        result = _classify(_Err(ZAI_1310_TEXT, body=ZAI_1310_BODY))
        assert result.reason == FailoverReason.billing
        assert result.should_fallback is True
        assert result.retryable is False

    def test_code_1310_alone_is_billing(self):
        body = {"error": {"code": "1310", "message": "quota window spent"}}
        result = _classify(_Err("Error code: 429", body=body))
        assert result.reason == FailoverReason.billing

    @pytest.mark.parametrize("msg", [
        "Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-11-24_10:00:35",
        "weekly/monthly limit reached",
        "Daily Limit Exhausted",
    ])
    def test_message_phrases_without_code_are_billing(self, msg):
        assert _classify(_Err(f"Error code: 429 - {msg}")).reason == FailoverReason.billing

    def test_no_body_dict_still_matches_on_text(self):
        # SDK errors sometimes arrive with only the flattened text.
        assert _classify(_Err(ZAI_1310_TEXT, body=None)).reason == FailoverReason.billing

    # ── boundary: things that must NOT become billing ──
    def test_short_wait_exhausted_stays_rate_limit(self):
        e = _Err("Error code: 429 - Rate limit exhausted, retry after 5s")
        assert _classify(e).reason == FailoverReason.rate_limit

    def test_short_wait_header_with_1310_stays_rate_limit(self):
        # A structured short retry window beats the code: a throttle, not a wall.
        body = {"error": {"code": "1310", "message": "slow down"}}
        e = _Err("Error code: 429", body=body, headers={"retry-after": "30"})
        assert _classify(e).reason == FailoverReason.rate_limit

    @pytest.mark.parametrize("seconds,reason", [
        ("899", FailoverReason.rate_limit),
        ("900", FailoverReason.billing),
    ])
    def test_wall_threshold_is_900_seconds(self, seconds, reason):
        body = {"error": {"code": "1310", "message": "Weekly/Monthly Limit Exhausted"}}
        e = _Err("Error code: 429", body=body, headers={"retry-after": seconds})
        assert _classify(e).reason == reason

    @pytest.mark.parametrize("phrase", [
        "quota exhausted", "quota has been exhausted", "limit exhausted", "weekly/monthly",
    ])
    def test_each_wall_phrase_is_billing(self, phrase):
        assert _classify(_Err(f"Error code: 429 - {phrase}")).reason == FailoverReason.billing

    @pytest.mark.parametrize("phrase", [
        "quota exhausted", "quota has been exhausted", "limit exhausted", "weekly/monthly",
    ])
    def test_each_wall_phrase_with_far_reset_date_is_billing(self, phrase):
        # The reset signal ("resets at") alone must not turn a spent window into a throttle.
        msg = f"Error code: 429 - {phrase}. Window resets at 2026-11-24 10:00:35"
        assert _classify(_Err(msg)).reason == FailoverReason.billing

    def test_other_zai_code_stays_rate_limit(self):
        body = {"error": {"code": "1302", "message": "High concurrency, slow down"}}
        assert _classify(_Err("Error code: 429", body=body)).reason == FailoverReason.rate_limit

    def test_overload_1305_is_untouched(self):
        body = {"error": {"code": "1305", "message": "The service may be temporarily overloaded"}}
        assert _classify(_Err("Error code: 429", body=body)).reason == FailoverReason.overloaded

    def test_plain_429_stays_rate_limit(self):
        assert _classify(_Err("Too Many Requests")).reason == FailoverReason.rate_limit
