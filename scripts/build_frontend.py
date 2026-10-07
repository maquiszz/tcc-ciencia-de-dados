"""Gera cópias minificadas do frontend sem substituir os arquivos-fonte."""

from pathlib import Path

import minify_html


ROOT = Path(__file__).resolve().parents[1]
INPUTS = (
    ROOT / "database" / "servicos.html",
    ROOT / "templates" / "index.html",
    ROOT / "templates" / "privacidade.html",
    ROOT / "templates" / "404.html",
)
STYLESHEETS = (ROOT / "static" / "css" / "privacy-notice.css",)
SCRIPTS = (
    ROOT / "static" / "js" / "privacy-notice.js",
    ROOT / "static" / "js" / "registration.js",
)


def main():
    for source in INPUTS:
        output = source.with_name(f"{source.stem}.min{source.suffix}")
        original = source.read_text(encoding="utf-8")
        compact = minify_html.minify(
            original,
            minify_js=True,
            minify_css=True,
            keep_closing_tags=True,
            preserve_brace_template_syntax=True,
            preserve_chevron_percent_template_syntax=True,
        )
        output.write_text(compact, encoding="utf-8", newline="")
        original_bytes = len(original.encode("utf-8"))
        compact_bytes = len(compact.encode("utf-8"))
        reduction = 100 * (1 - compact_bytes / original_bytes) if original_bytes else 0
        print(f"{source.relative_to(ROOT)} -> {output.relative_to(ROOT)}: {original_bytes} -> {compact_bytes} bytes ({reduction:.1f}% menor)")

    for source in STYLESHEETS:
        original = source.read_text(encoding="utf-8")
        wrapper = minify_html.minify(f"<style>{original}</style>", minify_css=True)
        compact = wrapper[len("<style>"):-len("</style>")]
        output = source.with_name(f"{source.stem}.min{source.suffix}")
        output.write_text(compact, encoding="utf-8", newline="")
        original_bytes = len(original.encode("utf-8"))
        compact_bytes = len(compact.encode("utf-8"))
        reduction = 100 * (1 - compact_bytes / original_bytes) if original_bytes else 0
        print(f"{source.relative_to(ROOT)} -> {output.relative_to(ROOT)}: {original_bytes} -> {compact_bytes} bytes ({reduction:.1f}% menor)")

    for source in SCRIPTS:
        original = source.read_text(encoding="utf-8")
        wrapper = minify_html.minify(f"<script>{original}</script>", minify_js=True)
        compact = wrapper[len("<script>"):-len("</script>")]
        output = source.with_name(f"{source.stem}.min{source.suffix}")
        output.write_text(compact, encoding="utf-8", newline="")
        original_bytes = len(original.encode("utf-8"))
        compact_bytes = len(compact.encode("utf-8"))
        reduction = 100 * (1 - compact_bytes / original_bytes) if original_bytes else 0
        print(f"{source.relative_to(ROOT)} -> {output.relative_to(ROOT)}: {original_bytes} -> {compact_bytes} bytes ({reduction:.1f}% menor)")


if __name__ == "__main__":
    main()
