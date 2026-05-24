# tsindex → agent-native tool 포팅 가이드

`tsindex.py`는 단일 파일 CLI지만, 안의 구성요소는 그대로 agent 도구 (MCP server,
직접 호출 라이브러리, HTTP 서비스 등)로 옮길 수 있도록 설계돼 있습니다. 이 문서는
포팅에 필요한 데이터 모델, 호출 인터페이스, 확장 프로토콜, 운영 특성을 정리합니다.

---

## 1. 무엇을 제공하나

tree-sitter로 소스를 파싱해서 **SQLite 인덱스**(`tsindex.db`)를 생성하고, 다음
질문에 빠르게 답합니다:

- 어떤 심볼이 어디서 정의됐는가 (`lookup`)
- 이 심볼은 어디서 참조되는가 (`refs`)
- 이 함수는 무엇을 호출하는가 / 누가 이 함수를 호출하는가 (`callees / callers`)
- 이 심볼의 본문 + 컨텍스트를 한 markdown 덩어리로 (`slice`, LLM 컨텍스트용)
- 인덱스 자체의 통계 (`stats`)

지원 언어: **C, C++, Python, Go, Rust, Java, JavaScript, TypeScript** (확장 쉬움).

---

## 2. 데이터 모델

### 2.1 정규화된 4-vocab

| `kind` | 의미 | 예시 |
|---|---|---|
| `function` | 호출 가능한 모든 것 | 함수, 메서드, function-like 매크로, Rust `macro_rules!`, JS arrow |
| `type` | 모양/계약 | C struct/union/enum/typedef, Java class/interface/enum, Python class, Rust struct/enum/trait, TS interface/type |
| `variable` | 런타임 storage | 전역 변수, 클래스 필드 (parent로 구분) |
| `constant` | 컴파일 시점 상수 | C `#define X 5`, Java `static final`, Rust `const`, Python `UPPER_SNAKE` |

`kind_raw`는 원본 AST 노드 이름을 보존합니다 (예: `preproc_function_def`,
`class_specifier`, `arrow_function`). 정밀 분석이 필요할 때 사용.

### 2.2 SQLite 스키마

```sql
files (
    path TEXT PK, size, lines, sha1, has_error, n_symbols,
    identifiers TEXT,  -- JSON: 그 파일에 등장한 모든 식별자 이름 (incremental rebuild용)
    language TEXT
)

symbols (
    id PK, name, kind, file, line, col, end_line, is_definition,
    language, kind_raw, modifiers TEXT, parent TEXT,
    signature, return_type, enum_values TEXT, params TEXT
)
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_kind ON symbols(kind);
CREATE INDEX idx_symbols_file ON symbols(file);
CREATE INDEX idx_symbols_language ON symbols(language);

refs (
    id PK, name, kind, file, line, col, language
)
-- kind ∈ {call, name, type}
CREATE INDEX idx_refs_name ON refs(name);
CREATE INDEX idx_refs_file ON refs(file);
CREATE INDEX idx_refs_kind ON refs(kind);
CREATE INDEX idx_refs_language ON refs(language);

meta (key PK, value TEXT)
-- schema_version, root, built_at, preproc_fingerprint, preprocessing(JSON)
```

`schema_version` 또는 `preproc_fingerprint` 가 다르면 인덱스는 자동으로 무효화돼
풀 리빌드가 강제됩니다.

### 2.3 ref kinds (refs.kind)

| kind | 의미 |
|---|---|
| `call` | `name(args)` 형태의 호출 사이트 |
| `name` | 정의된 심볼 이름의 단순 mention (예: 함수 포인터 전달, 매크로 인자) |
| `type` | 타입 위치의 식별자 참조 |

호출 그래프는 `kind=call ∪ kind=name` 중 타겟이 `function`인 것만 사용.

---

## 3. 라이브러리로 직접 호출

