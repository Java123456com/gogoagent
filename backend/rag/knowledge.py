import re
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from backend.config import get_settings
from backend.infrastructure.embedding import DashScopeEmbedding, DashScopeEmbeddingError
from backend.rag.providers import (
    DEFAULT_BAILIAN_KNOWLEDGE_ENDPOINT,
    BailianKnowledgeError,
    BailianKnowledgeProvider,
)

_RESOURCE_ROOT = Path(__file__).resolve().parents[1] / "resources"
_DATASET_DIRS = (_RESOURCE_ROOT / "dataset",)
_CITY_PATTERN = re.compile(
    r"(北京|上海|广州|深圳|成都|杭州|重庆|武汉|西安|苏州|天津|南京|长沙|郑州|东莞|"
    r"青岛|沈阳|宁波|昆明|厦门|合肥|佛山|无锡|哈尔滨|济南|福州|大连|贵阳|太原|"
    r"南昌|南宁|石家庄|长春|呼和浩特|兰州|乌鲁木齐|海口|银川|西宁|拉萨)"
)


class KeywordRetriever:
    def __init__(self, name: str, files: list[str]):
        self.name, self.files = name, files
        settings = get_settings()
        self._embedding = (
            DashScopeEmbedding(
                settings.dashscope_api_key,
                settings.dashscope_embedding_model,
                settings.dashscope_embedding_dimensions,
                settings.external_request_timeout_seconds,
            )
            if settings.dashscope_embedding_enabled and settings.dashscope_api_key
            else None
        )
        self._embedded_docs: list[tuple[str, list[float]]] | None = None

    def retrieve(self, query: str, limit: int = 4) -> list[dict[str, str]]:
        terms = _search_terms(query)
        docs = []
        for filename in self.files:
            path = _resolve_dataset_path(filename)
            if not path.exists(): continue
            content = _read_dataset(path)
            score = sum(term in content.lower() for term in terms)
            docs.append({"source": filename, "provider": "local", "content": content[:4000], "score": score})
        if self._embedding and docs:
            try:
                if self._embedded_docs is None:
                    vectors = self._embedding.embed([item["content"] for item in docs])
                    self._embedded_docs = [(item["content"], _normalize(vector))
                                           for item, vector in zip(docs, vectors)]
                query_vector = _normalize(self._embedding.embed([query])[0])
                scores = {content: _cosine(query_vector, vector)
                          for content, vector in self._embedded_docs}
                for item in docs:
                    item["score"] = max(float(item.get("score", 0)), scores.get(item["content"], 0.0))
            except DashScopeEmbeddingError:
                # Keep keyword retrieval available during DashScope outages.
                self._embedding = None
        return sorted(docs, key=lambda item: item["score"], reverse=True)[:limit]


class TravelKnowledgeBase:
    """Three travel knowledge sources exposed behind one provider-aware facade.

    Attraction retrieval can use Bailian, while policy and guideline knowledge
    are bundled local corpora.
    """
    def __init__(self):
        self.attraction_knowledge = KeywordRetriever("attractionKnowledge", ["tourist_attraction.xlsx"])
        self.policy_knowledge = KeywordRetriever("corporateTravelPolicyKnowledge", ["business_travel_policy.docx"])
        self.guideline_knowledge = KeywordRetriever("corporateTravelGuidelinesKnowledge", ["business_travel_guidelines.docx"])
        settings = get_settings()
        self.attraction_provider = None
        if (settings.bailian_knowledge_enabled
                and settings.bailian_access_key_id
                and settings.bailian_access_key_secret
                and settings.bailian_workspace_id
                and settings.bailian_index_id):
            self.attraction_provider = BailianKnowledgeProvider(
                endpoint=settings.bailian_knowledge_endpoint or DEFAULT_BAILIAN_KNOWLEDGE_ENDPOINT,
                access_key_id=settings.bailian_access_key_id,
                access_key_secret=settings.bailian_access_key_secret,
                workspace_id=settings.bailian_workspace_id,
                index_id=settings.bailian_index_id,
                timeout=settings.external_request_timeout_seconds,
            )

    def retrieve(self, query: str, knowledge: str = "all") -> list[dict[str, str]]:
        retrievers = {"attraction": self.attraction_knowledge, "policy": self.policy_knowledge, "guideline": self.guideline_knowledge}
        if knowledge == "attraction" and self.attraction_provider:
            try:
                return self.attraction_provider.retrieve(query)
            except BailianKnowledgeError:
                # Keep local corpus available for development and transient API errors.
                pass
        if knowledge in retrievers:
            return retrievers[knowledge].retrieve(query)
        return self.retrieve(query, "attraction") + self.retrieve(query, "policy") + self.retrieve(query, "guideline")

    @staticmethod
    def extract_city(query: str) -> str | None:
        match = _CITY_PATTERN.search(query or "")
        return match.group(1) if match else None


travel_knowledge = TravelKnowledgeBase()


def _normalize(vector: list[float]) -> list[float]:
    import math
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _search_terms(query: str) -> set[str]:
    raw = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", (query or "").lower())
    terms = set(raw)
    for token in raw:
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            terms.update(token[index:index + 2] for index in range(len(token) - 1))
            terms.update(token[index:index + 3] for index in range(len(token) - 2))
    return {term for term in terms if len(term) >= 2}


def _resolve_dataset_path(filename: str) -> Path:
    candidates = [filename]
    suffix = Path(filename).suffix.lower()
    if suffix in {".docx", ".xlsx"}:
        candidates.append(f"{Path(filename).stem}.md")
    for candidate in candidates:
        for root in _DATASET_DIRS:
            path = root / candidate
            if path.exists():
                return path
    return _DATASET_DIRS[0] / filename


def _read_dataset(path: Path) -> str:
    """Read bundled knowledge assets without requiring POI/openpyxl."""
    if path.suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    try:
        with ZipFile(path) as archive:
            if path.suffix == ".docx":
                xml = archive.read("word/document.xml")
                root = ElementTree.fromstring(xml)
                words = [
                    node.text or ""
                    for node in root.iter()
                    if node.tag.endswith("}t")
                ]
                return " ".join(words)
            if path.suffix == ".xlsx":
                shared = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                    shared = [
                        "".join(node.text or "" for node in item.iter() if node.tag.endswith("}t"))
                        for item in root
                    ]
                values = list(shared)
                for name in archive.namelist():
                    if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                        root = ElementTree.fromstring(archive.read(name))
                        for cell in root.iter():
                            if not cell.tag.endswith("}c"):
                                continue
                            value = next(
                                (node.text for node in cell if node.tag.endswith("}v")),
                                None,
                            )
                            if value is None:
                                continue
                            if cell.attrib.get("t") == "s":
                                value = shared[int(value)] if int(value) < len(shared) else value
                            values.append(str(value))
                return " ".join(values)
    except (OSError, KeyError, ValueError, ElementTree.ParseError):
        return path.name
    return path.name
