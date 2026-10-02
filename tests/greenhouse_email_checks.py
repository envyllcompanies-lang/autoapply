"""Regression checks for Greenhouse application email verification correlation."""
from __future__ import annotations

import os

from autoapply import mailbox, submit


def check(ok: bool, msg: str):
    if not ok:
        raise AssertionError(msg)


def main():
    old_recent = mailbox._recent
    old_disabled = mailbox._DISABLED
    old_sleep = mailbox.time.sleep
    try:
        mailbox._DISABLED = ""
        mailbox.time.sleep = lambda _seconds: None

        messages = [
            {
                "from": "no-reply@us.greenhouse-mail.io",
                "subject": "Your verification code",
                "body": "Use code 731204 to continue your application with Acme.",
                "code": "731204",
                "link": None,
                "hint": True,
                "ts": 2000,
            },
            {
                "from": "no-reply@greenhouse.io",
                "subject": "Your verification code",
                "body": "Use code 999999 to continue your application with OtherCo.",
                "code": "999999",
                "link": None,
                "hint": True,
                "ts": 2000,
            },
            {
                "from": "attacker@example.com",
                "subject": "Your verification code",
                "body": "Use code 123456.",
                "code": "123456",
                "link": None,
                "hint": True,
                "ts": 2000,
            },
        ]
        mailbox._recent = lambda *args, **kwargs: iter(messages)
        got = mailbox.wait_for_greenhouse_code(
            since_ts=1900,
            company_hint="Acme",
            job_hint="Treasury Operations Associate",
            timeout=1,
            log=lambda *_: None,
        )
        check(got and got["code"] == "731204" and got["provider"] == "greenhouse",
              f"wrong Greenhouse/provider correlation: {got!r}")

        messages[:] = [{
            "from": "no-reply@greenhouse.io",
            "subject": "Thanks for applying",
            "body": "Your application was received. Reference code 2026.",
            "code": "2026",
            "link": None,
            "hint": True,
            "ts": 2000,
        }]
        got = mailbox.wait_for_greenhouse_code(
            since_ts=1900, company_hint="Acme", timeout=1, log=lambda *_: None
        )
        check(got is None, f"confirmation/year was misread as verification code: {got!r}")

        messages[:] = [{
            "from": "no-reply@greenhouse.io",
            "subject": "Security code",
            "body": "Your security code is X7K2QF for Acme.",
            "code": "X7K2QF",
            "link": None,
            "hint": True,
            "ts": 2000,
        }]
        got = mailbox.wait_for_greenhouse_code(
            since_ts=1900, company_hint="Acme", timeout=1, log=lambda *_: None
        )
        check(got and got["code"] == "X7K2QF", f"alphanumeric Greenhouse code failed: {got!r}")

        greenhouse_screen = (
            "Code Requested 9104A104 Security code for your application to Charlie Health. "
            "Copy and paste this code into the security code field on your application."
        )
        check(submit.EMAIL_CODE_RE.search(greenhouse_screen) is not None,
              "Greenhouse security-code page wording was not recognized")

        print("ok  greenhouse email: provider, freshness, employer correlation, and numeric/alphanumeric code filtering")
    finally:
        mailbox._recent = old_recent
        mailbox._DISABLED = old_disabled
        mailbox.time.sleep = old_sleep


if __name__ == "__main__":
    main()
