---
title: Torture fixture
tags: [docs, i18n]
sidebar_position: 3
---

# torture ![build](https://img.shields.io/badge/build-passing-green) [![cov](https://codecov.io/gh/x/y/badge.svg)](https://codecov.io/gh/x/y)

A paragraph with `inline code`, an ENV_VAR, a --flag, a ./relative/path.py,
a [link with anchor](#installation), an <abbr title="HyperText">HTML</abbr>
tag, and a bare URL https://example.org/path?q=1#frag. Sentence ends here.

| Command | Effect |
|---|---|
| `koine gate --forbid-machine-only` | strict mode |
| `pip install -e ".[agents]"` | with ADK |

<details>
<summary>Click to expand</summary>

Nested prose inside HTML with `code` and GEMINI_API_KEY=abc123 assignment.

</details>

```python
# fences are never touched, even with "prose" inside
print("ceci n'est pas une doc")
```

- List item with [text](https://example.org) and trailing punctuation!
- Item with a Windows-ish token C:\path is out of scope (documented limit)

## Installation

1. Numbered item with `backticks` and --two-flags --side-by-side
2. Unicode: café, naïve, 日本語, кириллица — must survive untouched
