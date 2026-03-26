from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

START_MARKER = "<!-- BLOG-POST-LIST:START -->"
END_MARKER = "<!-- BLOG-POST-LIST:END -->"
USER_AGENT = "al-husayn-readme-updater/1.0"
DEFAULT_POST_LIMIT = 9


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
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_POST_LIMIT,
        help="Number of recent posts to include in the README.",
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


class MetaTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta_values: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return

        attr_map = {name.lower(): value for name, value in attrs if value is not None}
        content = attr_map.get("content")
        if not content:
            return

        for key_name in ("name", "property"):
            key = attr_map.get(key_name)
            if key:
                self.meta_values.setdefault(key, []).append(content)


def parse_meta_values(html: str) -> dict[str, list[str]]:
    parser = MetaTagParser()
    parser.feed(html)
    return parser.meta_values


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def first_meta_value(meta_values: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = meta_values.get(key, [])
        for value in values:
            cleaned = normalize_whitespace(value)
            if cleaned:
                return cleaned
    return ""


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
                description = candidate.get("description", "")

                if not title or not url or url in seen_urls:
                    continue

                posts.append(
                    {
                        "title": normalize_whitespace(title),
                        "url": url.strip(),
                        "published": str(published).strip(),
                        "description": normalize_whitespace(str(description)),
                    }
                )
                seen_urls.add(url)

    return posts


def format_date(raw_date: str) -> str:
    parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def sort_posts(posts: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(posts, key=lambda post: post.get("published", ""), reverse=True)


def fetch_post_metadata(post_url: str) -> dict[str, str]:
    try:
        meta_values = parse_meta_values(fetch_html(post_url))
    except Exception:
        return {}

    return {
        "creator": first_meta_value(
            meta_values, "article:author", "author", "creator"
        ),
        "category": first_meta_value(
            meta_values, "article:section", "article:tag", "category"
        ),
        "description": first_meta_value(
            meta_values, "description", "og:description", "twitter:description"
        ),
    }


def enrich_posts(posts: list[dict[str, str]]) -> list[dict[str, str]]:
    enriched_posts: list[dict[str, str]] = []
    for post in posts:
        metadata = fetch_post_metadata(post["url"])
        enriched_post = dict(post)
        enriched_post["creator"] = metadata.get("creator", "")
        enriched_post["category"] = metadata.get("category", "")
        if metadata.get("description"):
            enriched_post["description"] = metadata["description"]
        enriched_posts.append(enriched_post)

    return enriched_posts


def build_post_block(post: dict[str, str]) -> str:
    published = post.get("published")
    date_suffix = f" - {format_date(published)}" if published else ""
    creator = post.get("creator") or "Al-Hussein"
    category = post.get("category") or "Uncategorized"
    description = post.get("description") or "No description available."
    return "\n".join(
        [
            f"- [{post['title']}]({post['url']}){date_suffix}",
            f"  Creator: {creator}",
            f"  Category: {category}",
            f"  Description: {description}",
        ]
    )


def build_post_blocks(posts: list[dict[str, str]], limit: int) -> list[str]:
    if not posts:
        raise RuntimeError("No blog posts were found in the homepage structured data.")

    selected_posts = sort_posts(posts)[: max(limit, 1)]
    return [build_post_block(post) for post in enrich_posts(selected_posts)]


def update_readme(readme_path: Path, post_blocks: list[str]) -> None:
    readme = readme_path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in readme else "\n"
    replacement = newline.join([START_MARKER, *post_blocks, END_MARKER]).replace(
        "\n", newline
    )
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
    post_blocks = build_post_blocks(collect_posts(html), args.limit)
    update_readme(readme_path, post_blocks)
    print(f"Updated {readme_path} with {len(post_blocks)} post(s).")


if __name__ == "__main__":
    main()