```python
from tsindex import build, load_index, build_callgraph, cmd_slice
from pathlib import Path

# 1. 인덱스 빌드 (idempotent, incremental 자동)
build(Path("./src"), Path("./out.db"),
      defs_path=Path("./tsindex.defs"),  # C/C++만 필요. 다른 언어는 None.
      undef_unknown_configs=True,
      force_full=False,
      verbose=False)

# 2. 인덱스 로드
idx = load_index(Path("./out.db"))

# 3. 인덱스 쿼리 (SQL 인덱스 활용)
hits = idx.find_symbols(name="MyClass", kind="type")
refs = idx.find_refs(name="my_function", kind="call")
in_file = idx.find_symbols(file="src/util.py")
body_refs = idx.find_refs_in_range("src/util.py", 10, 50)

# 4. 호출 그래프 (한 번 빌드 ~10-20ms)
calls_of, callers_of, sites_of = build_callgraph(idx)
for callee, cnt in calls_of["my_function"].items():
    print(f"my_function calls {callee} {cnt}x")

# 5. LLM 컨텍스트 슬라이스
cmd_slice(idx, "my_function",
          with_callees=True, with_callers=True,
          with_types=True, with_macros=False,
          depth=2, max_bytes=8000)
```

### 자주 쓰는 패턴

**파일 내 모든 함수**: `idx.find_symbols(kind="function", file=path)`

**클래스의 모든 멤버 (메서드 + 필드)**: SQL 직접 — `WHERE parent = 'MyClass'`

**언어별 통계**: `idx.kind_counts()`, `idx.ref_kind_counts()`

**호출 그래프 BFS** (transitively):
```python
def descendants(start, calls_of, depth):
    seen, frontier = {start}, {start}
    for _ in range(depth):
        nxt = set()
        for f in frontier:
            for c in calls_of.get(f, {}):
                if c not in seen:
                    seen.add(c); nxt.add(c)
        frontier = nxt
    return seen
```

---

## 4. 언어 확장 프로토콜

새 언어를 추가하려면 `LangSpec`을 등록합니다:

```python
LANGUAGES["mylang"] = LangSpec(
    name="mylang",
    exts=(".my",),
    grammar_factory=_lang_mylang,           # lazy: returns tree_sitter Language
    walk_definitions=mylang_walk_definitions,
    walk_refs=mylang_walk_refs,
    preprocess=_noop_preprocess,            # 또는 언어 전용 전처리 함수
)
```

### 4.1 `walk_definitions(root, src, rel, syms)`

`root`는 tree-sitter 루트 노드, `src`는 raw bytes, `rel`는 root-relative 경로,
`syms`는 채울 list. `Symbol` 인스턴스를 append하면 됩니다.

**필수 필드**:
- `name`, `kind` (4-vocab 중 하나), `file=rel`, `line/col/end_line`,
  `is_definition`, `language`

**권장 필드**:
- `kind_raw`: 원본 AST 노드 이름 (정밀 쿼리용)
- `modifiers`: `["static", "async", "pub", "exported", ...]` — 언어 무관 또는 언어 특이
- `parent`: 클래스/네임스페이스/모듈 등 컨테이너 (메서드면 클래스명)
- `signature`: 사람 읽을 시그니처 (`" ".join(text.split())`로 1줄화)
- `params`: 파라미터 이름 list (callgraph에서 shadow 필터로 사용)
- `enum_values`: enum의 경우 variant 이름들
- `return_type`: 함수 반환 타입 텍스트

### 4.2 `walk_refs(root, src, rel, refs, defined_names, identifiers_out, language="...")`

- `defined_names`: 전체 코드베이스에 정의된 심볼 이름 집합 (Pass-1 결과)
- `identifiers_out`: 이 파일에 등장한 모든 식별자 이름을 채울 set (incremental
  rebuild의 Option B에서 사용)
- `refs`: `Ref` 인스턴스 append

**Ref 종류**:
- `kind="call"`: 함수 호출 사이트 (`X(...)` 형태)
- `kind="name"`: 정의된 심볼 이름의 단순 mention (defined_names에 있을 때만)
- `kind="type"`: 타입 위치의 식별자

