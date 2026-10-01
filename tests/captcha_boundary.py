"""Safety checks for the CAPTCHA provider boundary.

These checks intentionally verify that the configured 2Captcha adapter never performs
challenge solving, polling, or token injection.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autoapply.captcha import (  # noqa: E402
    CaptchaChallenge,
    CaptchaDisabled,
    TwoCaptchaProvider,
    provider_from_settings,
)


def main():
    old = os.environ.get("TWOCAPTCHA_API_KEY")
    os.environ["TWOCAPTCHA_API_KEY"] = "test-only-placeholder"

    try:
        provider = provider_from_settings({
            "captcha": {
                "provider": "twocaptcha",
                "api_key_env": "TWOCAPTCHA_API_KEY",
            }
        })
        assert isinstance(provider, TwoCaptchaProvider)
        assert provider.configured

        challenge = CaptchaChallenge(
            kind="recaptcha_v2",
            site_key="test-site-key",
            page_url="https://example.test/apply",
        )
        try:
            provider.solve(challenge)
        except CaptchaDisabled:
            pass
        else:
            raise AssertionError("2Captcha adapter must remain non-operational")

        print("ok  2Captcha adapter is configured for metadata only and cannot solve challenges")
    finally:
        if old is None:
            os.environ.pop("TWOCAPTCHA_API_KEY", None)
        else:
            os.environ["TWOCAPTCHA_API_KEY"] = old


if __name__ == "__main__":
    main()
