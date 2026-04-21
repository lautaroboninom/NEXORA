from __future__ import annotations

import argparse
import importlib.util
import time
import unicodedata
from pathlib import Path

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTENT_PATH = REPO_ROOT / "docs" / "comercial" / "content.py"
DEFAULT_BASE_URL = "http://localhost:5175"
DEFAULT_API_URL = "http://localhost:18100"
DEFAULT_EMAIL = "nexora.pdf@local"
DEFAULT_PASSWORD = "NexoraPdf#2026"
DEFAULT_WINDOW = (1600, 1040)


def load_content():
    spec = importlib.util.spec_from_file_location("nexora_content", CONTENT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No se pudo cargar {CONTENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_driver(binary_location: str | None) -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=%d,%d" % DEFAULT_WINDOW)
    opts.add_argument("--hide-scrollbars")
    opts.add_argument("--force-device-scale-factor=1.25")
    opts.add_argument("--lang=es-AR")
    if binary_location:
        opts.binary_location = binary_location
    return webdriver.Chrome(options=opts)


def login_session(api_url: str, email: str, password: str) -> requests.Session:
    sess = requests.Session()
    res = sess.post(
        api_url.rstrip("/") + "/api/auth/login/",
        json={"email": email, "password": password},
        timeout=15,
    )
    res.raise_for_status()
    return sess


def wait_for_text(driver: webdriver.Chrome, text: str, timeout: int = 20) -> None:
    needle = _norm_text(text)

    def _matches(drv: webdriver.Chrome) -> bool:
        body = _norm_text(drv.find_element(By.TAG_NAME, "body").text)
        return needle in body

    WebDriverWait(driver, timeout).until(_matches)


def _norm_text(value: str) -> str:
    return (
        unicodedata.normalize("NFD", value or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def inject_cookies(driver: webdriver.Chrome, base_url: str, session: requests.Session) -> None:
    driver.get(base_url.rstrip("/") + "/login")
    for cookie in session.cookies:
        if cookie.name and cookie.value:
            driver.add_cookie(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "path": cookie.path or "/",
                }
            )


def capture(driver: webdriver.Chrome, base_url: str, target: dict, out_path: Path) -> None:
    driver.get(base_url.rstrip("/") + target["route"])
    wait_for_text(driver, target["wait_text"])
    time.sleep(float(target.get("delay", 0.8)))
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.15)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not driver.save_screenshot(str(out_path)):
        raise RuntimeError(f"No se pudo guardar screenshot en {out_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Captura pantallas comerciales de NEXORA desde DEV.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--chrome-binary", default="")
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args(argv)

    content = load_content()
    screenshots = content.SCREENSHOTS
    selected = set(args.only or [])
    if selected:
        missing = sorted(selected - set(screenshots))
        if missing:
            raise SystemExit(f"Capturas no definidas: {', '.join(missing)}")

    session = login_session(args.api_url, args.email, args.password)
    driver = build_driver(args.chrome_binary or None)
    captured: list[Path] = []
    try:
        inject_cookies(driver, args.base_url, session)
        driver.get(args.base_url.rstrip("/") + "/")
        wait_for_text(driver, "Busqueda por N/S o MG")
        time.sleep(0.8)

        for key, target in screenshots.items():
            if selected and key not in selected:
                continue
            out_path = Path(target["file"])
            print(f"[capture] {key} -> {out_path}")
            try:
                capture(driver, args.base_url, target, out_path)
            except TimeoutException as exc:
                raise RuntimeError(f"Timeout esperando '{target['wait_text']}' en {target['route']}") from exc
            captured.append(out_path)
    finally:
        driver.quit()

    print(f"[capture] ok: {len(captured)} archivos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
