"""Encrypt your private files and push them to GitHub, so the phone version updates.

Run after any change to config.json, portfolios/ or the bank exports in data/:
    .venv/bin/python publish.py            (encrypt + commit + push)
    .venv/bin/python publish.py --no-push  (only write private/bundle.enc)

The password is kept in the macOS Keychain (service "stock-tool"). It must be the same
as the PORTFOLIO_PASSWORD secret on GitHub. Only the encrypted bundle leaves this Mac.
"""
import getpass
import io
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import crypto_box  # noqa: E402

SERVICE = "stock-tool"
PRIVATE = ["config.json", "portfolios", "data"]


def password():
    r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-w"],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.rstrip("\n")
    pw = getpass.getpass("Choose the dashboard password (same as the GitHub secret): ")
    if len(pw) < 10 or pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords differ or shorter than 10 characters.")
    subprocess.run(["security", "add-generic-password", "-s", SERVICE, "-a", getpass.getuser(),
                    "-w", pw, "-U"], check=True)
    print("Saved in the Keychain.")
    return pw


def bundle():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in PRIVATE:
            p = HERE / name
            if p.exists():
                tar.add(p, arcname=name,
                        filter=lambda ti: None if Path(ti.name).name.startswith(".") else ti)
    return buf.getvalue()


def main():
    out = HERE / "private" / "bundle.enc"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(crypto_box.encrypt(bundle(), password()))
    print(f"Wrote {out.relative_to(HERE)}")
    if "--no-push" in sys.argv:
        return
    git = lambda *a: subprocess.run(["git", *a], cwd=HERE, check=True)
    git("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=HERE).returncode == 0:
        print("Nothing changed.")
        return
    git("commit", "-q", "-m", "Update portfolio data")
    git("push", "-q")
    print("Pushed. GitHub rebuilds the phone version in a minute or two.")


if __name__ == "__main__":
    main()
