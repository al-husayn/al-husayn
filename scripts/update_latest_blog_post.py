from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime
from html import escape as escape_html, unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from xml.etree import ElementTree

BLOG_START_MARKER = "<!-- BLOG-POST-LIST:START -->"
BLOG_END_MARKER = "<!-- BLOG-POST-LIST:END -->"
YOUTUBE_START_MARKER = "<!-- YOUTUBE:START -->"
YOUTUBE_END_MARKER = "<!-- YOUTUBE:END -->"
USER_AGENT = "al-husayn-readme-updater/1.0"
DEFAULT_POST_LIMIT = 5
DEFAULT_VIDEO_LIMIT = 1
DEFAULT_YOUTUBE_CHANNEL_ID = "UCc19yVrMKZ9tCy40hWEc3BA"
ATOM_NS = "http://www.w3.org/2005/Atom"
MEDIA_NS = "http://search.yahoo.com/mrss/"
YOUTUBE_NS = "http://www.youtube.com/xml/schemas/2015"
XML_NAMESPACES = {
    "atom": ATOM_NS,
    "media": MEDIA_NS,
    "yt": YOUTUBE_NS,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Update the README with the latest blog posts and YouTube videos."
        )
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
        help="Number of recent blog posts to include in the README.",
    )
    parser.add_argument(
        "--youtube-feed",
        default=(
            "https://www.youtube.com/feeds/videos.xml"
            f"?channel_id={DEFAULT_YOUTUBE_CHANNEL_ID}"
        ),
        help="YouTube channel Atom feed URL.",
    )
    parser.add_argument(
        "--youtube-limit",
        type=int,
        default=DEFAULT_VIDEO_LIMIT,
        help="Number of recent videos to include in the README.",
    )
    return parser.parse_args()


def fetch_text(url: str) -> str:
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
                image = candidate.get("image", "")

                if not title or not url or url in seen_urls:
                    continue

                image_url = ""
                if isinstance(image, str):
                    image_url = image.strip()
                elif isinstance(image, list) and image:
                    first_image = image[0]
                    if isinstance(first_image, str):
                        image_url = first_image.strip()
                elif isinstance(image, dict):
                    image_url = str(image.get("url", "")).strip()

                posts.append(
                    {
                        "title": normalize_whitespace(title),
                        "url": url.strip(),
                        "published": str(published).strip(),
                        "description": normalize_whitespace(str(description)),
                        "image": image_url,
                    }
                )
                seen_urls.add(url)

    return posts


def collect_videos(feed_xml: str) -> list[dict[str, str]]:
    try:
        root = ElementTree.fromstring(feed_xml)
    except ElementTree.ParseError as exc:
        raise RuntimeError("Unable to parse the YouTube feed XML.") from exc

    channel_title = normalize_whitespace(
        root.findtext("atom:title", default="", namespaces=XML_NAMESPACES) or ""
    )
    videos: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for entry in root.findall("atom:entry", XML_NAMESPACES):
        title = normalize_whitespace(
            entry.findtext("atom:title", default="", namespaces=XML_NAMESPACES) or ""
        )
        published = (
            entry.findtext("atom:published", default="", namespaces=XML_NAMESPACES) or ""
        ).strip()
        video_id = (
            entry.findtext("yt:videoId", default="", namespaces=XML_NAMESPACES) or ""
        ).strip()
        url = ""
        description = ""
        thumbnail_url = ""
        author = normalize_whitespace(
            entry.findtext(
                "atom:author/atom:name",
                default=channel_title,
                namespaces=XML_NAMESPACES,
            )
            or channel_title
        )

        link = entry.find("atom:link[@rel='alternate']", XML_NAMESPACES)
        if link is None:
            link = entry.find("atom:link", XML_NAMESPACES)
        if link is not None:
            url = (link.get("href") or "").strip()

        media_group = entry.find("media:group", XML_NAMESPACES)
        if media_group is not None:
            description = normalize_whitespace(
                media_group.findtext(
                    "media:description", default="", namespaces=XML_NAMESPACES
                )
                or ""
            )
            thumbnail = media_group.find("media:thumbnail", XML_NAMESPACES)
            if thumbnail is not None:
                thumbnail_url = (thumbnail.get("url") or "").strip()

        if not thumbnail_url and video_id:
            thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

        if not title or not url or url in seen_urls:
            continue

        videos.append(
            {
                "title": title,
                "url": url,
                "published": published,
                "description": description,
                "thumbnail": thumbnail_url,
                "channel": author,
            }
        )
        seen_urls.add(url)

    return videos


def format_date(raw_date: str) -> str:
    parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def sort_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(items, key=lambda item: item.get("published", ""), reverse=True)


def fetch_post_metadata(post_url: str) -> dict[str, str]:
    try:
        meta_values = parse_meta_values(fetch_text(post_url))
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
        "image": first_meta_value(meta_values, "og:image", "twitter:image"),
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
        if metadata.get("image") and not enriched_post.get("image"):
            enriched_post["image"] = metadata["image"]
        enriched_posts.append(enriched_post)

    return enriched_posts