### 4.3 새 언어 추가 시 체크리스트

1. `pip install tree_sitter_<lang>` 가능한지 확인
2. AST를 작은 스니펫으로 dump해서 노드 타입 파악
3. `<lang>_walk_definitions` 작성 — top-level def + nested 처리
4. `<lang>_walk_refs` 작성 — call/name/identifier 분류
5. `tests/fixtures/<lang>/` 에 샘플 추가
6. `tests/test_<lang>.py` 에 unittest 추가
7. `LANGUAGES` 레지스트리에 등록
8. `python -m unittest tests.test_<lang>` 통과
9. `SCHEMA_VERSION`은 walker 출력 포맷이 바뀔 때만 bump

### 4.4 흔한 함정 (실제로 겪은 것들)

- **Identifier 종류가 여러 개**: tree-sitter는 `identifier`,
  `type_identifier`, `field_identifier`, `property_identifier` 등을 구분.
  walker가 모두 처리해야 함.
- **언어별 method receiver 표기**:
  - Go: `func (p *Point) Method()` — receiver 별도 parameter_list
  - Rust: `impl T { fn ... }` — impl 블록의 type
  - C++: `int Service::process()` — qualified_identifier
  - Python: `def method(self, ...)` — 첫 파라미터가 self
- **다중 declarator**: C/C++의 `int a, b, c;` 같이 한 declaration에 여러 이름
- **prototype vs definition**: C/Java/Rust 모두 본문 없는 선언 가능 →
  `is_definition=False`로 emit, kind은 그대로
- **Async / Variadic / Default args**: parameters_node 파싱 시 케이스 별로 분기

---

## 5. 운영 특성

| 측정 | 값 |
|---|---:|
| 풀 빌드 (265파일 C, 6.5 MB) | ~8s |
| no-op incremental (변경 없음) | ~0.15s |
| 1파일 변경 incremental | ~0.2s |
| `idx.find_symbols(name=X)` | <1ms |
| `cmd_callers/callees` | 15-20ms |
| `cmd_slice` (depth=2, all) | ~20ms |
| 50× find_symbols 연속 | ~0.7ms (0.014ms/each) |

**SQLite DB 크기**: 소스 KiB의 약 3.5배 (24 MB for 6.5 MB C).

**Memory**: 빌드 중 max ~50 MB (모든 raw bytes를 메모리에 보관).

---

## 6. MCP / 외부 서비스로 포팅

### 6.1 MCP server 패턴

```python
# server.py
from mcp.server import Server
from tsindex import load_index, build_callgraph, cmd_slice
from pathlib import Path

INDEX = Path("./tsindex.db")
_store = None
_callgraph_cache = None

def store():
    global _store, _callgraph_cache
    if _store is None:
        _store = load_index(INDEX)
        _callgraph_cache = None
    return _store

server = Server("tsindex")

@server.tool("lookup_symbol")
def lookup_symbol(name: str, kind: str = None) -> list[dict]:
    return store().find_symbols(name=name, kind=kind)

@server.tool("get_callers")
def get_callers(name: str) -> list[dict]:
    global _callgraph_cache
    if _callgraph_cache is None:
        _callgraph_cache = build_callgraph(store())
    _, callers_of, sites_of = _callgraph_cache
    return [
        {"caller": fn, "count": n, "sites": sites_of.get((fn, name), [])}
        for fn, n in callers_of.get(name, {}).items()
    ]

@server.tool("get_slice")
def get_slice(name: str, with_callees: bool = False,
              with_callers: bool = False, depth: int = 1) -> str:
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_slice(store(), name,
                  with_callees=with_callees, with_callers=with_callers,
                  with_types=True, with_macros=False,
                  depth=depth, max_bytes=8000)
    return buf.getvalue()
```

### 6.2 인덱스 freshness 전략

agent 도구는 보통 long-running 프로세스. 인덱스 재빌드 트리거:

