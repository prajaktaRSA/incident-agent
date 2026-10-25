"""`search_runbooks`: BM25 search over runbook sections (markdown split by heading)."""

import re
from functools import cached_property

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

from incident_agent.backends.base import RunbooksBackend
from incident_agent.tools.common import ToolResult, ok

MAX_SECTION_CHARS = 1200
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_SETEXT = re.compile(r"^(=+|-{3,})\s*$")
_TOKEN = re.compile(r"[a-z0-9]+")

DESCRIPTION = """\
Search the team's runbooks (docs/*.md in the service repos). Returns the best-matching sections
with repo, path, heading, text and a GitHub link. Runbook formats vary, so matching is by
keywords: include symptoms, status codes and component names."""


class SearchRunbooksInput(BaseModel):
    query: str = Field(description="Keywords, e.g. 'merchant endpoint 503 timeout retries'.")
    top_k: int = Field(default=3, ge=1, le=5)


class RunbookIndex:
    def __init__(self, backend: RunbooksBackend) -> None:
        self._backend = backend

    @cached_property
    def sections(self) -> list[dict[str, str]]:
        """Split each document at headings: `# ATX` style and `Setext\\n===` style.

        Text before the first heading becomes a section named after the file.
        """
        sections = []
        for doc in self._backend.documents():
            title = None
            heading = doc["path"].rsplit("/", 1)[-1].removesuffix(".md")
            lines: list[str] = []
            for line in doc["text"].splitlines():
                atx = _HEADING.match(line)
                setext = _SETEXT.match(line) and lines and lines[-1].strip()
                if atx or setext:
                    new_heading = atx.group(2).strip() if atx else lines.pop().strip()
                    if any(text.strip() for text in lines):
                        sections.append(_section(doc, title, heading, lines))
                    heading, lines = new_heading, []
                    title = title or new_heading
                else:
                    lines.append(line)
            if any(text.strip() for text in lines):
                sections.append(_section(doc, title, heading, lines))
        return sections

    @cached_property
    def _bm25(self) -> BM25Okapi | None:
        corpus = [_tokens(f"{s['title']} {s['heading']} {s['text']}") for s in self.sections]
        return BM25Okapi(corpus) if corpus else None

    def search(self, query: str, top_k: int) -> list[dict]:
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(_tokens(query))
        ranked = sorted(zip(scores, self.sections), key=lambda pair: pair[0], reverse=True)
        return [{**s, "score": round(float(score), 2)} for score, s in ranked[:top_k] if score > 0]


def _section(doc: dict, title: str | None, heading: str, lines: list[str]) -> dict[str, str]:
    text = "\n".join(lines).strip()
    return {
        "repo": doc["repo"],
        "path": doc["path"],
        "title": title or heading,
        "heading": heading,
        "text": text[:MAX_SECTION_CHARS],
        "url": f"{doc['url']}#{_slug(heading)}",
    }


def _slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def search_runbooks(index: RunbookIndex, query: str, top_k: int = 3) -> ToolResult:
    results = index.search(query, top_k)
    if not results:
        return ok({"query": query, "results": []}, "no matching runbook sections")
    summary = " · ".join(f"{r['path'].split('/')[-1]} › {r['heading']}" for r in results)
    return ok({"query": query, "results": results}, summary)


def make_search_runbooks_tool(backend: RunbooksBackend) -> BaseTool:
    index = RunbookIndex(backend)

    def _run(query: str, top_k: int = 3) -> ToolResult:
        return search_runbooks(index, query, top_k)

    return StructuredTool.from_function(
        func=_run, name="search_runbooks", description=DESCRIPTION,
        args_schema=SearchRunbooksInput, response_format="content_and_artifact",
    )