def build_post_row(post: dict[str, str]) -> str:
    published = post.get("published")
    published_text = format_date(published) if published else "No publish date"
    creator = post.get("creator") or "Al-Hussein"
    category = post.get("category") or "Uncategorized"
    description = post.get("description") or "No description available."
    image = post.get("image")
    image_markup = ""
    if image:
        image_markup = "\n".join(
            [
                f'      <a href="{escape_html(post["url"], quote=True)}">',
                f'        <img src="{escape_html(image, quote=True)}" width="120" alt="{escape_html(post["title"], quote=True)} feature image" />',
                "      </a>",
            ]
        )

    return "\n".join(
        [
            "  <tr>",
            '    <td width="132" valign="top">',
            image_markup or "      &nbsp;",
            "    </td>",
            '    <td valign="top">',
            f'      <a href="{escape_html(post["url"], quote=True)}"><strong>{escape_html(post["title"])}</strong></a><br />',
            f"      <sub>{escape_html(published_text)}</sub><br />",
            f"      <sub>Creator: {escape_html(creator)} | Category: {escape_html(category)}</sub><br /><br />",
            f"      {escape_html(description)}",
            "    </td>",
            "  </tr>",
        ]
    )


def build_post_markup(posts: list[dict[str, str]], limit: int) -> str:
    if not posts:
        raise RuntimeError("No blog posts were found in the homepage structured data.")

    selected_posts = sort_items(posts)[: max(limit, 1)]
    rows = [build_post_row(post) for post in enrich_posts(selected_posts)]
    return "\n".join(["<table>", *rows, "</table>"])


def truncate_text(value: str, limit: int) -> str:
    normalized = normalize_whitespace(value)
    if len(normalized) <= limit:
        return normalized
    shortened = normalized[:limit].rsplit(" ", 1)[0].strip()
    return (shortened or normalized[:limit]).rstrip(".,;:") + "..."


def build_video_row(video: dict[str, str]) -> str:
    published = video.get("published")
    published_text = format_date(published) if published else "No publish date"
    description = truncate_text(
        video.get("description") or "Watch the latest upload on YouTube.",
        170,
    )
    channel = video.get("channel") or "AL Drake"
    thumbnail = video.get("thumbnail")
    thumbnail_markup = ""
    if thumbnail:
        thumbnail_markup = "\n".join(
            [
                f'      <a href="{escape_html(video["url"], quote=True)}">',
                f'        <img src="{escape_html(thumbnail, quote=True)}" width="160" alt="{escape_html(video["title"], quote=True)} thumbnail" />',
                "      </a>",
            ]
        )

    return "\n".join(
        [
            "  <tr>",
            '    <td width="172" valign="top">',
            thumbnail_markup or "      &nbsp;",
            "    </td>",
            '    <td valign="top">',
            f'      <a href="{escape_html(video["url"], quote=True)}"><strong>{escape_html(video["title"])}</strong></a><br />',
            f"      <sub>{escape_html(published_text)}</sub><br />",
            f"      <sub>Channel: {escape_html(channel)}</sub><br /><br />",
            f"      {escape_html(description)}",
            "    </td>",
            "  </tr>",
        ]
    )


def build_video_markup(videos: list[dict[str, str]], limit: int) -> str:
    if not videos:
        raise RuntimeError("No YouTube videos were found in the channel feed.")

    selected_videos = sort_items(videos)[: max(limit, 1)]
    rows = [build_video_row(video) for video in selected_videos]
    return "\n".join(["<table>", *rows, "</table>"])


def replace_marked_section(
    readme: str,
    *,
    start_marker: str,
    end_marker: str,
    content: str,
    newline: str,
) -> str:
    replacement = newline.join([start_marker, content, end_marker]).replace(
        "\n", newline
    )
    pattern = re.compile(
        rf"{re.escape(start_marker)}.*?{re.escape(end_marker)}",
        flags=re.DOTALL,
    )

    if not pattern.search(readme):
        raise RuntimeError(f"README markers were not found for section {start_marker}.")

    return pattern.sub(replacement, readme, count=1)


def update_readme(readme_path: Path, post_markup: str, video_markup: str) -> None:
    readme = readme_path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in readme else "\n"
    updated_readme = replace_marked_section(
        readme,
        start_marker=BLOG_START_MARKER,
        end_marker=BLOG_END_MARKER,
        content=post_markup,
        newline=newline,
    )
    updated_readme = replace_marked_section(
        updated_readme,
        start_marker=YOUTUBE_START_MARKER,
        end_marker=YOUTUBE_END_MARKER,
        content=video_markup,
        newline=newline,
    )

    if updated_readme != readme:
        readme_path.write_text(updated_readme, encoding="utf-8")


def main() -> None:
    args = parse_args()
    readme_path = Path(args.readme_path)
    html = fetch_text(args.blog_home)
    youtube_feed_xml = fetch_text(args.youtube_feed)
    post_markup = build_post_markup(collect_posts(html), args.limit)
    video_markup = build_video_markup(collect_videos(youtube_feed_xml), args.youtube_limit)
    update_readme(readme_path, post_markup, video_markup)
    print(
        f"Updated {readme_path} with up to {args.limit} blog post(s) "
        f"and {args.youtube_limit} YouTube video(s)."
    )


if __name__ == "__main__":
    main()
