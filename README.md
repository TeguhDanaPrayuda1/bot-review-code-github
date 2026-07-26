# 🤖 GitHub Code Reviewer Bot

Bot review kode otomatis untuk Pull Request GitHub. Setiap kali PR dibuka atau
mendapat commit baru, bot mengambil diff, mengirimkannya ke LLM (Anthropic
Claude), lalu memposting hasil review sebagai **komentar inline** pada baris
yang relevan plus **satu komentar ringkasan**. Bot tidak pernah
approve/request-changes — hanya berkomentar.

## Fitur

- **Webhook GitHub App** (`pull_request` opened/reopened/synchronize + `push`
  pada branch yang punya PR terbuka), dengan verifikasi `X-Hub-Signature-256`.
- **Review inkremental** — commit tambahan hanya direview terhadap perubahan
  sejak review terakhir (via GitHub compare API), sehingga komentar tidak
  berulang.
- **Idempoten** — satu commit SHA tidak pernah direview dua kali (state SQLite),
  aman terhadap event `push` + `synchronize` ganda maupun webhook redelivery.
- **Temuan terstruktur** — file, baris, kategori (Bug/Security/Performance/
  Code Quality/Best Practice/Style), severity (Critical→Info), penjelasan, dan
  saran perbaikan dengan contoh kode. Output LLM dipaksa JSON via structured
  outputs.
- **Konfigurasi** — pola file yang dilewati, batas ukuran diff (per file & per
  panggilan LLM, dengan chunking untuk PR besar), bahasa komentar (id/en),
  severity minimum.
- **Tahan gangguan** — retry exponential backoff untuk GitHub API (menghormati
  `Retry-After`/`X-RateLimit-Reset`) dan LLM (retry bawaan SDK Anthropic);
  review berjalan di background sehingga webhook selalu dijawab cepat; kegagalan
  satu chunk LLM tidak menggagalkan seluruh review; logging terstruktur (JSON).
- **Provider LLM bisa diganti** — lapisan abstraksi `app/llm/base.py`;
  implementasi bawaan: Anthropic Claude.

## Struktur proyek

```
app/
├── main.py              # FastAPI: endpoint /webhook & /healthz, dispatch event
├── config.py            # Konfigurasi dari environment (.env)
├── models.py            # Finding, Severity, Category, ReviewResult
├── logging_config.py    # structlog (JSON)
├── github/
│   ├── auth.py          # JWT GitHub App + cache installation token
│   ├── client.py        # Klien REST GitHub dengan retry & rate-limit handling
│   └── webhook.py       # Verifikasi X-Hub-Signature-256
├── diff/
│   ├── parser.py        # Parse unified diff, mapping nomor baris, anotasi
│   └── filters.py       # Skip pattern, truncation, chunking
├── llm/
│   ├── base.py          # Abstraksi provider (mudah ganti provider)
│   └── anthropic_provider.py
└── review/
    ├── engine.py        # Orkestrasi review end-to-end
    ├── prompts.py       # Prompt + JSON schema output
    └── state.py         # Idempotensi (SQLite)
tests/                   # Unit test parser diff & filter file
```

## 1. Membuat GitHub App

1. Buka **Settings → Developer settings → GitHub Apps → New GitHub App**
   (di akun pribadi atau organisasi).
2. Isi:
   - **GitHub App name**: mis. `my-code-reviewer`
   - **Homepage URL**: bebas (mis. URL repo ini)
   - **Webhook URL**: `https://<domain-anda>/webhook`
     (saat pengembangan lokal: URL ngrok, lihat bagian di bawah)
   - **Webhook secret**: string acak yang kuat — samakan dengan
     `GITHUB_WEBHOOK_SECRET` di `.env`
     (mis. hasil `openssl rand -hex 32`)
3. **Permissions** (Repository permissions):
   | Permission | Akses | Untuk apa |
   |---|---|---|
   | Pull requests | **Read & write** | Membaca diff PR, memposting review |
   | Contents | **Read-only** | Compare antar commit (review inkremental) |
   | Metadata | Read-only (otomatis) | — |
