# Fonts — FiroGate

All fonts used in FiroGate are served locally. No external font requests are made.

## Included Fonts

| Font | License | Usage |
|------|---------|-------|
| Inter | SIL Open Font License | UI — Latin characters |
| JetBrains Mono | SIL Open Font License | Code blocks, monospace |
| Noto Sans Arabic | SIL Open Font License | Arabic RTL support |

## Why Local Fonts?

- No requests to Google Fonts or any external CDN
- Works on Tor hidden services without leaking DNS
- Faster load time — fonts served from your own server
- No privacy concerns for your users

## Adding Fonts

Place `.woff2` files in this directory and add `@font-face` declarations to `static/css/main.css`.

Only use fonts with open licenses (SIL OFL, Apache 2.0, etc.).
