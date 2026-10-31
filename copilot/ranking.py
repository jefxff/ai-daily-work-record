"""知识库检索排序：BM25（中文用 2-gram 切词，不引入分词依赖）。

为什么换掉原来的 ``icontains`` + 按更新时间排：
- 命中不排序时，"哪篇更相关"完全取决于数据库返回顺序，用户在前 5 条看不到
  真正想要的就会判定"AI 搜不到"；
- 中文没有空格，英文那套空格分词直接失效。这里用**汉字 2-gram**——把
  "客诉处理流程"切成"客诉/诉处/处理/理流/流程"，不需要词典也能把
  部分重合的文档捞回来，代价只是索引膨胀一点（50 篇文档毫无压力）。

为什么不上向量：数据量（几十到几百篇）远没到需要向量的规模，而向量库会
引入新的部署依赖；BM25 在这个量级的召回已经足够，做不到的事是
"语义相近但字面不同"，那部分留给后续再说（已记进计划书 §4 B3-4）。
"""
import math
import re
from collections import Counter

# 英文/数字连成一个词；其余（含中日韩）按 2-gram 切。
_TOKEN_RE = re.compile(r'[A-Za-z0-9]+|[\u4e00-\u9fff]')
STOPWORDS = frozenset('的了和在与及为对是也要会等都就好很'.split())
MAX_DOCS = 500          # 参与排序的候选上限：再多就是全表扫描，量级也不该到这
MIN_SCORE = 0.05        # 低于此分视为"没相关性"，宁可回退也不要给噪声

K1 = 1.5
B = 0.75


def tokenize(text):
    """汉字按相邻二字组，英文数字按词；单字与停用字丢弃。"""
    text = (text or '').lower()
    tokens = []
    han = [m.group(0) for m in _TOKEN_RE.finditer(text) if '\u4e00' <= m.group(0) <= '\u9fff']
    # 英文/数字词
    for word in re.findall(r'[A-Za-z0-9]+', text):
        if len(word) >= 2:
            tokens.append(word)
    for i in range(len(han) - 1):
        bigram = han[i] + han[i + 1]
        if bigram not in STOPWORDS:
            tokens.append(bigram)
    if len(han) == 1 and han[0] not in STOPWORDS:
        tokens.append(han[0])
    return tokens


def build_corpus(docs, title_weight=3):
    """``[(doc_id, 文本)]`` → 语料。标题字段加权：命中标题比命中正文更有说服力。"""
    corpus = []
    for doc_id, parts in docs:
        counts = Counter()
        for weight, text in parts:
            for token in tokenize(text):
                counts[token] += weight
        corpus.append((doc_id, counts, sum(counts.values())))
    return corpus


def _idf(corpus):
    total = max(1, len(corpus))
    df = Counter()
    for _doc_id, counts, _length in corpus:
        for token in counts:
            df[token] += 1
    return {
        token: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
        for token, freq in df.items()
    }


def score(query, corpus, top=5, boost=None):
    """BM25 打分，返回 ``[(doc_id, score), ...]``（按分数降序）。

    ``boost`` 是**领域**加分：BM25 只看词频，不知道"标题里有整串关键词"
    比"正文中反复出现某个词"更说明问题。调用方把它算出来传进来，
    排序模块保持对业务无知。

    ``corpus`` 为空时返回空列表；查询一个词都命中不了也一样——
    调用方据此决定是回退旧写法还是告诉用户没找到。
    """
    if not corpus:
        return []
    terms = tokenize(query)
    if not terms:
        return []
    boost = boost or {}
    idf = _idf(corpus)
    avg_len = max(1.0, sum(length for _d, _c, length in corpus) / len(corpus))

    scored = []
    for doc_id, counts, length in corpus:
        total = 0.0
        for term in terms:
            freq = counts.get(term, 0)
            if not freq:
                continue
            denom = freq + K1 * (1 - B + B * length / avg_len)
            total += idf.get(term, 0.0) * (freq * (K1 + 1)) / denom
        if not total and doc_id not in boost:
            continue
        total += boost.get(doc_id, 0.0)
        if total >= MIN_SCORE:
            scored.append((doc_id, round(total, 4)))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:top]


def title_boost(candidates, keyword, full=2.0, partial=0.8):
    """标题/标签/摘要里出现完整关键词串时加分。

    只在**整串**出现时给大分："客诉流程"出现在标题里，肯定比两万字正文里
    各散落一次"客诉"和"流程"更对得上用户的心思。
    """
    needle = (keyword or '').strip()
    boost = {}
    if not needle:
        return boost
    bigrams = set(tokenize(needle))
    for doc in candidates:
        title = getattr(doc, 'title', '') or ''
        summary = getattr(doc, 'summary', '') or ''
        tags = getattr(doc, 'tags', '') or ''
        extra = 0.0
        if needle in title:
            extra += full
        elif needle in tags or needle in summary:
            extra += partial
        # 标题里命中部分词：比正文命中强，比整串命中弱
        hit = sum(1 for token in bigrams if token in title)
        if hit:
            extra += partial * min(hit, 3) / 3
        if extra:
            boost[doc.pk] = round(extra, 4)
    return boost


def rank_documents(query, docs, top=5, boost=None):
    """一步到位：``docs`` 为 ``[(doc_id, [(权重, 文本), ...]), ...]``。"""
    corpus = build_corpus(docs)
    scored = score(query, corpus, top=top, boost=boost)
    known = {doc_id for doc_id, _parts in docs}
    return [(doc_id, value) for doc_id, value in scored if doc_id in known]