- **수동**: `rebuild_index()` 툴 노출, agent가 명시적으로 호출
- **시간 기반**: 매 N분마다 백그라운드 rebuild (incremental이므로 거의 무료)
- **파일시스템 watch**: `watchdog`으로 .c/.py 변경 감지 → debounced rebuild

incremental rebuild는 변경 안 된 파일은 sha1 비교만 하므로 (~50ms/265파일)
거의 무료. 부담 없이 자주 돌릴 수 있습니다.

### 6.3 멀티 리포 지원

한 인덱스 = 한 root. 여러 리포는 여러 DB로 분리:

```python
INDEXES = {
    "frontend": load_index(Path("./tsindex.frontend.db")),
    "backend":  load_index(Path("./tsindex.backend.db")),
    "kernel":   load_index(Path("./tsindex.kernel.db")),
}

@server.tool("lookup_symbol")
def lookup_symbol(name: str, repo: str = None, kind: str = None) -> list[dict]:
    if repo:
        return INDEXES[repo].find_symbols(name=name, kind=kind)
    out = []
    for r, idx in INDEXES.items():
        for s in idx.find_symbols(name=name, kind=kind):
            out.append({**s, "_repo": r})
    return out
```

---

## 7. 알려진 한계와 우회

### 7.1 syntactic only — 의미 분석 없음

tree-sitter는 의미를 모릅니다. 영향:
- 동명 함수/메서드 구분 안 됨 → `parent` 필드로 보강
- 함수 포인터/콜백 추적 불가 → `kind="name"` ref로 부분 회복
- 타입 추론 안 됨 → 호출 그래프는 이름 일치 수준

정밀 의미 분석이 필요하면 clangd / pyright / rust-analyzer와 하이브리드 권장.

### 7.2 C 전처리기 우회의 한계

C/C++의 `tsindex.defs` + auto-undef + rewriter 조합은 ~99% 파일을 정상 파싱
수준까지 끌어올리지만:
- 매크로 내부 의미는 알 수 없음 (`CONCAT(a, b)` 같은 token paste)
- 컨디션이 너무 복잡한 `#if`는 unifdef가 처리 못 함 → 그 파일만 fallback
- 정말 깨진 코드 (`(unbalanced paren`)는 우리도 못 고침

### 7.3 incremental의 cross-file ref 일관성

`build()`의 Option B 처리:
- 변경된 파일에 **새 심볼이 추가**되면, 변경 안 된 파일 중 그 이름을 mention하는
  파일을 자동으로 re-Pass2 합니다 (`identifiers` 필드 사용)
- 그러나 변경 안 된 파일이 새로 추가된 함수를 **호출**하는데 그 호출 사이트가
  이전 인덱스의 `identifiers`에 없었다면 (없을 일은 거의 없지만), 누락 가능.
- 의심되면 `--full` 한 번이면 100% 회복.

---

## 8. 의존성

- Python 3.10+ (dataclass 슬롯, `list[str]` 등 사용)
- `tree-sitter` 0.23+
- 언어별 `tree_sitter_<lang>` wheel
- `unifdef` (C/C++ 전처리, 시스템 패키지 또는 brew)
- 표준 라이브러리: `sqlite3`, `hashlib`, `subprocess`, `re`

---

## 9. 테스트

```bash
python -m unittest discover -s tests -v
```

각 언어마다 `tests/fixtures/<lang>/` 의 작은 샘플로 walker의 핵심 케이스를
검증. 새 walker 추가 시 동일한 패턴 따르면 됨.

`tests/helpers.py`의 `build_fixture(lang)`이 임시 DB를 만들고 (store,
by_name dict)을 반환. `setUpClass`에서 한 번 빌드해 method들 공유.

---

## 10. 마이그레이션

`SCHEMA_VERSION` 상수를 bump하면 옛 DB는 자동으로 무효화돼서 풀 리빌드됨.
walker 출력 포맷이 호환되지 않는 변경 (필드 추가/제거)일 때만 bump.
