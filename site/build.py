import hashlib
import json
import re
import shutil
from pathlib import Path

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent
ASSETS = SITE / "assets"
SRC = ROOT / "templates" / "landing_home.html"
REF = re.compile(r"\{\{\s*'(/static/[^']+)'\s*\|\s*bust\s*\}\}")


def publish(static_path: str) -> str:
    src = ROOT / static_path.lstrip("/")
    rel = Path(static_path).relative_to("/static")
    if rel.parts[0] == "images":
        rel = Path(*rel.parts[2:]) if rel.parts[1] == "landing" else Path(*rel.parts[1:])
    dst = ASSETS / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:10]
    return f"/{rel.as_posix()}?v={digest}"


def main() -> None:
    html = SRC.read_text(encoding="utf-8")
    html = REF.sub(lambda m: publish(m.group(1)), html)
    left = re.findall(r"\{\{|\{%", html)
    if left:
        raise SystemExit(f"unrendered template tags left: {len(left)}")
    worker = (
        f"const HTML = {json.dumps(html, ensure_ascii=False)};\n\n"
        "export default {\n"
        "  async fetch(request, env) {\n"
        "    const path = new URL(request.url).pathname;\n"
        '    if (path !== "/" && env.ASSETS) {\n'
        "      const asset = await env.ASSETS.fetch(request);\n"
        "      if (asset.status !== 404) return asset;\n"
        "    }\n"
        "    return new Response(HTML, {\n"
        "      headers: {\n"
        '        "content-type": "text/html; charset=UTF-8",\n'
        '        "cache-control": "public, max-age=300",\n'
        '        "x-content-type-options": "nosniff",\n'
        '        "referrer-policy": "strict-origin-when-cross-origin",\n'
        "      },\n"
        "    });\n"
        "  },\n"
        "};\n"
    )
    (SITE / "worker.js").write_text(worker, encoding="utf-8")
    print(f"worker.js {len(worker) // 1024} KB, assets in {ASSETS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