4. **Subscribe to events**: centang **Pull request** dan **Push**.
5. Klik **Create GitHub App**, lalu:
   - Catat **App ID** → `GITHUB_APP_ID`
   - **Generate a private key** → unduh file `.pem` →
     `GITHUB_PRIVATE_KEY_PATH` (atau isi kontennya ke `GITHUB_PRIVATE_KEY`
     dengan newline diganti `\n`)
6. **Install App** ke akun/organisasi dan pilih repositori yang ingin direview.

## 2. Menjalankan lokal (dengan ngrok)

```bash
# 1. Siapkan environment
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Konfigurasi
cp .env.example .env
# edit .env: GITHUB_APP_ID, GITHUB_WEBHOOK_SECRET, private key, ANTHROPIC_API_KEY

# 3. Jalankan server
uvicorn app.main:app --reload --port 8000

# 4. Di terminal lain, expose ke internet
ngrok http 8000
```

Salin URL https dari ngrok (mis. `https://abc123.ngrok-free.app`) dan set
**Webhook URL** GitHub App ke `https://abc123.ngrok-free.app/webhook`.

Uji: buka PR di repositori tempat App terpasang, atau kirim ulang delivery dari
tab **Advanced → Recent Deliveries** di pengaturan App. Log JSON muncul di
terminal server; hasil review muncul sebagai komentar di PR dalam beberapa
puluh detik (tergantung ukuran diff).

## 3. Deploy (Docker)

```bash
cp .env.example .env         # lalu edit
# letakkan private key sebagai ./github-app.private-key.pem
docker compose up -d --build
```

Layanan berjalan di port 8000. Letakkan di belakang reverse proxy dengan TLS
(Caddy/Nginx/Traefik) dan arahkan Webhook URL GitHub App ke
`https://<domain>/webhook`. State idempotensi disimpan di volume
`review-state` sehingga selamat dari restart.

Catatan deploy:
- Endpoint webhook selalu menjawab `202` cepat; proses review berjalan async.
  Timeout LLM (`LLM_TIMEOUT_SECONDS`) tidak memengaruhi delivery webhook.
- Jika menjalankan lebih dari satu replika, gunakan satu volume state bersama
  (atau ganti `state.py` ke Postgres/Redis) agar idempotensi tetap terjaga.

## 4. Menjalankan test

```bash
pip install -r requirements.txt
pytest
```

Test mencakup parsing unified diff (hunk, mapping nomor baris baru,
penentuan baris yang bisa dikomentari, rendering diff beranotasi) dan
pemfilteran file (skip pattern, truncation, chunking).

## Konfigurasi penting

| Variabel | Default | Keterangan |
|---|---|---|
| `SKIP_PATTERNS` | lockfile, `dist/`, `node_modules/`, aset biner, dll. | Glob dipisah koma; akhiri `/` untuk folder (cocok di kedalaman berapa pun) |
| `MAX_FILE_DIFF_CHARS` | 30000 | Diff per file dipotong sampai batas ini |
| `MAX_CHUNK_CHARS` | 60000 | PR besar dipecah per chunk file per panggilan LLM |
| `SKIP_FILE_OVER_CHARS` | 100000 | File dengan diff lebih besar dilewati total |
| `REVIEW_LANGUAGE` | `id` | `id` atau `en` |
| `MIN_SEVERITY` | `low` | Temuan di bawah ini dibuang (`critical`\|`high`\|`medium`\|`low`\|`info`) |
| `MAX_INLINE_COMMENTS` | 30 | Sisanya dirangkum di komentar ringkasan |
| `LLM_MODEL` | `claude-opus-5` | Model Anthropic yang dipakai |

## Cara kerja (alur)

1. GitHub mengirim webhook → signature diverifikasi → server langsung menjawab
   `202`, review dijadwalkan sebagai background task.
2. Bot mengklaim SHA head di store idempotensi. Jika sudah pernah diklaim
   (mis. event `push` dan `synchronize` untuk commit yang sama), review
   dilewati.
3. PR baru → ambil seluruh diff PR. Commit tambahan → `compare` antara SHA
   review terakhir dan head baru (fallback ke diff penuh bila compare gagal,
   mis. setelah force-push).
