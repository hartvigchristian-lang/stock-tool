"""Build the encrypted static site for GitHub Pages.

Runs on GitHub Actions (or locally to test):
  1. decrypts private/bundle.enc (config, portfolios, bank exports) into this folder,
  2. computes every portfolio with live prices, exactly like the local server,
  3. writes site/index.html and one encrypted file per portfolio in site/data/.

Password: env PORTFOLIO_PASSWORD.  Usage: python build_static.py [--no-unpack]
"""
import io
import shutil
import json
import os
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import crypto_box  # noqa: E402


def unpack(password):
    blob = (HERE / "private" / "bundle.enc").read_bytes()
    with tarfile.open(fileobj=io.BytesIO(crypto_box.decrypt(blob, password)), mode="r:gz") as tar:
        for m in tar.getmembers():
            target = (HERE / m.name).resolve()
            if not str(target).startswith(str(HERE)) or not (m.isfile() or m.isdir()):
                raise ValueError(f"Unexpected entry in bundle: {m.name}")
        tar.extractall(HERE)


def main():
    password = os.environ.get("PORTFOLIO_PASSWORD")
    if not password:
        sys.exit("Set PORTFOLIO_PASSWORD.")
    if "--no-unpack" not in sys.argv:
        unpack(password)

    import portfolio  # after unpacking, so config.json is there
    site = HERE / "site"
    (site / "data").mkdir(parents=True, exist_ok=True)

    html = (HERE / "dashboard.html").read_text(encoding="utf-8")
    html = html.replace("<head>", "<head>\n<script>window.STATIC = true;</script>", 1)
    (site / "index.html").write_text(html, encoding="utf-8")
    (site / ".nojekyll").write_text("")
    shutil.copy(HERE / "manifest.json", site / "manifest.json")
    shutil.copytree(HERE / "icons", site / "icons", dirs_exist_ok=True)

    # Who can open what: the main password opens every portfolio; a portfolio can also name an
    # env var with its own password (config.json → portfolios → id → "password_env"), which
    # opens only that portfolio.
    cfg = portfolio.load_config()
    main_kek = crypto_box.make_kek(password)
    ids = []
    for p in portfolio.portfolio_list(cfg):
        t0 = time.time()
        keks = [main_kek]
        env = cfg.get("portfolios", {}).get(p["id"], {}).get("password_env")
        if env and os.environ.get(env):
            keks.append(crypto_box.make_kek(os.environ[env]))
        elif env:
            print(f"  note: {env} not set, so only the main password opens {p['label']}")
        snap = portfolio.snapshot(True, (), p["id"])
        snap["static"] = True
        snap["label"] = p["label"]
        snap["portfolios"] = [p]   # the browser lists only the portfolios a password opens
        data = json.dumps(snap, default=str).encode()
        (site / "data" / f"{p['id']}.enc").write_bytes(crypto_box.encrypt_for(data, keks))
        ids.append(p["id"])
        print(f"{p['label']}: {len(snap['holdings'])} holdings, {len(snap['warnings'])} warnings, "
              f"{time.time() - t0:.1f}s")
        for w in snap["warnings"]:
            print("  warning:", w)
    (site / "data" / "index.json").write_text(json.dumps({"portfolios": ids}))


if __name__ == "__main__":
    main()
