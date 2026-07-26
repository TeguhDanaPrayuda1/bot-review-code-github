#!/usr/bin/env python
"""Cek koneksi & konfigurasi LLM tanpa menjalankan seluruh bot.

Mengirim satu diff contoh ke provider yang dikonfigurasi di .env, lalu
menampilkan temuan yang berhasil diparse — sehingga Anda tahu apakah API key,
base URL, dan nama model sudah benar sebelum menghubungkan bot ke GitHub.

Pemakaian:
    python scripts/check_llm.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.diff.parser import FileDiff, parse_patch  # noqa: E402
from app.llm.base import LLMError, create_provider  # noqa: E402
from app.review.engine import ReviewEngine  # noqa: E402
from app.review.prompts import (  # noqa: E402
    REVIEW_JSON_SCHEMA,
    build_system_prompt,
    build_user_prompt,
)

SAMPLE_PATCH = """@@ -1,5 +1,14 @@
 import sqlite3
+
+API_KEY = "sk-prod-hardcoded-secret-value"
+
+def login(db, username, password):
+    cur = db.cursor()
+    cur.execute("SELECT * FROM users WHERE name = '" + username + "'")
+    try:
+        return cur.fetchone()[0]
+    except:
+        return None
+
 def health():
     return "ok"
"""


async def main() -> int:
    settings = get_settings()
    print("Konfigurasi:")
    print(f"  provider : {settings.llm_provider}")
    print(f"  model    : {settings.llm_model}")
    print(f"  base_url : {settings.llm_base_url or '(default provider)'}")
    key = settings.llm_api_key or settings.anthropic_api_key
    print(f"  api_key  : {(key[:10] + '...' + key[-4:]) if key else '(kosong!)'}")
    print()

    file_diff = FileDiff(
        path="auth/login.py",
        status="modified",
        patch=SAMPLE_PATCH,
        hunks=parse_patch(SAMPLE_PATCH),
    )
    system = build_system_prompt(settings.review_language)
    user = build_user_prompt(
        settings.review_language, "Add login endpoint", "", [file_diff], incremental=False
    )

    print("Mengirim diff contoh ke LLM...")
    try:
        raw = await create_provider(settings).complete(system, user, REVIEW_JSON_SCHEMA)
    except LLMError as exc:
        print(f"\n❌ GAGAL memanggil LLM: {exc}")
        print("\nPeriksa: LLM_API_KEY, LLM_BASE_URL, dan LLM_MODEL di .env Anda.")
        return 1

    print(f"✅ Respons diterima ({len(raw)} karakter).\n")

    try:
        result = ReviewEngine._parse_result(raw)
    except Exception as exc:
        print(f"❌ Respons bukan JSON yang valid: {exc}")
        print(f"\nRespons mentah:\n{raw[:1500]}")
        print("\nModel ini mungkin kurang patuh pada instruksi JSON-only.")
        return 1

    print(f"Ringkasan: {result.summary}\n")
    print(f"Temuan ({len(result.findings)}):")
    for finding in result.findings:
        print(f"  • {finding.file}:{finding.line} [{finding.severity.value}] "
              f"{finding.category.value}")
        print(f"    {finding.problem}")

    if not result.findings:
        print("\n⚠️  Tidak ada temuan pada diff yang jelas bermasalah ini —")
        print("   model mungkin terlalu lemah untuk dipakai review.")
        return 1

    print("\n✅ Konfigurasi LLM siap dipakai.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