4. File difilter (skip pattern, ukuran), diff dipotong bila perlu, lalu
   dikelompokkan per chunk.
5. Tiap chunk dikirim ke LLM dengan diff beranotasi nomor baris; output JSON
   (schema-constrained) diparse menjadi temuan.
6. Temuan difilter severity, dipetakan ke baris diff yang valid (snap ke baris
   terdekat bila meleset ≤3 baris), lalu diposting sebagai satu review:
   komentar inline + ringkasan. Temuan yang tidak bisa dipetakan masuk ke
   ringkasan. Jika GitHub menolak komentar inline (422), seluruh temuan
   diposting sebagai ringkasan agar tidak hilang.

## Mengganti provider LLM

Dua provider tersedia bawaan:

**1. `anthropic`** (default) — Anthropic Claude langsung, atau gateway yang
Anthropic-compatible (endpoint `/v1/messages`):

```env
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=<api key>
# opsional, untuk gateway Anthropic-compatible:
LLM_BASE_URL=https://api.gateway-anda.tld
```

**2. `openai_compatible`** — gateway/agregator apa pun dengan endpoint
`/chat/completions` dan auth `Authorization: Bearer` (mis. openagentic.id,
OpenRouter, server inference lokal):

```env
LLM_PROVIDER=openai_compatible
LLM_API_KEY=<api key dari gateway>
LLM_BASE_URL=https://api.gateway-anda.tld/v1
LLM_MODEL=<nama model sesuai daftar gateway>
```

Contoh untuk openagentic.id:

```env
LLM_PROVIDER=openai_compatible
LLM_API_KEY=sk-...
LLM_BASE_URL=https://openagentic.id/api/v1
LLM_MODEL=mimo-v2.5-pro
```

Uji konfigurasi sebelum menghubungkan bot ke GitHub — script berikut mengirim
satu diff contoh (berisi SQL injection, hardcoded secret, dan bare `except`)
dan menampilkan temuan yang berhasil diparse:

```bash
python scripts/check_llm.py
```

Catatan: pada provider `openai_compatible`, output JSON tidak dipaksa lewat
schema di sisi API (banyak gateway tidak mendukungnya) — prompt sudah meminta
JSON-only dan parser di engine bersifat lenient, jadi tetap berfungsi; model
yang lemah mengikuti instruksi bisa sesekali menghasilkan output yang gagal
diparse (chunk dilewati dan dicatat di ringkasan).

Untuk provider lain, implementasikan subclass `LLMProvider` di `app/llm/`,
daftarkan di `create_provider()` (`app/llm/base.py`), lalu set
`LLM_PROVIDER=<nama>`. Interface-nya hanya satu method:
`complete(system, user, json_schema) -> str`.

## Asumsi yang diambil

- **Draft PR dilewati** — direview saat berubah menjadi ready (event
  `synchronize`/`reopened` berikutnya, atau push baru).
- **Review inkremental berbasis compare `lastSHA...head`** — setelah
  force-push, compare bisa gagal/kosong; bot fallback ke review diff penuh.
- **Baris temuan di luar diff** — komentar inline GitHub hanya valid pada baris
  yang muncul di hunk. Temuan yang melesetnya ≤3 baris di-snap ke baris
  terdekat; selebihnya dipindah ke komentar ringkasan.
- **File terhapus tidak direview** (tidak ada baris sisi kanan untuk
  dikomentari); file biner/patch kosong juga dilewati.
- **State idempotensi lokal (SQLite)** — cukup untuk satu instance; untuk
  multi-replika gunakan storage bersama.
- **Satu review per commit** — event `push` dan `pull_request.synchronize`
  saling tumpang tindih dan dideduplikasi lewat idempotensi, bukan lewat
  debouncing waktu.
- **`stop_reason: refusal`** dari model diperlakukan sebagai kegagalan chunk
  (dicatat, chunk dilewati); bila perlu bisa ditambah fallback model server-side
  (parameter `fallbacks` Anthropic) di `anthropic_provider.py`.
