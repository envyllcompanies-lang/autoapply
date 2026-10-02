"""The bot stops at a human check. It never solves one, works around one, or hands one to a solving service.

This check fails if code for a CAPTCHA-solving service ever appears in the bot, and it confirms that a page showing a
human check ends the application. Run: python tests/captcha_boundary.py"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SOLVERS = re.compile(r"2captcha|twocaptcha|anti-?captcha|capsolver|capmonster|deathbycaptcha|nopecha|buster|"
                     r"solve_?captcha|captcha_?solv|recaptcha_?token|turnstile_?token|h-captcha-response|"
                     r"playwright[-_]stealth|undetected[-_]chromedriver|puppeteer-extra", re.I)


def main() -> int:
    problems = []
    for f in sorted((ROOT / "autoapply").glob("**/*.py")) + [ROOT / "settings.yaml", ROOT / "requirements.txt"]:
        if not f.exists():
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = SOLVERS.search(line)
            if m:
                problems.append(f"{f.relative_to(ROOT)} line {n}: mentions '{m.group(0)}' (no CAPTCHA solving or anti-detection code belongs in the bot)")
    try:
        from playwright.sync_api import sync_playwright
        from autoapply import submit as S
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page()
            for frame, name in (("https://www.google.com/recaptcha/api2/anchor?k=x", "reCAPTCHA"),
                                ("https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html", "hCaptcha"),
                                ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/x", "Turnstile")):
                page.set_content(f'<form><input name="a"><iframe src="{frame}" width="304" height="78"></iframe><button type="submit">Submit</button></form>',
                                 wait_until="commit")
                got = S._blocker(page) or ""
                if name not in got or "the bot stops here" not in got:
                    problems.append(f"a page showing {name} did not stop the application (got {got!r})")
            b.close()
    except ImportError:
        print("skip browser part (no playwright)")
    if problems:
        print("PROBLEMS:\n  - " + "\n  - ".join(problems))
        return 1
    print("ok  human checks stop the application; no CAPTCHA-solving or anti-detection code in the bot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
