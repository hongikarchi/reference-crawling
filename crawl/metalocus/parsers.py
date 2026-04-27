"""HTML parsing logic for listing pages and article pages."""

import json
import re
from urllib.parse import urljoin, unquote

from bs4 import BeautifulSoup

from crawl.metalocus.models import BuildingData, ImageData
from core import config


def parse_listing_page(html):
    """Parse a category listing page and return list of article URL paths."""
    soup = BeautifulSoup(html, "lxml")
    articles = []
    seen = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/news/" not in href:
            continue
        if link.find(["h2", "h3"]) or link.find(class_=re.compile(r"title", re.I)):
            clean = href.split("?")[0].rstrip("/")
            if clean not in seen:
                seen.add(clean)
                articles.append(clean)

    # Fallback: if no title-tagged links found, grab all /news/ links
    if not articles:
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/news/" in href:
                clean = href.split("?")[0].rstrip("/")
                if clean not in seen:
                    seen.add(clean)
                    articles.append(clean)

    return articles


def parse_last_page_number(html):
    """Extract the last page number from pagination on a listing page."""
    soup = BeautifulSoup(html, "lxml")

    last_link = soup.find("a", title=re.compile(r"last\s+page", re.I))
    if last_link and last_link.get("href"):
        match = re.search(r"page=(\d+)", last_link["href"])
        if match:
            return int(match.group(1))

    max_page = 0
    for link in soup.find_all("a", href=True):
        match = re.search(r"page=(\d+)", link["href"])
        if match:
            num = int(match.group(1))
            if num > max_page:
                max_page = num

    return max_page if max_page > 0 else None


def parse_article_page(html, article_url=""):
    """Parse an article page and extract structured building data."""
    soup = BeautifulSoup(html, "lxml")
    data = BuildingData()

    # Title from <h1> or field--name-title
    title_el = soup.find(class_="field--name-title") or soup.find("h1")
    if title_el:
        data.title = title_el.get_text(strip=True)

    # Publication date from field--name-field-date
    date_el = soup.find(class_="field--name-field-date")
    if date_el:
        data.publication_date = date_el.get_text(strip=True)
    else:
        date_match = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", html)
        if date_match:
            data.publication_date = date_match.group(1)

    # Location from field--name-field-subtitle (format: "[City] Country")
    subtitle_el = soup.find(class_="field--name-field-subtitle")
    if subtitle_el:
        loc_text = subtitle_el.get_text(strip=True)
        # Parse "[City] Country" or "[City, Region] Country" pattern
        bracket_match = re.match(r"\[(.+?)\]\s*(.*)", loc_text)
        if bracket_match:
            data.city = bracket_match.group(1).strip()
            data.country = bracket_match.group(2).strip().rstrip(".")
            data.location = f"{data.city}, {data.country}" if data.country else data.city
        else:
            data.location = loc_text
            parts = [p.strip() for p in loc_text.split(",")]
            if len(parts) >= 2:
                data.city = parts[0]
                data.country = parts[-1]

    # Extract credits metadata from Drupal custom fields
    metadata = _extract_credits_metadata(soup)
    data.raw_metadata = json.dumps(metadata, ensure_ascii=False)

    # Map metadata to structured fields
    data.architects = (
        metadata.get("architects", "")
        or metadata.get("architect", "")
        or metadata.get("architecture", "")
        or metadata.get("design", "")
    )
    data.year = metadata.get("dates", "") or metadata.get("date", "") or metadata.get("year", "")
    data.area_sqm = metadata.get("area", "") or metadata.get("built area", "") or metadata.get("site area", "")
    data.materials = metadata.get("materials", "")

    # If location not found from subtitle, try metadata
    if not data.location and metadata.get("location"):
        data.location = metadata["location"]
        parts = [p.strip() for p in data.location.split(",")]
        if len(parts) >= 2:
            data.city = parts[0]
            data.country = parts[-1]

    # Credits as JSON
    credit_keys = [
        "photography", "photographer", "photos", "collaborators",
        "collaboration", "client", "structure", "structural",
        "engineers", "engineering", "builder", "contractor",
        "interior design", "landscape", "mep", "developer", "project manager",
    ]
    credits = {k: metadata[k] for k in credit_keys if k in metadata}
    data.credits = json.dumps(credits, ensure_ascii=False) if credits else ""

    # Body text from introduction + body fields
    data.description = _extract_body_text(soup)

    # Tags from the content_by_category view's parent category
    data.tags = _extract_tags(soup)

    # Building type from tags
    building_types = [
        "housing", "offices", "schools", "hospitals", "hotels",
        "museums", "libraries", "theaters", "churches", "stations",
        "airports", "bridges", "parking", "restaurants", "wineries",
        "sports", "cultural centers", "universities", "pavilions",
        "minim-dwelling", "bar-restaurant-cafeteria",
    ]
    for tag in data.tags:
        if tag.lower() in building_types:
            data.building_type = tag
            break

    # Images
    data.images = _extract_images(soup)

    return data


