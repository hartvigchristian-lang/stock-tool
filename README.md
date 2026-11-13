# Stock Tool

A small portfolio dashboard. Runs locally on a Mac (`start.sh`, live prices every 30 s) and,
optionally, as an encrypted phone version on GitHub Pages.

## Phone version (GitHub Pages)

Only code and `private/bundle.enc` are in this repository. The bundle is your config, portfolios
and bank exports, encrypted with AES-256-GCM. A GitHub Action decrypts it with the
`PORTFOLIO_PASSWORD` secret every 15 minutes on weekdays, fetches prices, and publishes one
encrypted file per portfolio. The page asks for the password and decrypts in the browser.

After changing trades or adding a bank export on the Mac:

    .venv/bin/python publish.py

Prices: Yahoo Finance. Not investment advice: the plan section applies generic rules you set in
config.json.
