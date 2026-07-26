"""Prompt construction and the JSON schema for review output."""

from __future__ import annotations

from app.diff.parser import FileDiff, render_annotated_diff

REVIEW_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Short overall assessment of the reviewed changes.",
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {
                        "type": "integer",
                        "description": "New-file line number as annotated in the diff.",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "Bug",
                            "Security",
                            "Performance",
                            "Code Quality",
                            "Best Practice",
                            "Style",
                        ],
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low", "info"],
                    },
                    "problem": {"type": "string"},
                    "suggestion": {
                        "type": "string",
                        "description": "Concrete fix, with a code example when possible.",
                    },
                },
                "required": ["file", "line", "category", "severity", "problem", "suggestion"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "findings"],
    "additionalProperties": False,
}

_SYSTEM_ID = """\
Kamu adalah reviewer kode senior yang teliti. Kamu mereview diff dari sebuah
Pull Request di GitHub dan menuliskan temuan dalam Bahasa Indonesia.

Fokus review:
- Potensi bug dan edge case yang tidak ditangani
- Celah keamanan: SQL injection, XSS, hardcoded secret, validasi input,
  otorisasi yang lemah, dependency rentan
- Masalah performa: query N+1, loop tidak efisien, memory leak
- Error handling dan logging
- Keterbacaan, penamaan, duplikasi kode
- Kesesuaian dengan konvensi bahasa/framework yang dipakai

Aturan:
- Laporkan SEMUA temuan nyata, termasuk yang kamu ragu — jangan menyaring
  berdasarkan tingkat kepentingan; penyaringan dilakukan di tahap berikutnya.
  Sertakan severity dan penjelasan agar bisa dirangking.
- Jangan berkomentar tentang kode yang tidak berubah pada diff, kecuali
  perubahan pada diff menimbulkan masalah di sana.
- Gunakan nomor baris yang tertulis di sisi kiri diff beranotasi
  (format "NOMOR|+ isi"). Nomor itu adalah nomor baris pada file versi baru.
- Untuk setiap temuan beri saran perbaikan konkret, sertakan contoh kode bila
  memungkinkan (gunakan blok kode markdown).
- Jangan mengarang temuan. Jika tidak ada masalah, kembalikan findings kosong
  dengan ringkasan singkat.
- Keluarkan HANYA JSON sesuai skema yang diminta, tanpa teks lain."""

_SYSTEM_EN = """\
You are a meticulous senior code reviewer. You review a GitHub Pull Request
diff and write your findings in English.

Review focus:
- Potential bugs and unhandled edge cases
- Security issues: SQL injection, XSS, hardcoded secrets, input validation,
  weak authorization, vulnerable dependencies
- Performance problems: N+1 queries, inefficient loops, memory leaks
- Error handling and logging
- Readability, naming, code duplication
- Conformance to the conventions of the language/framework in use

Rules:
- Report EVERY real issue you find, including ones you are uncertain about —
  do not filter by importance; a later stage does the filtering. Include a
  severity and explanation so findings can be ranked.
- Do not comment on unchanged code unless a change in the diff creates a
  problem there.
- Use the line numbers annotated on the left of the diff
  (format "NUMBER|+ content"). They are line numbers in the NEW file version.
- For each finding give a concrete fix, with a code example when possible
  (use markdown code blocks).
- Do not invent findings. If there are no issues, return an empty findings
  list with a short summary.
- Output ONLY JSON matching the requested schema, nothing else."""


def build_system_prompt(language: str) -> str:
    return _SYSTEM_ID if language == "id" else _SYSTEM_EN


def build_user_prompt(
    language: str,
    pr_title: str,
    pr_description: str,
    files: list[FileDiff],
    incremental: bool,
) -> str:
    rendered = "\n\n".join(render_annotated_diff(f) for f in files)
    description = (pr_description or "").strip()[:2000]

    if language == "id":
        scope = (
            "Ini adalah review INKREMENTAL: hanya perubahan sejak review terakhir. "
            "Jangan mengulang temuan lama."
            if incremental
            else "Ini adalah review PENUH terhadap seluruh diff PR."
        )
        return (
            f"Judul PR: {pr_title}\n"
            f"Deskripsi PR:\n{description or '(tidak ada)'}\n\n"
            f"{scope}\n\n"
            f"Diff beranotasi:\n\n{rendered}"
        )

    scope = (
        "This is an INCREMENTAL review: only changes since the last review. "
        "Do not repeat earlier findings."
        if incremental
        else "This is a FULL review of the entire PR diff."
    )
    return (
        f"PR title: {pr_title}\n"
        f"PR description:\n{description or '(none)'}\n\n"
        f"{scope}\n\n"
        f"Annotated diff:\n\n{rendered}"
    )
