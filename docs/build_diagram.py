#!/usr/bin/env python3
"""Build docs/toolgate-ddd.html from docs/toolgate-ddd.mmd + docs/_shell.html.

Inlines the local Mermaid bundle so the page is fully self-contained (works offline,
no CDN).  Run from anywhere:  python3 docs/build_diagram.py
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

BUNDLE_CANDIDATES = [
    ROOT / "node_modules/mermaid/dist/mermaid.min.js",
    pathlib.Path("/home/ubuntu/.dsh/profiles/web/node_modules/mermaid/dist/mermaid.min.js"),
]


def find_bundle() -> pathlib.Path:
    for c in BUNDLE_CANDIDATES:
        if c.exists():
            return c
    sys.exit("mermaid.min.js not found; tried:\n  " + "\n  ".join(map(str, BUNDLE_CANDIDATES)))


def main() -> int:
    shell_path = HERE / "_shell.html"
    src_path = HERE / "toolgate-ddd.mmd"
    out_path = HERE / "toolgate-ddd.html"

    shell = shell_path.read_text()
    src = src_path.read_text()
    bundle_path = find_bundle()
    bundle = bundle_path.read_text()

    if "</script" in src:
        sys.exit("diagram source contains </script — cannot embed safely")

    # Mermaid 11.17's lexer chokes on a comment line that is exactly '%%'
    # ("Parse error on line 1 ... Expecting 'GRAPH'"). Require trailing text.
    bare = [i for i, line in enumerate(src.splitlines(), 1) if line.strip() == "%%"]
    if bare:
        sys.exit(f"bare '%%' comment line(s) at line {bare} — mermaid fails to lex these; "
                 f"add trailing text (e.g. '%% ---')")

    escaped = bundle.count("</script")
    bundle_js = bundle.replace("</script", "<\\/script")

    for token in ("/*__MERMAID_BUNDLE__*/", "__DIAGRAM_SOURCE__"):
        if shell.count(token) != 1:
            sys.exit(f"shell must contain exactly one {token!r} placeholder")

    html = shell.replace("/*__MERMAID_BUNDLE__*/", bundle_js).replace("__DIAGRAM_SOURCE__", src)
    out_path.write_text(html)

    print(f"source : {src_path}  ({len(src):,} bytes, {src.count(chr(10)) + 1} lines)")
    print(f"mermaid: {bundle_path}  ({len(bundle):,} bytes"
          + (f", escaped {escaped} </script)" if escaped else ")"))
    print(f"output : {out_path}  ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