def _extract_credits_metadata(soup):
    """Extract key-value metadata from Drupal field--name-field-creditos-custom blocks."""
    metadata = {}

    # Each credit block has a label div and a text div
    credit_blocks = soup.find_all(class_="field--name-field-creditos-custom")
    for block in credit_blocks:
        label_el = block.find(class_="field--name-field-label")
        text_el = block.find(class_="field--name-field-text")

        if label_el and text_el:
            # The label div contains "Label" prefix text + actual label
            label_text = label_el.get_text(strip=True)
            # Remove "Label" prefix if present
            label_text = re.sub(r"^Label\s*", "", label_text, flags=re.I).strip()

            # The text div contains "Text" prefix + actual value
            value_text = text_el.get_text(strip=True)
            # Remove "Text" prefix if present
            value_text = re.sub(r"^Text\s*", "", value_text, flags=re.I).strip()

            if label_text and value_text:
                metadata[label_text.lower()] = value_text

    return metadata


def _extract_body_text(soup):
    """Extract the main body text from introduction and body fields."""
    paragraphs = []

    # Introduction field
    intro = soup.find(class_="field--name-field-introduction")
    if intro:
        for p in intro.find_all("p"):
            text = p.get_text(strip=True)
            if text and len(text) > 10:
                paragraphs.append(text)

    # Main body field
    body = soup.find(class_="field--name-field-body-noticia")
    if body:
        for p in body.find_all("p"):
            text = p.get_text(strip=True)
            if text and len(text) > 10:
                paragraphs.append(text)

    # Fallback to generic body field
    if not paragraphs:
        body = soup.find(class_="field--name-body")
        if body:
            for p in body.find_all("p"):
                text = p.get_text(strip=True)
                if text and len(text) > 20:
                    paragraphs.append(text)

    return "\n\n".join(paragraphs)


def _extract_tags(soup):
    """Extract article-specific category tags, excluding site-wide navigation."""
    tags = []
    seen = set()

    # Look for the content_by_category view — this contains category-specific content
    # The view's title or the page's category context gives us the article's category
    # But the actual tags are in the listing page that discovered this article.
    # On the article page itself, we look for category links that are within
    # the article content area, not in the navigation sidebar.

    # Strategy: find category links in the article's own header/meta area
    # These are typically in a different container than the sidebar navigation
    # The sidebar categories are in "block-metalocus-blocks-menu-categories"

    # Get all sidebar category containers to exclude
    sidebar_blocks = soup.find_all(class_=re.compile(r"block-metalocus-blocks|full_view_categories|full_categories|tipo_categories|main_categories|categories$"))

    sidebar_links = set()
    for block in sidebar_blocks:
        for a in block.find_all("a", href=True):
            sidebar_links.add(id(a))

    # Now find category links NOT in sidebar
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/category/" not in href:
            continue
        if id(link) in sidebar_links:
            continue
        tag_name = link.get_text(strip=True)
        if tag_name and tag_name.lower() not in seen:
            seen.add(tag_name.lower())
            tags.append(tag_name)

    # If no article-specific tags found, try to extract from the breadcrumb or URL
    if not tags and "architecture" in (soup.get_text().lower()):
        tags.append("Architecture")

    return tags


def _extract_images(soup):
    """Extract content images from the article, excluding navigation thumbnails."""
    images = []
    seen_basenames = set()

    # Strategy 1: Get high-res URLs from venobox lightbox links (gallery)
    # These <a class="venobox"> have href pointing to full-res images
    for a in soup.find_all("a", class_="venobox", href=True):
        href = a["href"]
        if "/sites/default/files/" not in href:
            continue
        _add_image_from_url(href, a.find("img"), images, seen_basenames)

    # Strategy 2: Get images from <article class="media"> (inline high-res)
    for article in soup.find_all("article", class_="media"):
        for img in article.find_all("img", src=True):
            src = img["src"]
            if "/sites/default/files/" not in src:
                continue
            _add_image_from_url(src, img, images, seen_basenames)

    # Strategy 3: Get inline images from body content
    for cls in ["field--name-field-body-noticia", "field--name-field-introduction", "field--name-body"]:
        body = soup.find(class_=cls)
        if not body:
            continue
        for img in body.find_all("img", src=True):
            src = img["src"]
            if "/sites/default/files/" not in src:
                continue
            _add_image_from_url(src, img, images, seen_basenames)

    # Strategy 4: Fallback — search all images on page
    if not images:
        main = soup.find("div", id="main-content") or soup
        for img in main.find_all("img", src=True):
            src = img["src"]
            if "/sites/default/files/" not in src:
                continue
            _add_image_from_url(src, img, images, seen_basenames)

    # Re-number image_order
    for idx, img in enumerate(images):
        img.image_order = idx

    return images


def _add_image_from_url(url, img_tag, images, seen_basenames):
    """Add an image to the list, handling deduplication and URL cleanup."""
    # Strip /styles/XXX/public/ to get original file path
    clean_url = re.sub(r"/styles/[^/]+/public/", "/", url)
    clean_url = clean_url.split("?")[0]

    basename = unquote(clean_url.split("/")[-1])

    # Skip thumbnails and non-content images
    if basename.endswith("_p.jpg") or basename.endswith("_p.png"):
        return
    if "_bio." in basename:
        return
    if "article_image_lead_mobile_nano" in url:
        return

    # Deduplicate by base filename
    if basename in seen_basenames:
        return
    seen_basenames.add(basename)

    # Build full URL
    if clean_url.startswith("/"):
        full_url = config.BASE_URL + clean_url
    else:
        full_url = clean_url

    alt_text = ""
    if img_tag:
        alt_text = img_tag.get("alt", "")

    images.append(ImageData(
        url=full_url,
        filename=basename,
        alt_text=alt_text,
        image_order=0,
    ))
