from langchain.tools import tool
from langchain_tavily import TavilySearch
import requests
from dotenv import load_dotenv
import os
from rich import print
# for scraping and parsing HTML content
# from bs4 import BeautifulSoup
# from readability import Document
# import trafilatura
# import re

load_dotenv()  # Load environment variables from .env file
# tavily = TavilySearch(api_key=os.getenv("TAVILY_API_KEY"))
tavily=TavilySearch(max_results=5)
def web_search(query: str) -> str:
    """
    Perform a web search using the Tavily API.

    Args:
        query (str): The search query.

    Returns:
        str: The search results.
    """
    try:
        results = tavily.invoke(query)
        out = []
        for result in results["results"]:
            out.append(f"Title: {result['title']}\nURL: {result['url']}\nSnippet: {result['content'][:300]}\n")
        return "\n".join(out)
    except Exception as e:
        return f"An error occurred while performing the web search: {str(e)}"   
    
    # Tested upto here the search and rich printing in main.py, now need to test the scraping and parsing of the content from the URL.
    
    """Web page scraping tool for LangChain agents.

Pipeline: download the page, then extract the main text with the best method
that works:
    1. trafilatura          (best at finding the article body)
    2. readability-lxml     (finds the main content block, then BeautifulSoup
                             turns it into text)
    3. plain BeautifulSoup  (last resort: all visible text)

`re` is used for URL validation and for cleaning up whitespace.
"""

import ipaddress
import re
import socket
from urllib.parse import urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup
from langchain_core.tools import tool
from readability import Document

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15            # seconds
MAX_DOWNLOAD_BYTES = 3_000_000  # stop downloading pages bigger than ~3 MB
MIN_USEFUL_CHARS = 200          # below this, try the next extraction method

_URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


# --------------------------------------------------------------------------
# Safety: the agent chooses the URL, so refuse addresses inside your network.
# --------------------------------------------------------------------------
def _is_public_host(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return False
    return True


def _validate_url(url: str) -> str | None:
    """Return an error message if the URL should not be fetched, else None."""
    url = url.strip()
    if not _URL_RE.match(url):
        return "Error: URL must start with http:// or https:// and contain no spaces."
    host = urlparse(url).hostname
    if not host:
        return "Error: could not read a host name from the URL."
    if not _is_public_host(host):
        return "Error: refusing to fetch a private, local or unresolvable address."
    return None


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------
def _download(url: str) -> tuple[str | None, str | None]:
    """Return (html, error)."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
            return None, f"Error: unsupported content type '{ctype}'."

        chunks, size = [], 0
        for chunk in resp.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                break  # keep what we have; the main content is usually near the top
            chunks.append(chunk)
        raw = b"".join(chunks)
        encoding = resp.encoding or resp.apparent_encoding or "utf-8"
        return raw.decode(encoding, errors="replace"), None
    except requests.exceptions.Timeout:
        return None, f"Error: request timed out after {REQUEST_TIMEOUT}s."
    except requests.exceptions.HTTPError as exc:
        return None, f"Error: the site returned HTTP {exc.response.status_code}."
    except requests.exceptions.RequestException as exc:
        return None, f"Error: could not download the page ({exc.__class__.__name__})."


# --------------------------------------------------------------------------
# Cleaning and extraction
# --------------------------------------------------------------------------
def _clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)            # collapse spaces and tabs
    text = re.sub(r" *\n *", "\n", text)           # trim around line breaks
    text = re.sub(r"\n{3,}", "\n\n", text)         # at most one blank line
    return text.strip()


def _bs4_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["head", "script", "style", "noscript", "nav", "footer",
                     "header", "aside", "form", "svg", "iframe"]):
        tag.decompose()
    return soup.get_text(separator="\n")


def _page_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    if soup.title and soup.title.string:
        return _clean_text(soup.title.string)
    h1 = soup.find("h1")
    return _clean_text(h1.get_text()) if h1 else ""


def extract_main_text(html: str, url: str = "") -> tuple[str, str]:
    """Return (text, method_used). Tries trafilatura, readability, then bs4."""
    # 1. trafilatura
    try:
        text = trafilatura.extract(
            html, url=url or None, include_comments=False,
            include_tables=True, favor_recall=True,
        )
        if text and len(text.strip()) >= MIN_USEFUL_CHARS:
            return _clean_text(text), "trafilatura"
    except Exception:
        pass

    # 2. readability picks the main block, BeautifulSoup converts it to text
    try:
        summary_html = Document(html).summary(html_partial=True)
        text = _clean_text(_bs4_text(summary_html))
        if len(text) >= MIN_USEFUL_CHARS:
            return text, "readability+bs4"
    except Exception:
        pass

    # 3. everything visible
    return _clean_text(_bs4_text(html)), "bs4"


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------
@tool
def scrape_webpage(url: str, max_chars: int = 6000) -> str:
    """Download a web page and return its main readable text.

    Use this to read the content of a specific URL, for example a page found
    through a search. It does not run JavaScript, so pages that only render in
    the browser may come back empty.

    Args:
        url: Full page address starting with http:// or https://.
        max_chars: Maximum number of characters to return (default 6000).
    """
    error = _validate_url(url)
    if error:
        return error

    html, error = _download(url.strip())
    if error:
        return error
    if not html or not html.strip():
        return "Error: the page was empty."

    text, method = extract_main_text(html, url.strip())
    if not text:
        return "No readable text found. The page may need JavaScript to load."

    max_chars = max(500, min(int(max_chars), 20000))
    truncated = len(text) > max_chars
    text = text[:max_chars].rstrip()

    title = _page_title(html)
    header = f"Title: {title}\n" if title else ""
    footer = "\n[Content truncated]" if truncated else ""
    return f"{header}Source: {url.strip()} (extracted with {method})\n\n{text}{footer}"


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    print(scrape_webpage.invoke({"url": target}))