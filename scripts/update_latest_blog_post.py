from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

START_MARKER = "<!-- BLOG-POST-LIST:START -->"
END_MARKER = "<!-- BLOG-POST-LIST:END -->"
USER_AGENT = "al-husayn-readme-updater/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update the README with the latest post from the blog homepage."
    )
    parser.add_argument(
        "--blog-home",
        default="https://blog.al-husayn.dev/",
        help="Blog homepage URL.",
    )
    parser.add_argument(
        "--readme-path",
        default="README.md",
        help="Path to the profile README.",
    )
    return parser.parse_args()


def fetch_html(url: str) -> str:
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if curl:
        result = subprocess.run(
            [curl, "-fsSL", url],
            check=True,
            capture_output=True,
            text=False,
            timeout=30,
        )
        return result.stdout.decode("utf-8", errors="replace")

    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def iter_json_ld_documents(html: str) -> list[Any]:
    matches = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    documents: list[Any] = []
    for raw in matches:
        payload = raw.strip()
        if not payload:
            continue
        try:
            documents.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return documents


def walk(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        nodes = [node]
        for value in node.values():
            nodes.extend(walk(value))
        return nodes
    if isinstance(node, list):
        nodes: list[dict[str, Any]] = []
        for item in node:
            nodes.extend(walk(item))
        return nodes
    return []


def collect_posts(html: str) -> list[dict[str, str]]:
    posts: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for document in iter_json_ld_documents(html):
        for node in walk(document):
            node_type = node.get("@type")
            if node_type == "Blog" and isinstance(node.get("blogPost"), list):
                candidates = node["blogPost"]
            elif node_type == "BlogPosting":
                candidates = [node]
            else:
                continue

            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue

                title = candidate.get("headline")
                url = candidate.get("url")
                published = candidate.get("datePublished", "")

                if not title or not url or url in seen_urls:
                    continue

                posts.append(
                    {
                        "title": title.strip(),
                        "url": url.strip(),
                        "published": str(published).strip(),
                    }
                )
                seen_urls.add(url)

    return posts


def format_date(raw_date: str) -> str:
    parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def build_latest_post_line(posts: list[dict[str, str]]) -> str:
    if not posts:
        raise RuntimeError("No blog posts were found in the homepage structured data.")

    latest_post = max(posts, key=lambda post: post.get("published", ""))
    published = latest_post.get("published")
    suffix = f" - {format_date(published)}" if published else ""
    return f"- [{latest_post['title']}]({latest_post['url']}){suffix}"


def update_readme(readme_path: Path, latest_post_line: str) -> None:
    readme = readme_path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in readme else "\n"
    replacement = newline.join([START_MARKER, latest_post_line, END_MARKER])
    pattern = re.compile(
        rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}",
        flags=re.DOTALL,
    )

    if not pattern.search(readme):
        raise RuntimeError("README blog markers were not found.")

    updated_readme = pattern.sub(replacement, readme, count=1)
    if updated_readme != readme:
        readme_path.write_text(updated_readme, encoding="utf-8")


def main() -> None:
    args = parse_args()
    readme_path = Path(args.readme_path)
    html = fetch_html(args.blog_home)
    latest_post_line = build_latest_post_line(collect_posts(html))
    update_readme(readme_path, latest_post_line)
    print(f"Updated {readme_path} with: {latest_post_line}")


if __name__ == "__main__":
    main()
