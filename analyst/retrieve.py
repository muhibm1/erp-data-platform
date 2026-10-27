"""Context retrieval: pick the domain knowledge a question needs.

Two rankers look at every table, term and example. A keyword ranker (BM25)
catches exact words like "credit note" or a column name. An embedding ranker
catches meaning when the words differ ("outstanding" for open). The two
rankings are fused with reciprocal rank fusion, and the top pieces of each kind
go into the prompt. The embedding model runs locally (bge-small, 384
dimensions, ONNX on CPU), so retrieval costs nothing and takes milliseconds
once the model is loaded.
"""

import math
import re
from collections import Counter

LIMITS = {"table": 4, "term": 4, "example": 2}
RRF_K = 60
_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower().replace("_", " "))


class BM25:
    def __init__(self, texts: list[str], k1: float = 1.2, b: float = 0.75):
        self.docs = [Counter(tokens(t)) for t in texts]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lens) / max(len(self.lens), 1)
        df = Counter(w for d in self.docs for w in d)
        n = len(self.docs)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query: str) -> list[float]:
        q = tokens(query)
        out = []
        for d, n in zip(self.docs, self.lens):
            s = 0.0
            for w in q:
                f = d.get(w, 0)
                if f:
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * n / self.avg))
            out.append(s)
        return out


class Embedder:
    _model = None

    @classmethod
    def embed(cls, texts: list[str]) -> list[list[float]]:
        if cls._model is None:
            from fastembed import TextEmbedding
            cls._model = TextEmbedding("BAAI/bge-small-en-v1.5")
        return [list(v) for v in cls._model.embed(texts)]


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


class Retriever:
    """Ranks a domain's documents for a question. `embed=None` keeps it keyword-only (tests)."""

    def __init__(self, docs: list[dict], embed=Embedder.embed):
        self.docs = docs
        self.bm25 = BM25([d["text"] for d in docs])
        self.embed = embed
        self.vectors = embed([d["text"] for d in docs]) if embed else None

    def rank(self, question: str) -> list[int]:
        rankings = [sorted(range(len(self.docs)), key=lambda i, s=self.bm25.scores(question): -s[i])]
        if self.vectors is not None:
            q = self.embed([question])[0]
            sims = [_cos(q, v) for v in self.vectors]
            rankings.append(sorted(range(len(self.docs)), key=lambda i: -sims[i]))
        fused = Counter()
        for ranking in rankings:
            for pos, i in enumerate(ranking):
                fused[i] += 1 / (RRF_K + pos + 1)
        return [i for i, _ in fused.most_common()]

    def select(self, question: str, limits: dict = LIMITS) -> list[dict]:
        taken = Counter()
        picked = []
        for i in self.rank(question):
            d = self.docs[i]
            if taken[d["kind"]] < limits.get(d["kind"], 0):
                taken[d["kind"]] += 1
                picked.append(d)
        return picked
